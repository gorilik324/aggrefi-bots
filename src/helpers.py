import os
import sys
import pactsdk
from typing import Dict
from json_environ import Environ
from pymongo import MongoClient
from algofi_amm.v0.client import AlgofiAMMMainnetClient, AlgofiAMMTestnetClient
from tinyman.v1.client import TinymanMainnetClient, TinymanTestnetClient
from algosdk.v2client import algod, indexer

from src.classes.account import Account
from src.classes.exceptions import AlgoTradeBotError

# Which Algorand network are we running against (mainnet or testnet).
try:
    network = os.environ["ALGO_NETWORK"].lower()
    if network != "mainnet" and network != "testnet":
        print("ALGO_NETWORK environment variable must be set to either 'mainnet' or 'testnet'.")
        sys.exit(1)
except KeyError:
    print("ALGO_NETWORK environment variable is not set. Please set ALGO_NETWORK to either 'mainnet' or 'testnet'.")
    sys.exit(1)

file_path = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(file_path, f"../env/env-{network}.json")
env = Environ(path=env_path)


# We're using a global variable for the DB client connection so we only create the client
# once, no matter how many times the get_db_client function below is called.
client = None


def get_network():
    return network


def get_db_client() -> (MongoClient | None):
    global client
    if client is None:
        db_host = env("database:host")
        db_user = env("database:user")
        db_password = env("database:password")

        try:
            client = MongoClient(
                f"mongodb+srv://{db_user}:{db_password}@{db_host}/?retryWrites=true&w=majority")
        except BaseException as e:
            print(f"Error connecting to off-chain DB: {e}")
            client = None

    return client


def get_amm_clients(account: Account) -> Dict[str, AlgofiAMMTestnetClient | AlgofiAMMMainnetClient | TinymanTestnetClient | TinymanMainnetClient | pactsdk.PactClient]:
    """Get and return instances of the supported AMM Clients."""
    network = get_network()
    algod_token = env("algod:api_key")
    headers = {
        "X-API-Key": algod_token,
        "User-Agent": "algosdk"
    }

    if network == "testnet":
        algod_address = env("algod:algod:testnet")
        indexer_address = env("algod:indexer:testnet")
        pact_api = env("pact_api:testnet")

        algod_client = algod.AlgodClient(
            algod_token, algod_address, headers=headers)
        indexer_client = indexer.IndexerClient(
            "", indexer_address, headers=headers)

        algofi_client = AlgofiAMMTestnetClient(
            user_address=account.address, algod_client=algod_client, indexer_client=indexer_client)
        tinyman_client = TinymanTestnetClient(
            user_address=account.address, algod_client=algod_client)
    else:
        algod_address = env("algod:algod:mainnet")
        indexer_address = env("algod:indexer:mainnet")
        pact_api = env("pact_api:mainnet")

        algod_client = algod.AlgodClient(
            algod_token, algod_address, headers=headers)
        indexer_client = indexer.IndexerClient(
            "", indexer_address, headers=headers)

        algofi_client = AlgofiAMMMainnetClient(
            user_address=account.address, algod_client=algod_client, indexer_client=indexer_client)
        tinyman_client = TinymanMainnetClient(
            user_address=account.address, algod_client=algod_client)

    pact_client = pactsdk.PactClient(algod_client, pact_api_url=pact_api)

    return {
        'algofi': algofi_client,
        'pactfi': pact_client,
        'tinyman': tinyman_client
    }


def is_algofi_nanoswap_stable_asset_pair(asset1_id: int, asset2_id: int) -> bool:
    """Check and return true if a NanoSwap pool exists on Algofi for the given asset pair.

    Parameters:
    asset1_id (int): Asset 1 on-chain ID
    asset2_id (int): Asset 2 on-chain ID

    Returns:
    bool
    """
    if asset1_id == asset2_id:
        raise AlgoTradeBotError("Assets in asset pair must be different!")

    # Currently there are NanoSwap pools on Algofi for the following stablecoin pairs:
    # STBL/USDC, STBL/USDT, USDC/USDT
    network = get_network()

    if network == "testnet":
        token_asset_ids = [10458941, 26837931]
    else:
        token_asset_ids = [31566704, 312769, 465865291]

    return (asset1_id in token_asset_ids and asset2_id in token_asset_ids)
