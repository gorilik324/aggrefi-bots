import os
import sys
import json
import pactsdk
from decimal import Decimal
from typing import Any, Dict, Tuple
from json_environ import Environ
from pymongo import MongoClient
from algofi_amm.v0.client import AlgofiAMMMainnetClient, AlgofiAMMTestnetClient
from algofi_amm.v0.config import PoolType, PoolStatus
from tinyman.v1.client import TinymanMainnetClient, TinymanTestnetClient
from algosdk.v2client import algod, indexer

from src.classes.account import Account
from src.classes.asset import AlgoAsset, SwapAmount
from src.classes.exceptions import AlgoTradeBotError, AlgofiLPNotFoundError, PactLPNotFoundError, \
    SupportedAssetsLookupError, TinymanLPNotFoundError

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


def get_supported_algo_assets(index_with_onchain_id: bool = False):
    """Returns a dictionary with details of the Algorand assets that can be traded with our bots."""
    assets: Dict[int | str, AlgoAsset] = {}
    client = get_db_client()
    db = client.aggrefidb

    cursor = db.assets.find({'is_active': True})
    for doc in cursor:
        assets[doc["asset_onchain_id"] if index_with_onchain_id else str(doc["_id"])] = AlgoAsset(
            id=str(doc["_id"]),
            asset_name=doc["asset_name"],
            asset_code=doc["asset_code"],
            asset_onchain_id=doc["asset_onchain_id"],
            decimals=doc["decimals"],
            is_native=doc["is_native"],
            is_active=doc["is_active"]
        )

    return assets


def get_asset_details(asset_ids: Tuple[int, ...]):
    """Get details of a tuple of specified assets."""
    supported_assets_dict = get_supported_algo_assets(
        index_with_onchain_id=True)

    if supported_assets_dict is None:
        raise SupportedAssetsLookupError(
            'Unable to retrieve information on supported assets')
    else:
        assets: Dict[int, AlgoAsset] = {}
        for asset_id in asset_ids:
            assets[asset_id] = supported_assets_dict[asset_id]

        return assets


def get_amm_clients(account: Account) -> Dict[str, AlgofiAMMTestnetClient | AlgofiAMMMainnetClient | TinymanTestnetClient | TinymanMainnetClient | pactsdk.PactClient]:
    """Get and return instances of the supported AMM Clients."""
    network = get_network()
    algod_token = env("algod:api_key")
    algod_address = env("algod:algod")
    indexer_address = env("algod:indexer")
    pact_api = env("pact_api")
    headers = {
        "X-API-Key": algod_token,
        "User-Agent": "algosdk"
    }

    algod_client = algod.AlgodClient(
        algod_token, algod_address, headers=headers)
    indexer_client = indexer.IndexerClient(
        "", indexer_address, headers=headers)

    if network == "testnet":
        algofi_client = AlgofiAMMTestnetClient(
            user_address=account.getAddress(), algod_client=algod_client, indexer_client=indexer_client)
        tinyman_client = TinymanTestnetClient(
            user_address=account.getAddress(), algod_client=algod_client)
    else:
        algofi_client = AlgofiAMMMainnetClient(
            user_address=account.getAddress(), algod_client=algod_client, indexer_client=indexer_client)
        tinyman_client = TinymanMainnetClient(
            user_address=account.getAddress(), algod_client=algod_client)

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


