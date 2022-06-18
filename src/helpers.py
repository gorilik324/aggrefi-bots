import os
import sys
import json
import pactsdk
from decimal import Decimal
from typing import Any, Dict
from json_environ import Environ
from pymongo import MongoClient
from algofi_amm.v0.client import AlgofiAMMMainnetClient, AlgofiAMMTestnetClient
from algofi_amm.v0.config import PoolType, PoolStatus
from tinyman.v1.client import TinymanMainnetClient, TinymanTestnetClient
from algosdk.v2client import algod, indexer

from src.classes.account import Account
from src.classes.asset import AlgoAsset, SwapAmount
from src.classes.exceptions import AlgoTradeBotError, AlgofiLPNotFoundError, TinymanLPNotFoundError

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


def get_liquidity_pools(amm_clients: Dict[str, Any], asset1_id: int, asset2_id: int) -> Dict[str, Any]:
    """Get liquidity pool references for specified token pairs on supported Algorand DEXs.

    Parameters:
    amm_clients (Dict[str, Any]): Dictionary mapping of the AMM Clients to retrieve LPs for
    asset1_id (int): Asset 1 on-chain ID
    asset2_id (int): Asset 2 on-chain ID

    Returns:
    Dict[str, Any]: A dictionary mapping of the LPs, where the indexes are the names of
                    the DEXs, i.e. algofi, tinyman, pactfi.
    """
    pools: Dict[str, Any] = {}

    # Note: the ALGO asset has ID 0 on Tinyman's and Pact's DEXs but on Algofi it has the ID 1.
    # We will allow our bots to be configured with either of these two IDs for ALGO so
    # we need to support that here.

    if "algofi" in amm_clients:
        if asset1_id == 0:
            asset1_id = 1

        if asset2_id == 0:
            asset2_id = 1

        # Update: Algofi recently added NanoSwap pools for stable asset pairs. So let's
        # include support for that.
        pool_type = PoolType.NANOSWAP if is_algofi_nanoswap_stable_asset_pair(
            asset1_id, asset2_id) else PoolType.CONSTANT_PRODUCT_25BP_FEE

        try:
            algofi_pool = amm_clients["algofi"].get_pool(
                pool_type, asset1_id, asset2_id)

            if algofi_pool.pool_status == PoolStatus.UNINITIALIZED:
                raise AlgofiLPNotFoundError(asset1_id, asset2_id)
            else:
                pools["algofi"] = algofi_pool
        except AttributeError as e:
            raise AlgofiLPNotFoundError(asset1_id, asset2_id)

    if "tinyman" in amm_clients:
        if asset1_id == 1:
            asset1_id = 0

        if asset2_id == 1:
            asset2_id = 0

        try:
            asset1 = amm_clients["tinyman"].fetch_asset(asset1_id)
            asset2 = amm_clients["tinyman"].fetch_asset(asset2_id)
            tinyman_pool = amm_clients["tinyman"].fetch_pool(asset1, asset2)
        except Exception as e:
            raise TinymanLPNotFoundError(asset1_id, asset2_id)

        if not tinyman_pool.exists:
            raise TinymanLPNotFoundError(asset1_id, asset2_id)
        else:
            pools["tinyman"] = tinyman_pool

    return pools


