import base64
import subprocess
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from pathlib import Path
from typing import Dict

from web3.datastructures import AttributeDict

import src.contracts.ethereum.message as message
from src.contracts.ethereum.multisig_wallet import MultisigWallet
from src.contracts.secret.secret_contract import swap_query_res
from src.util.common import Token
from src.util.config import Config
from src.util.logger import get_logger
from src.util.secretcli import query_scrt_swap
from src.util.web3 import contract_event_in_range, w3, erc20_contract


class EthSignerImpl:  # pylint: disable=too-many-instance-attributes, too-many-arguments
    """
    Used to run through all the blocks starting from the number specified by the 'eth_start_block' config value, and up
    to the current block. After that is done the handle_submission method is used to sign individual transactions
    when triggered by an event listener

    Not 100% sure why we're doing this like this instead of just doing it in a single generic signer thread, but eh,
    it is what it is.

    Saves the last block in a file, which is used on the next execution to tell us where to start so we don't run
    through the same blocks multiple times

    Todo: Naming sucks. This is mostly caused by bad design, and by me not having enough coffee
    """

    def __init__(self, multisig_contract: MultisigWallet, private_key: bytes, account: str,
                 token_map: Dict[str, Token], config: Config):
        # todo: simplify this, pylint is right
        self.multisig_contract = multisig_contract
        self.private_key = private_key
        self.account = account
        self.config = config
        self.logger = get_logger(db_name=config['db_name'],
                                 logger_name=config.get('logger_name', f"{self.__class__.__name__}-{self.account[0:5]}"))

        self.erc20 = erc20_contract()

        self.catch_up_complete = False
        self.cache = self._create_cache()

        self.tracked_tokens = token_map.keys()
        self.token_map = token_map

        self.thread_pool = ThreadPoolExecutor()

    def sign_all_historical_swaps(self):
        self._submission_catch_up()

    def handle_submission(self, submission_event: AttributeDict):
        """ Validates submission event with secret20 network and sends confirmation if valid """
        self._validate_and_sign(submission_event)

    def _create_cache(self):
        # todo: db this shit
        directory = Path.joinpath(Path.home(), self.config['app_data'])
        directory.mkdir(parents=True, exist_ok=True)  # pylint: disable=no-member
        file_path = Path.joinpath(directory, f'submission_events_{self.account[0:5]}')

        return open(file_path, "a+")

    # noinspection PyUnresolvedReferences
    def _validate_and_sign(self, submission_event: AttributeDict):
        """Tries to validate the transaction corresponding to submission id on the smart contract,
        confirms and signs if valid"""
        transaction_id = submission_event.args.transactionId
        self.logger.info(f'Got submission event with transaction id: {transaction_id}, checking status')

        data = self.multisig_contract.submission_data(transaction_id)

        # placeholder - check how this looks for ETH transactions
        # check if submitted tx is an ERC-20 transfer tx
        if data['amount'] == 0 and data['data']:
            fn_name, params = self.erc20.decode_function_input(data['data'].hex())
            data['amount'] = params['amount']
            data['dest'] = params['recipient']

        if not self._is_confirmed(transaction_id, data):
            self.logger.info(f'Transaction {transaction_id} is missing approvals. Checking validity..')

            try:
                if self._is_valid(data):
                    self.logger.info(f'Transaction {transaction_id} is valid. Signing & approving..')
                    self._approve_and_sign(transaction_id)
                else:
                    self.logger.error(f'Failed to validate transaction: {data}')
            except ValueError as e:
                self.logger.error(f"Error parsing secret-20 swap event {data}. Error: {e}")

    def _submission_catch_up(self):
        """ Used to sync the signer with the chain after downtime, utilize local file to keep track of last processed
         block number.
        """

        from_block = self._choose_starting_block()
        to_block = w3.eth.blockNumber - self.config['eth_confirmations']
        self.logger.info(f'starting to catch up from {from_block} to {to_block}..')

        for event in contract_event_in_range(self.multisig_contract, 'Submission',
                                             from_block, to_block):
            self.logger.info(f'Got new Submission event on block: {event.blockNumber}')
            self._update_last_block_processed(event.blockNumber)
            self._validate_and_sign(event)

        self.catch_up_complete = True
        self.logger.info('catch up complete')

    def _choose_starting_block(self) -> int:
        """Returns the block from which we start scanning Ethereum for new tx"""
        from_block = self.cache.read()
        if from_block:  # if we have a record, use it
            return int(from_block)
        return int(self.config.get('eth_start_block', 0))

    def _update_last_block_processed(self, block_num: int):
        self.cache.seek(0)
        self.cache.write(str(block_num))
        self.cache.truncate()
        self.cache.flush()

    def _is_valid(self, submission_data: Dict[str, any]) -> bool:
        # lookup the tx hash in secret20, and validate it.
        self.logger.info(f"Testing validity of {submission_data}")
        nonce = submission_data['nonce']
        token = submission_data['token']

        try:
            if token == '0x0000000000000000000000000000000000000000':
                self.logger.info("Testing secret-ETH to ETH swap")
                swap = query_scrt_swap(nonce, self.token_map['native'].address)
            else:
                self.logger.info(f"Testing {self.token_map[token].address} to {token} swap")
                swap = query_scrt_swap(nonce, self.token_map[token].address)
        except subprocess.CalledProcessError as e:
            self.logger.error(f'Error querying transaction: {e}')
            raise RuntimeError from None

        try:
            swap_data = swap_query_res(swap)
            self.logger.debug(f'Parsing swap info: {swap_data}')
        except (AttributeError, JSONDecodeError) as e:
            raise ValueError from e
        if self._validate_tx_data(swap_data, submission_data):
            self.logger.info(f'Validated successfully')
            return True
        self.logger.info(f'Failed to validate')
        return False

    def _validate_tx_data(self, swap_data: dict, submission_data: dict) -> bool:
        """
        This used to verify secret-20 <-> ether tx data
        :param swap_data: the data from secret20 contract query
        :param submission_data: the data from the proposed tx on the smart contract
        """
        if int(swap_data['amount']) != int(submission_data['amount']):
            self.logger.error(f'Invalid transaction - {swap_data["amount"]} does not match {submission_data["amount"]}')
            return False

        dest = base64.standard_b64decode(swap_data['destination']).decode()
        if dest != submission_data['dest']:
            self.logger.error(f'Invalid transaction - {dest} does not match {submission_data["dest"]}')
            return False

        return True

    def _is_confirmed(self, transaction_id: int, submission_data: Dict[str, any]) -> bool:
        """Checks with the data on the contract if signer already added confirmation or if threshold already reached"""

        # check if already executed
        if submission_data['executed']:
            return True

        # check if signer already signed the tx
        if self.multisig_contract.contract.functions.confirmations(transaction_id, self.account).call():
            return True

        return False

    def _approve_and_sign(self, submission_id: int):
        """
        Sign the transaction with the signer's private key and then broadcast
        Note: This operation costs gas
        """
        msg = message.Confirm(submission_id)
        tx_hash = self.multisig_contract.confirm_transaction(self.account, self.private_key, msg)
        self.logger.info(msg=f"Signed transaction - signer: {self.account}, signed msg: {msg}, "
                             f"tx hash: {tx_hash.hex()}")