def get_liquidity_pools(amm_clients: Dict[str, Any], asset1_id: int, asset2_id: int, raise_error_on_missing_lp: bool = True) -> Dict[str, Any]:
    """Get liquidity pool references for specified asset pairs on supported Algorand DEXs.

    Parameters:
    amm_clients (Dict[str, Any]): Dictionary mapping of the AMM Clients to retrieve LPs for
    asset1_id (int): Asset 1 on-chain ID
    asset2_id (int): Asset 2 on-chain ID
    raise_error_on_missing_lp (bool): Should we raise an error when an LP for the asset pair is not found on any of the DEXs? Defaults to True.

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
                if raise_error_on_missing_lp:
                    raise AlgofiLPNotFoundError(asset1_id, asset2_id)
            else:
                pools["algofi"] = algofi_pool
        except Exception as e:
            if raise_error_on_missing_lp:
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
            if raise_error_on_missing_lp:
                raise TinymanLPNotFoundError(asset1_id, asset2_id)

        if not tinyman_pool.exists:
            if raise_error_on_missing_lp:
                raise TinymanLPNotFoundError(asset1_id, asset2_id)
        else:
            pools["tinyman"] = tinyman_pool

    if "pactfi" in amm_clients:
        if asset1_id == 1:
            asset1_id = 0

        if asset2_id == 1:
            asset2_id = 0

        try:
            asset1 = amm_clients["pactfi"].fetch_asset(asset1_id)
            asset2 = amm_clients["pactfi"].fetch_asset(asset2_id)
            pact_pools = amm_clients["pactfi"].fetch_pools_by_assets(
                asset1, asset2)
            pools["pactfi"] = pact_pools[0]
        except Exception as e:
            if raise_error_on_missing_lp:
                raise PactLPNotFoundError(asset1_id, asset2_id)

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

    # Pact's pool state needs refreshing periodically.
    pools["pactfi"].update_state()

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

    if "pactfi" in pools:
        from_asset_id = 0 if from_asset.asset_onchain_id == 1 else from_asset.asset_onchain_id
        asset_ref = amm_clients["pactfi"].fetch_asset(from_asset_id)
        slippage_pct = slippage * float(10**2)

        swap = pools["pactfi"].prepare_swap(
            asset=asset_ref,
            amount=asset_in_amt_scaled,
            slippage_pct=slippage_pct
        )

        swap_effect = swap.effect
        amount_out = to_asset.get_unscaled_from_scaled_amount(
            swap_effect.amount_received)
        amount_out_with_slippage = to_asset.get_unscaled_from_scaled_amount(
            swap_effect.minimum_amount_received)
        quotes["pactfi"] = {'amount_in': asset_in_amt, 'amount_out': amount_out,
                            'amount_out_with_slippage': amount_out_with_slippage,
                            'slippage': slippage, 'prepared_swap': swap}

        print(
            f"Pact quote: amount_in={from_asset.asset_code}('{asset_in_amt:.{from_decimals}f}'), "
            f"amount_out={to_asset.asset_code}('{amount_out:.{to_decimals}f}'), slippage={slippage})")
        print(
            f"Minimum that will be received: {amount_out_with_slippage:.{to_decimals}f} {to_asset.asset_code}\n")

    return quotes


def get_highest_swap_amount_out(amm_clients: Dict[str, Any], dex_pools: Dict[str, Any], from_asset: AlgoAsset,
                                to_asset: AlgoAsset, asset_in_amt: Decimal, slippage: float) -> SwapAmount:
    quotes = get_swap_quotes(amm_clients, dex_pools,
                             from_asset, to_asset, asset_in_amt, slippage)

    if "tinyman" in quotes:
        tinyman_amount_out = to_asset.get_unscaled_from_scaled_amount(
            quotes["tinyman"].amount_out.amount)
        tinyman_amount_out_with_slippage = to_asset.get_unscaled_from_scaled_amount(
            quotes["tinyman"].amount_out_with_slippage.amount)

    # Check if all supported pools are available.
    if "tinyman" in quotes and "algofi" in quotes and "pactfi" in quotes:
        if quotes["algofi"]["amount_out"] >= tinyman_amount_out and quotes["algofi"]["amount_out"] >= quotes["pactfi"]["amount_out"]:
            higher_amt = quotes["algofi"]["amount_out"]
            higher_amt_with_slippage = quotes["algofi"]["amount_out_with_slippage"]
            winning_dex = "Algofi"
            quote = quotes["algofi"]
        elif quotes["pactfi"]["amount_out"] >= tinyman_amount_out and quotes["pactfi"]["amount_out"] >= quotes["algofi"]["amount_out"]:
            higher_amt = quotes["pactfi"]["amount_out"]
            higher_amt_with_slippage = quotes["pactfi"]["amount_out_with_slippage"]
            winning_dex = "Pact"
            quote = quotes["pactfi"]
        else:
            higher_amt = tinyman_amount_out
            higher_amt_with_slippage = tinyman_amount_out_with_slippage
            winning_dex = "Tinyman"
            quote = quotes["tinyman"]

    # Check if Tinyman and Algofi pools available, but not Pact.
    if "tinyman" in quotes and "algofi" in quotes and "pactfi" not in quotes:
        if quotes["algofi"]["amount_out"] >= tinyman_amount_out:
            higher_amt = quotes["algofi"]["amount_out"]
            higher_amt_with_slippage = quotes["algofi"]["amount_out_with_slippage"]
            winning_dex = "Algofi"
            quote = quotes["algofi"]
        else:
            higher_amt = tinyman_amount_out
            higher_amt_with_slippage = tinyman_amount_out_with_slippage
            winning_dex = "Tinyman"
            quote = quotes["tinyman"]

    # Check if Tinyman and Pact pools available, but not Algofi.
    if "tinyman" in quotes and "pactfi" in quotes and "algofi" not in quotes:
        if quotes["pactfi"]["amount_out"] >= tinyman_amount_out:
            higher_amt = quotes["pactfi"]["amount_out"]
            higher_amt_with_slippage = quotes["pactfi"]["amount_out_with_slippage"]
            winning_dex = "Pact"
            quote = quotes["pactfi"]
        else:
            higher_amt = tinyman_amount_out
            higher_amt_with_slippage = tinyman_amount_out_with_slippage
            winning_dex = "Tinyman"
            quote = quotes["tinyman"]

    # Check if Algofi and Pact pools available, but not Tinyman.
    if "algofi" in quotes and "pactfi" in quotes and "tinyman" not in quotes:
        if quotes["algofi"]["amount_out"] >= quotes["pactfi"]["amount_out"]:
            higher_amt = quotes["algofi"]["amount_out"]
            higher_amt_with_slippage = quotes["algofi"]["amount_out_with_slippage"]
            winning_dex = "Algofi"
            quote = quotes["algofi"]
        else:
            higher_amt = quotes["pactfi"]["amount_out"]
            higher_amt_with_slippage = quotes["pactfi"]["amount_out_with_slippage"]
            winning_dex = "Pact"
            quote = quotes["pactfi"]

    # Check if only Tinyman pool available.
    if "tinyman" in quotes and "algofi" not in quotes and "pactfi" not in quotes:
        higher_amt = tinyman_amount_out
        higher_amt_with_slippage = tinyman_amount_out_with_slippage
        winning_dex = "Tinyman"
        quote = quotes["tinyman"]

    # Check if only Algofi pool available.
    if "algofi" in quotes and "tinyman" not in quotes and "pactfi" not in quotes:
        higher_amt = quotes["algofi"]["amount_out"]
        higher_amt_with_slippage = quotes["algofi"]["amount_out_with_slippage"]
        winning_dex = "Algofi"
        quote = quotes["algofi"]

    # Check if only Pact pool available.
    if "pactfi" in quotes and "algofi" not in quotes and "tinyman" not in quotes:
        higher_amt = quotes["pactfi"]["amount_out"]
        higher_amt_with_slippage = quotes["pactfi"]["amount_out_with_slippage"]
        winning_dex = "Pact"
        quote = quotes["pactfi"]

    return SwapAmount(to_asset=to_asset, from_asset=from_asset, quote=quote, amount_in=asset_in_amt, amount_out=higher_amt,
                      amount_out_with_slippage=higher_amt_with_slippage, dex=winning_dex, slippage=slippage)


def get_algofi_swap_amount_out_scaled(swap_result, amm_client, account: Account) -> int:
    # TODO: Please be sure to test this function thoroughly!
    amount = None
    response = amm_client.indexer.search_transactions_by_address(
        address=account.getAddress(), block=swap_result["confirmed-round"])

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
                    elif txn["tx-type"] == "axfer" and txn["asset-transfer-transaction"]["receiver"] == account.getAddress():
                        amount = txn['asset-transfer-transaction']['amount']
                        raise StopIteration
    except StopIteration:
        pass

    return amount


def get_pact_swap_amount_out_scaled(tx_id: str, indexer_client: indexer.IndexerClient, account: Account) -> int:
    # TODO: Please be sure to test this function thoroughly as well!
    amount = None
    tx_response = indexer_client.search_transactions_by_address(
        address=account.getAddress(), txid=tx_id)
    block_response = indexer_client.search_transactions_by_address(
        address=account.getAddress(), block=tx_response["transactions"][0]["confirmed-round"])

    try:
        print(json.dumps(block_response, indent=4))

        for tx_details in block_response["transactions"]:
            if tx_details["tx-type"] == "appl" and tx_details["group"] == tx_response["transactions"][0]["group"]:
                # Check through the inner txns for the one we want
                for txn in tx_details["inner-txns"]:
                    if txn["tx-type"] == "pay" and txn["sender"] == tx_response["transactions"][0]["asset-transfer-transaction"]["receiver"]:
                        amount = txn['payment-transaction']['amount']
                        raise StopIteration
                    elif txn["tx-type"] == "axfer" and txn["asset-transfer-transaction"]["receiver"] == account.getAddress():
                        amount = txn['asset-transfer-transaction']['amount']
                        raise StopIteration
    except StopIteration:
        pass

    return amount


def get_asa_balance(address: str, asset_id: int, algod_client: algod.AlgodClient) -> int:
    account_info = algod_client.account_info(address)
    assets = account_info.get("assets")
    balance = 0

    for asset in assets:
        if asset.get("asset-id") == asset_id:
            balance = asset.get("amount")
            break

    return balance
