import json
import os
import random
import string
import subprocess
from pathlib import Path
from shutil import copy, rmtree
from time import sleep
from typing import List

from brownie import project, network, accounts
from pytest import fixture

from src.contracts.ethereum.erc20 import Erc20
from src.leader.eth.leader import EtherLeader
from src.leader.secret20 import Secret20Leader
from src.signer.eth.signer import EtherSigner
from src.signer.secret20 import Secret20Signer
from src.util.common import Token, SecretAccount
from src.util.config import Config
from src.util.web3 import normalize_address
from tests.integration.conftest import contracts_folder, brownie_project_folder


def rand_str(n):
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for i in range(n))


@fixture(scope="module")
def make_project(db, configuration: Config):

    rmtree(brownie_project_folder, ignore_errors=True)

    # init brownie project structure
    project.new(brownie_project_folder)

    # copy contracts to brownie contract folder
    brownie_contracts = os.path.join(brownie_project_folder, 'contracts')

    erc20_contract = os.path.join(contracts_folder, 'USDT.sol')
    copy(erc20_contract, os.path.join(brownie_contracts, 'USDT.sol'))

    multisig_contract = os.path.join(contracts_folder, 'MultiSigSwapWallet.sol')
    copy(multisig_contract, os.path.join(brownie_contracts, 'MultiSigSwapWallet.sol'))

    # load and compile contracts to project
    brownie_project = project.load(brownie_project_folder, name="IntegrationTests")
    brownie_project.load_config()

    # noinspection PyUnresolvedReferences
    network.connect('development')  # connect to ganache cli

    yield

    # cleanup
    del brownie_project
    sleep(1)
    rmtree(brownie_project_folder, ignore_errors=True)


def init_swap_contracts(configuration: Config) -> (str, str):

    multisig_account = configuration["multisig_acc_addr"]

    tx_data = {"admin": configuration["a_address"].decode(), "name": "Coin Name", "symbol": "ETHR", "decimals": 6,
               "initial_balances": [], "config": {}, "prng_seed": "YWE"}
    print(f"{configuration['a_address'].decode()=}")
    cmd = f"docker exec secretdev secretcli tx compute instantiate 1 --label {rand_str(10)} '{json.dumps(tx_data)}'" \
          f" --from a -b block -y"
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE)

    res = subprocess.run("secretcli query compute list-contract-by-code 1 | jq '.[-1].address'",
                         shell=True, stdout=subprocess.PIPE)
    token_addr = res.stdout.decode().strip()[1:-1]
    res = subprocess.run(f"secretcli q compute contract-hash {token_addr}",
                         shell=True, stdout=subprocess.PIPE).stdout.decode().strip()[2:]
    sn_token_codehash = res

    tx_data = {"owner": multisig_account, "token_address": token_addr,
               "code_hash": sn_token_codehash}

    cmd = f"secretcli tx compute instantiate 2 --label {rand_str(10)} '{json.dumps(tx_data)}'" \
          f" --from t1 -b block -y"
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE)

    res = subprocess.run("secretcli query compute list-contract-by-code 2 | jq '.[-1].address'",
                         shell=True, stdout=subprocess.PIPE)
    swap_addr = res.stdout.decode().strip()[1:-1]

    res = subprocess.run(f"secretcli q compute contract-hash {swap_addr}",
                         shell=True, stdout=subprocess.PIPE).stdout.decode().strip()[2:]
    swap_code_hash = res

    tx_data = {"add_minters": {"minters": [swap_addr]}}
    cmd = f"docker exec secretdev secretcli tx compute execute {token_addr} '{json.dumps(tx_data)}'" \
          f" --from a -b block -y"
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE)

    return swap_addr, swap_code_hash, token_addr


@fixture(scope="module")
def setup(make_project, configuration: Config, erc20_token):

    configuration['token_contract_addr'] = erc20_token.address

    sn_swap_erc_addr, swap_code_hash, sn_token_erc_addr = init_swap_contracts(configuration)
    sn_swap_eth_addr, swap_code_hash2, sn_token_eth_addr = init_swap_contracts(configuration)

    configuration["sn_token_contracts"] = {'eth': sn_token_eth_addr, 'erc': sn_token_erc_addr}

    configuration["token_map_eth"] = \
        {erc20_token.address: Token(sn_swap_erc_addr, 'secret-erc', code_hash=swap_code_hash),
         'native': Token(sn_swap_eth_addr, 'secret-eth', code_hash=swap_code_hash2)}

    configuration["token_map_scrt"] = \
        {sn_swap_erc_addr: Token(erc20_token.address, 'erc'),
         sn_swap_eth_addr: Token('native', 'eth')}


@fixture(scope="module")
def erc20_token(make_project):
    from brownie.project.IntegrationTests import TetherToken
    # solidity contract deploy params
    _initialAmount = 1000
    _tokenName = 'Tether USD'
    _decimalUnits = 6
    _tokenSymbol = 'USDT'

    erc20 = TetherToken.deploy(_initialAmount, _tokenName, _tokenSymbol, _decimalUnits, {'from': accounts[0]})
    yield Token(erc20.address, _tokenSymbol)


@fixture(scope="module")
def erc20_contract(multisig_wallet, web3_provider, erc20_token):
    yield Erc20(web3_provider, erc20_token, multisig_wallet.address)


@fixture(scope="module")
def scrt_leader(multisig_account: SecretAccount, multisig_wallet, erc20_contract, configuration: Config):

    token_map = configuration["token_map_eth"]

    leader = Secret20Leader(multisig_account, multisig_wallet, token_map, configuration)
    yield leader
    leader.stop()


@fixture(scope="module")
def scrt_signers(scrt_accounts, multisig_wallet, configuration) -> List[Secret20Signer]:
    signers: List[Secret20Signer] = []
    for account in scrt_accounts:
        s = Secret20Signer(multisig_wallet, account, configuration)
        signers.append(s)

    yield signers

    for signer in signers:
        signer.stop()


@fixture(scope="module")
def ethr_leader(multisig_account, configuration: Config, web3_provider, erc20_token, multisig_wallet, ether_accounts):
    configuration['leader_key'] = ether_accounts[0].key
    configuration['leader_acc_addr'] = normalize_address(ether_accounts[0].address)
    configuration['eth_start_block'] = web3_provider.eth.blockNumber

    token_map = configuration["token_map_scrt"]

    leader = EtherLeader(multisig_wallet, configuration['leader_key'], configuration['leader_acc_addr'], token_map, configuration)

    leader.start()
    yield leader
    leader.stop()


@fixture(scope="module")
def ethr_signers(multisig_wallet, configuration: Config, ether_accounts, erc20_token) -> List[EtherSigner]:
    res = []

    # token_map = {erc20_token.address: Token(configuration['secret_swap_contract_address'],
    #                                         configuration['secret_token_name'])}
    token_map = configuration["token_map_eth"]
    # we will manually create the last signer in test_3
    for acc in ether_accounts[:]:
        private_key = acc.key
        address = acc.address

        res.append(EtherSigner(multisig_wallet, private_key, address, token_map, configuration))

    yield res

    for signer in res:
        signer.stop()
    rmtree(Path.joinpath(Path.home(), ".bridge_test"), ignore_errors=True)
