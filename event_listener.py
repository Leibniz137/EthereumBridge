from json import loads
from threading import Thread
from time import sleep

import config
from temp import abi
from util.web3 import web3_provider, last_confirmable_block, extract_tx_by_address


class EventListener:
    def __init__(self, contract_address="", provider_address=""):
        self.contract_address = contract_address if contract_address else config.contract_address
        self.provider_address = provider_address if provider_address else config.provider_address
        self.provider = web3_provider(self.provider_address)

        self.abi = loads(abi)  # TODO: Receive from ctor
        self.callbacks = []

        Thread(target=self.run).start()

    def register(self, callback: callable):
        self.callbacks.append(callback)

    def run(self):
        current_block = self.provider.eth.getBlock('latest')

        while True:
            if current_block.number > last_confirmable_block(self.provider_address,
                                                             config.blocks_confirmation_required):
                sleep(5)  # TODO: Code Review: so, I used to have a notification over here - however you can't
                          #  filter with confirmation threshold, so i changed it to pooling. I can create my own
                          #  notification mechanisem, but it will add complexity to somethign that should be simple.
                          #  let me know what you thinking.
            else:
                transactions = extract_tx_by_address(self.contract_address, current_block)
                for tx in transactions:
                    for callback in self.callbacks:
                        callback(tx)

                current_block += 1
