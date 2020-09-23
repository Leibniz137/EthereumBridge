import os
from pathlib import Path
from shutil import copy, rmtree
from subprocess import run, PIPE
from time import sleep
from typing import List

from brownie import project, network, accounts
from pytest import fixture
from src.signers import EthrSigner, SecretSigner

import src.contracts.ethereum as contracts_package
import tests.integration as integration_package
from src.contracts.ethereum.multisig_wallet import MultisigWallet
from src.contracts.secret.secret_contract import change_admin
from src.event_listener import EventListener
from src.leader import SecretLeader, EthrLeader
from src.manager import Manager
from src.util.common import module_dir
from src.util.web3 import normalize_address

contracts_folder = module_dir(contracts_package)
brownie_project_folder = os.path.join(module_dir(integration_package), 'brownie_project')


@fixture(scope="module")
def make_project(db, test_configuration):
    # init brownie project structure
    project.new(brownie_project_folder)

    # copy contracts to brownie contract folder
    brownie_contracts = os.path.join(brownie_project_folder, 'contracts')

    erc20_contract = '/home/guy/Workspace/dev/EthereumBridge/tests/integration/token_contract/EIP20.sol'
    copy(erc20_contract, os.path.join(brownie_contracts, 'EIP20.sol'))

    multisig_contract = os.path.join(contracts_folder, 'MultiSigSwapWallet.sol')
    copy(multisig_contract, os.path.join(brownie_contracts, 'MultiSigSwapWallet.sol'))

    # for contract in filter(lambda p: p.endswith(".sol"), os.listdir(contracts_folder)):
    #     copy(os.path.join(contracts_folder, contract), os.path.join(brownie_project_folder, 'contracts', contract))
    # copy(os.path.join(contracts_folder, contract), os.path.join(brownie_project_folder, 'contracts', contract))

    # load and compile contracts to project
    brownie_project = project.load(brownie_project_folder, name="IntegrationTests")
    brownie_project.load_config()

    # noinspection PyUnresolvedReferences
    # from brownie.project.IntegrationTests import MultiSigSwapWallet
    network.connect('development')  # connect to ganache cli

    yield network

    # cleanup
    del brownie_project
    sleep(1)
    rmtree(brownie_project_folder, ignore_errors=True)


@fixture(scope="module")
def ethr_signers(event_listener, web3_provider, multisig_wallet, test_configuration, ether_accounts, erc20_contract) \
        -> List[EthrSigner]:
    res = []

    # we will manually create the last signer in test_3
    for acc in ether_accounts[:-1]:
        private_key = acc.privateKey
        address = acc.address

        res.append(EthrSigner(event_listener, web3_provider, multisig_wallet, private_key, address, test_configuration))

    yield res
    rmtree(Path.joinpath(Path.home(), ".bridge_test"), ignore_errors=True)


@fixture(scope="module")
def scrt_signers(event_listener, scrt_signer_keys, web3_provider, multisig_wallet, test_configuration, ethr_signers) -> \
        List[SecretSigner]:
    signers: List[SecretSigner] = []
    for index, key in enumerate(scrt_signer_keys):
        s = SecretSigner(web3_provider, key, multisig_wallet, test_configuration)
        signers.append(s)

    return signers


@fixture(scope="module")
def multisig_wallet(web3_provider, test_configuration, ether_accounts):
    # erc20_contract is here only to deploy and update configuration, can be remove if not working with ERC20
    from brownie.project.IntegrationTests import MultiSigSwapWallet
    normalize_accounts = [normalize_address(acc.address) for acc in ether_accounts]
    swap_contract = MultiSigSwapWallet.deploy(normalize_accounts, test_configuration.signatures_threshold,
                                              {'from': normalize_address(accounts[0])})
    contract_address = str(swap_contract.address)
    return MultisigWallet(web3_provider, contract_address)


@fixture(scope="module")
def erc20_contract(make_project, test_configuration, ether_accounts):
    from brownie.project.IntegrationTests import EIP20
    # solidity contract deploy params
    _initialAmount = 100
    _tokenName = 'TN'
    _decimalUnits = 18
    _tokenSymbol = 'TS'

    erc20 = EIP20.deploy(_initialAmount, _tokenName, _decimalUnits, _tokenSymbol,
                         {'from': normalize_address(accounts[0])})
    test_configuration.mint_token = True
    test_configuration.token_contract_addr = str(erc20.address)
    test_configuration.token_abi = '/home/guy/Workspace/dev/EthereumBridge/tests/integration/token_contract/EIP20.json'
    # Note: we don't return here anything, as it's only created for leader's usage through config


@fixture(scope="module")
def ether_accounts(web3_provider, test_configuration):
    res = []
    for _ in range(test_configuration.signatures_threshold):
        acc = web3_provider.eth.account.create()
        # account[0] is network.eth.coinbase
        web3_provider.eth.sendTransaction({'from': normalize_address(web3_provider.eth.accounts[0]),
                                           'to': normalize_address(acc.address),
                                           'value': 10000000000000000})
        res.append(acc)

    return res


@fixture(scope="module")
def web3_provider(make_project):
    return make_project.web3


@fixture(scope="module")
def manager(event_listener, multisig_wallet, multisig_account, test_configuration):
    manager = Manager(event_listener, multisig_wallet, multisig_account, test_configuration)
    yield manager
    manager.stop_signal.set()


@fixture(scope="module")
def ethr_leader(multisig_account, test_configuration, web3_provider, multisig_wallet, ether_accounts, erc20_contract):
    private_key = ether_accounts[0].privateKey
    address = normalize_address(ether_accounts[0].address)
    leader = EthrLeader(web3_provider, multisig_wallet, private_key, address, test_configuration)
    yield leader
    leader.stop_event.set()


@fixture(scope="module")
def scrt_leader(multisig_account, test_configuration):
    change_admin_q = f"docker exec secretdev secretcli tx compute execute " \
                     f"{test_configuration.secret_contract_address}" \
                     f" '{change_admin(multisig_account.multisig_acc_addr)}' --from a -y"
    res = run(change_admin_q, shell=True, stdout=PIPE, stderr=PIPE)
    return SecretLeader(multisig_account, test_configuration)


@fixture(scope="module")
def event_listener(multisig_wallet, web3_provider, test_configuration):
    listener = EventListener(multisig_wallet, web3_provider, test_configuration)
    yield listener
    listener.stop_event.set()
