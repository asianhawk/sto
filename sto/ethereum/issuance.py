"""Issuing out tokenised shares."""
from decimal import Decimal
from logging import Logger

import colorama
import requests
from tqdm import tqdm

from sto.ethereum.txservice import EthereumStoredTXService, verify_on_etherscan

from sto.ethereum.utils import get_abi, check_good_private_key, create_web3
from sto.ethereum.exceptions import BadContractException

from sto.models.implementation import BroadcastAccount, PreparedTransaction
from sqlalchemy.orm import Session
from typing import Union, Optional
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput


class NeedAPIKey(RuntimeError):
    pass


def deploy_token_contracts(logger: Logger,
                          dbsession: Session,
                          network: str,
                          ethereum_node_url: Union[str, Web3],
                          ethereum_abi_file: Optional[str],
                          ethereum_private_key: Optional[str],
                          ethereum_gas_limit: Optional[int],
                          ethereum_gas_price: Optional[int],
                          name: str,
                          symbol: str,
                          amount: int,
                          transfer_restriction: str):
    """Issue out a new Ethereum token."""

    assert type(amount) == int
    decimals = 18  # Everything else is bad idea

    check_good_private_key(ethereum_private_key)

    abi = get_abi(ethereum_abi_file)

    web3 = create_web3(ethereum_node_url)

    # We do not have anything else implemented yet
    assert transfer_restriction == "unrestricted"

    service = EthereumStoredTXService(network, dbsession, web3, ethereum_private_key, ethereum_gas_price, ethereum_gas_limit, BroadcastAccount, PreparedTransaction)

    logger.info("Starting creating transactions from nonce %s", service.get_next_nonce())

    # Deploy security token
    note = "Deploying token contract for {}".format(name)
    deploy_tx1 = service.deploy_contract("SecurityToken", abi, note, constructor_args={"_name": name, "_symbol": symbol})  # See SecurityToken.sol

    # Deploy transfer agent
    note = "Deploying unrestricted transfer policy for {}".format(name)
    deploy_tx2 = service.deploy_contract("UnrestrictedTransferAgent", abi, note)

    # Set transfer agent
    note = "Whitelisting deployment account for {} issuer control".format(name)
    contract_address = deploy_tx1.contract_address
    update_tx1 = service.interact_with_contract("SecurityToken", abi, contract_address, note, "addAddressToWhitelist", {"addr": deploy_tx1.get_from()})
    assert update_tx1.contract_deployment == False

    # Set transfer agent
    note = "Making transfer restriction policy for {} effective".format(name)
    contract_address = deploy_tx1.contract_address
    update_tx2 = service.interact_with_contract("SecurityToken", abi, contract_address, note, "setTransactionVerifier", {"newVerifier": deploy_tx2.contract_address})

    # Issue out initial shares
    note = "Creating {} initial shares for {}".format(amount, name)
    contract_address = deploy_tx1.contract_address
    amount_18 = int(amount * 10**decimals)
    update_tx3 = service.interact_with_contract("SecurityToken", abi, contract_address, note, "issueTokens", {"value": amount_18})

    logger.info("Prepared transactions for broadcasting for network %s", network)
    logger.info("STO token contract address will be %s%s%s after broadcast", colorama.Fore.LIGHTGREEN_EX, deploy_tx1.contract_address, colorama.Fore.RESET)
    return [deploy_tx1, deploy_tx2, update_tx1, update_tx2, update_tx3]


def contract_status(logger: Logger,
                          dbsession: Session,
                          network: str,
                          ethereum_node_url: str,
                          ethereum_abi_file: str,
                          ethereum_private_key: str,
                          ethereum_gas_limit: str,
                          ethereum_gas_price: str,
                          token_contract: str):
    """Poll STO contract status."""

    abi = get_abi(ethereum_abi_file)

    web3 = create_web3(ethereum_node_url)

    service = EthereumStoredTXService(network, dbsession, web3, ethereum_private_key, ethereum_gas_price, ethereum_gas_limit, BroadcastAccount, PreparedTransaction)
    contract = service.get_contract_proxy("SecurityToken", abi, token_contract)

    try:
        logger.info("Name: %s", contract.functions.name().call())
        logger.info("Symbol: %s", contract.functions.symbol().call())
        supply = contract.functions.totalSupply().call()
        human_supply = Decimal(supply) / Decimal(10 ** contract.functions.decimals().call())
        raw_balance = contract.functions.balanceOf(service.get_or_create_broadcast_account().address).call()
        normal_balance = Decimal(raw_balance) / Decimal(10 ** contract.functions.decimals().call())
        logger.info("Total supply: %s", human_supply)
        logger.info("Decimals: %d", contract.functions.decimals().call())
        logger.info("Owner: %s", contract.functions.owner().call())
        logger.info("Broadcast account token balance: %f", normal_balance)
        logger.info("Transfer verified: %s", contract.functions.transferVerifier().call())
    except BadFunctionCallOutput as e:
        raise BadContractException("Looks like this is not a token contract address. Please check on EtherScan that the address presents the token contract")

    return {
        "name": contract.functions.name().call(),
        "symbol": contract.functions.symbol().call(),
        "totalSupply": contract.functions.totalSupply().call(),
        "broadcastBalance": raw_balance,
    }



def verify_source_code(logger: Logger,
              dbsession: Session,
              network: str,
              etherscan_api_key: str,
):
    """Verify source code of all unverified deployment transactions."""

    if not etherscan_api_key:
        raise NeedAPIKey("You need to give EtherScan API key in the configuration file. Get one from https://etherscan.io")

    unverified_txs = dbsession.query(PreparedTransaction).filter_by(verified_at=None, result_transaction_success=True, contract_deployment=True)

    logger.info("Found %d unverified contract deployments on %s", unverified_txs.count(), network)

    if unverified_txs.count() == 0:
        logger.info("No transactions to verify.")
        return []

    unverified_txs = list(unverified_txs)

    # HTTP keep-alive
    session = requests.Session()

    # https://stackoverflow.com/questions/41985993/tqdm-show-progress-for-a-generator-i-know-the-length-of
    for tx in unverified_txs:  # type: _PreparedTx
        logger.info("Verifying %s for %s", tx.contract_address, tx.human_readable_description)
        verify_on_etherscan(logger, network, tx, etherscan_api_key, session)
        dbsession.commit()  # Try to minimise file system sync issues

    return unverified_txs