def get_swap_quotes(amm_clients: Dict[str, Any], pools: Dict[str, Any], from_asset: AlgoAsset, to_asset: AlgoAsset, asset_in_amt: Decimal, slippage: float):
    """Get quotes from all supported Algorand DEXs for performing a swap.

    Parameters:
    amm_clients (Dict[str, Any]): Dictionary mapping of AMM Clients for each supported DEX
    pools (Dict[str, Any]): Dictionary mapping of the LPs for each supported DEX
    from_asset (AssetDetail): AssetDetail reference for the asset we want to swap in
    to_asset (AssetDetail): AssetDetail reference for the asset we want to swap out
    asset_in_amt (Decimal): The amount of `from_asset` that we want to trade in
    slippage (float): The slippage configured (i.e. 0.01 for 1% slippage)

    Returns:
    Dict[str, Any]: A dictionary mapping of the swap quotes, where the indexes are the names of
                    the DEXs, i.e. algofi, tinyman and pactfi.
    """
    quotes: Dict[str, Any] = {}
    asset_in_amt_scaled = from_asset.get_scaled_amount(asset_in_amt)
    from_decimals = from_asset.decimals
    to_decimals = to_asset.decimals

    if "algofi" in pools:
        from_asset_id = 1 if from_asset.asset_onchain_id == 0 else from_asset.asset_onchain_id
        quote_algofi = pools["algofi"].get_swap_exact_for_quote(
            from_asset_id, asset_in_amt_scaled)

        if from_asset_id == pools["algofi"].asset1.asset_id:
            amount_out = to_asset.get_unscaled_from_scaled_amount(
                quote_algofi.asset2_delta)
        else:
            amount_out = to_asset.get_unscaled_from_scaled_amount(
                quote_algofi.asset1_delta)

        amount_out_with_slippage = amount_out * Decimal(1.0 - slippage)
        quotes["algofi"] = {'amount_in': asset_in_amt, 'amount_out': amount_out,
                            'amount_out_with_slippage': amount_out_with_slippage, 'slippage': slippage}

        print(
            f"Algofi quote: amount_in={from_asset.asset_code}('{asset_in_amt:.{from_decimals}f}'), "
            f"amount_out={to_asset.asset_code}('{amount_out:.{to_decimals}f}'), slippage={slippage})")
        print(
            f"Minimum that will be received: {amount_out_with_slippage:.{to_decimals}f} {to_asset.asset_code}\n")

    if "tinyman" in pools:
        from_asset_id = 0 if from_asset.asset_onchain_id == 1 else from_asset.asset_onchain_id
        asset_ref = amm_clients["tinyman"].fetch_asset(from_asset_id)
        quotes["tinyman"] = pools["tinyman"].fetch_fixed_input_swap_quote(
            asset_ref(asset_in_amt_scaled), slippage)
        print(f"Tinyman quote: {quotes['tinyman']}")
        print(
            f"{to_asset.asset_code} per {from_asset.asset_code}: {quotes['tinyman'].price:.{to_decimals}f}")
        print(
            f"{to_asset.asset_code} per {from_asset.asset_code} (worst case): {quotes['tinyman'].price_with_slippage:.{to_decimals}f}")

        amount_out_with_slippage = Decimal(
            asset_in_amt * Decimal(quotes['tinyman'].price_with_slippage))
        print(
            f"Minimum that will be received: {amount_out_with_slippage:.{to_decimals}f} {to_asset.asset_code}\n")

    return quotes


def get_highest_swap_amount_out(amm_clients: Dict[str, Any], dex_pools: Dict[str, Any], from_asset: AlgoAsset,
                                to_asset: AlgoAsset, asset_in_amt: Decimal, slippage: float) -> SwapAmount:
    quotes = get_swap_quotes(amm_clients, dex_pools,
                             from_asset, to_asset, asset_in_amt, slippage)
    tinyman_amount_out = to_asset.get_unscaled_from_scaled_amount(
        quotes["tinyman"].amount_out.amount)
    tinyman_amount_out_with_slippage = to_asset.get_unscaled_from_scaled_amount(
        quotes["tinyman"].amount_out_with_slippage.amount)

    if quotes["algofi"]["amount_out"] > tinyman_amount_out:
        higher_amt = quotes["algofi"]["amount_out"]
        higher_amt_with_slippage = quotes["algofi"]["amount_out_with_slippage"]
        winning_dex = "Algofi"
        quote = quotes["algofi"]
    else:
        higher_amt = tinyman_amount_out
        higher_amt_with_slippage = tinyman_amount_out_with_slippage
        winning_dex = "Tinyman"
        quote = quotes["tinyman"]

    return SwapAmount(to_asset=to_asset, from_asset=from_asset, quote=quote, amount_in=asset_in_amt, amount_out=higher_amt,
                      amount_out_with_slippage=higher_amt_with_slippage, dex=winning_dex, slippage=slippage)


def get_algofi_swap_amount_out_scaled(swap_result, amm_client, account: Account) -> int:
    # TODO: Please be sure to test this function thoroughly!
    amount = None
    response = amm_client.indexer.search_transactions_by_address(
        address=account.address, block=swap_result["confirmed-round"])

    try:
        print(json.dumps(swap_result, indent=4))
        print("")
        print(json.dumps(response, indent=4))

        for tx_details in response["transactions"]:
            if tx_details["tx-type"] == "appl" and tx_details["group"] == swap_result["txn"]["txn"]["grp"]:
                # Check through the inner txns for the one we want
                for txn in tx_details["inner-txns"]:
                    if txn["tx-type"] == "pay" and txn["sender"] == swap_result["txn"]["txn"]["arcv"]:
                        amount = txn['payment-transaction']['amount']
                        raise StopIteration
                    elif txn["tx-type"] == "axfer" and txn["asset-transfer-transaction"]["receiver"] == account.address:
                        amount = txn['asset-transfer-transaction']['amount']
                        raise StopIteration
    except StopIteration:
        pass

    return amount
