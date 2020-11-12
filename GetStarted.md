## Okay, so what now

### Things that will be provided by leader

1. Database hosting + usernames and passwords
2. Transactional encryption key - must be shared between all parties to decrypt, verify and sign transaction
3. Contract addresses
4. General configuration parameters
5. Docker image

### What do you need to do as a signer

#### Light reading

* [How multisig on Secret Network (& Cosmos) works](https://hub.cosmos.network/master/resources/gaiacli.html)

##### Step 1 - Generate your keys
1. Generate your Ethereum key. Either in a PKCS11 compatible device, or as raw bytes
2. Generate your Secret Network key, and export it to a file using `secretcli keys export`

We may also require manual transaction signing, in case manual intervention is necessary, 
so be sure you know how to do that with the keys you generated.

3. Provide leader with Ethereum address and Secret Network public key. These will be used to instantiate multisig addresses and contracts
4. Once done, leader will provide the Secret Network multisig address and Ethereum smart contract address

##### Step 2 - Add Funds to Eth account
5. Send some Eth to your Ethereum account (recommended 10 ETH)

##### Customize docker-compose file (or other docker runner)

Of all available config parameters, the ones that require environment variables are:

* eth_node_address
* secret_node
* db_username
* db_password
* db_host

We recommend setting an .env file and using docker-compose, but you can also use `docker run` or any other
docker runner.

##### Start the signer

`docker-compose up`

##### Customization

If you want to customize the docker image in this image feel free - the executable is managed by supervisor, the configuration
of which can be found in `deployment/config/supervisor.conf`.
You can add a PKCS11 module by adding installation of the client module to the docker image, and
overriding the PKCS11_MODULE environment variable to point to the library `.so` file. Other HSM or key vault support can be added by request 