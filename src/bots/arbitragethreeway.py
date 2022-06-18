import os
import sys
import time
import traceback
from decimal import Decimal
from typing import Any, Dict
from json_environ import Environ
from algofi_amm.v0.asset import Asset
from src.classes.account import Account
from src.classes.asset import AlgoAsset, SwapAmount

from src.helpers import get_algofi_swap_amount_out_scaled, get_amm_clients, get_asset_details, \
    get_highest_swap_amount_out, get_liquidity_pools, get_network, is_algofi_nanoswap_stable_asset_pair

network = get_network()
file_path = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(file_path, f"../../env/env-{network}.json")
env = Environ(path=env_path)


def get_configured_assets() -> Dict[int, AlgoAsset]:
    asset1_id = env("arbitrage:threeway:assets:asset1_id")
    asset2_id = env("arbitrage:threeway:assets:asset2_id")
    asset3_id = env("arbitrage:threeway:assets:asset3_id")
    asset_ids = (int(asset1_id), int(asset2_id), int(asset3_id))

    try:
        assets = get_asset_details(asset_ids)
        if len(assets) == 3:
            return assets
        else:
            for asset_id in asset_ids:
                if asset_id not in assets:
                    print(
                        f"Sorry, the configured asset ID '{asset_id}' is not supported by this trading bot.")
                    sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)


def submit_swap(amm_clients: Dict[str, Any], lps: Dict[str, Any], account: Account, details: SwapAmount, retry_with_new_quote: bool = False):
    leave_loop = False
    network = get_network()
    swap_to_carry_out: SwapAmount = details
    swap_carried_out: SwapAmount = None
    slippage = swap_to_carry_out.slippage

    while leave_loop is False:
        dex = swap_to_carry_out.dex.lower()
        try:
            if dex == "algofi":
                amount_out_with_slippage = swap_to_carry_out.quote["amount_out_with_slippage"]
                from_asset_id = 1 if swap_to_carry_out.from_asset.asset_onchain_id == 0 else swap_to_carry_out.from_asset.asset_onchain_id
                swap_input_asset = Asset(amm_clients["algofi"], from_asset_id)
                swap_asset_scaled_amount = swap_input_asset.get_scaled_amount(
                    swap_to_carry_out.amount_in)

                to_asset_id = 1 if swap_to_carry_out.to_asset.asset_onchain_id == 0 else swap_to_carry_out.to_asset.asset_onchain_id
                asset_out = Asset(amm_clients["algofi"], to_asset_id)
                min_scaled_amount_to_receive = asset_out.get_scaled_amount(
                    amount_out_with_slippage)

                if is_algofi_nanoswap_stable_asset_pair(from_asset_id, to_asset_id):
                    swap_exact_for_txn = lps["algofi"].get_swap_exact_for_txns(
                        account.address, swap_input_asset, swap_asset_scaled_amount, min_amount_to_receive=min_scaled_amount_to_receive, fee=5000)
                else:
                    swap_exact_for_txn = lps["algofi"].get_swap_exact_for_txns(
                        account.address, swap_input_asset, swap_asset_scaled_amount, min_amount_to_receive=min_scaled_amount_to_receive)

                swap_exact_for_txn.sign_with_private_key(
                    account.address, account.private_key)
                result = swap_exact_for_txn.submit(
                    amm_clients["algofi"].algod, wait=True)

                amt = get_algofi_swap_amount_out_scaled(
                    result, amm_clients["algofi"], account)
                if amt is not None:
                    amount_out = swap_to_carry_out.to_asset.get_unscaled_from_scaled_amount(
                        amt)
                else:
                    amount_out = amount_out_with_slippage

                swap_carried_out = SwapAmount(dex=dex, to_asset=swap_to_carry_out.to_asset, from_asset=swap_to_carry_out.from_asset,
                                              amount_in=swap_to_carry_out.amount_in, amount_out=amount_out, slippage=slippage,
                                              amount_out_with_slippage=amount_out_with_slippage, quote=swap_to_carry_out.quote)
            else:
                amount_out_with_slippage = swap_to_carry_out.quote.amount_out_with_slippage.amount
                amount_out = amount_out_with_slippage
                transaction_group = lps["tinyman"].prepare_swap_transactions_from_quote(
                    swap_to_carry_out.quote)
                transaction_group.sign_with_private_key(
                    account.address, account.private_key)

                # Submit transactions to the network and wait for confirmation.
                amm_clients["tinyman"].submit(transaction_group, wait=True)

                # Check if any excess remains after the swap (it's a Tinyman thing).
                to_asset_id = 0 if swap_to_carry_out.to_asset.asset_onchain_id == 1 else swap_to_carry_out.to_asset.asset_onchain_id
                tinyman_asset = amm_clients["tinyman"].fetch_asset(to_asset_id)
                excess = lps["tinyman"].fetch_excess_amounts()

                if tinyman_asset in excess:
                    amount = excess[tinyman_asset]
                    amount_out += amount.amount
                    print(f'Excess: {amount}')

                    transaction_group = lps["tinyman"].prepare_redeem_transactions(
                        amount)
                    transaction_group.sign_with_private_key(
                        account.address, account.private_key)
                    amm_clients["tinyman"].submit(transaction_group, wait=True)

                amount_out = swap_to_carry_out.to_asset.get_unscaled_from_scaled_amount(
                    amount_out)
                amount_out_with_slippage = swap_to_carry_out.to_asset.get_unscaled_from_scaled_amount(
                    amount_out_with_slippage)

                swap_carried_out = SwapAmount(dex=dex, to_asset=swap_to_carry_out.to_asset, from_asset=swap_to_carry_out.from_asset,
                                              amount_in=swap_to_carry_out.amount_in, amount_out=amount_out, slippage=slippage,
                                              amount_out_with_slippage=amount_out_with_slippage, quote=swap_to_carry_out.quote)

            leave_loop = True
        except Exception as e:
            if retry_with_new_quote:
                lps = get_liquidity_pools(
                    amm_clients, swap_to_carry_out.from_asset.asset_onchain_id, swap_to_carry_out.to_asset.asset_onchain_id)
                swap_to_carry_out = get_highest_swap_amount_out(amm_clients, lps, swap_to_carry_out.from_asset,
                                                                swap_to_carry_out.to_asset, swap_to_carry_out.amount_in, slippage)
            else:
                leave_loop = True

    return swap_carried_out


def do_round_trip_helper(account: Account, assets: Dict[int, AlgoAsset], amm_clients: Dict[str, Any],
                         trade_amt: Decimal, a1: int, a2: int, a3: int, price_action_enabled: bool) -> bool:
    asset_ids = [key for key in assets.keys()]
    slippage = float(env("arbitrage:threeway:amounts:slippage"))
    amount_in = trade_amt
    min_profit = Decimal(env("arbitrage:threeway:amounts:min_profit"))
    from_decimals = assets[asset_ids[a1]].decimals
    from_asset_code = assets[asset_ids[a1]].asset_code
    to_decimals = assets[asset_ids[a2]].decimals
    to_asset_code = assets[asset_ids[a2]].asset_code

    print("Fetching liquidity pools (LPs) for each token pair that's possible with the configured assets...")
    swap1_pools = get_liquidity_pools(
        amm_clients, asset_ids[a1], asset_ids[a2])
    swap2_pools = get_liquidity_pools(
        amm_clients, asset_ids[a2], asset_ids[a3])
    swap3_pools = get_liquidity_pools(
        amm_clients, asset_ids[a1], asset_ids[a3])
    print("LPs fetched successfully.")

    print(
        f"Getting swap quote from DEXs for {amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}\n")

    swap_amount_1 = get_highest_swap_amount_out(
        amm_clients, swap1_pools, assets[asset_ids[a1]], assets[asset_ids[a2]], amount_in, slippage)

    print(f"Higher swap amount quoted at the {swap_amount_1.dex} DEX at {swap_amount_1.amount_out:.{to_decimals}f} "
          f"({swap_amount_1.amount_out_with_slippage:.{to_decimals}f} with slippage) {to_asset_code} for {amount_in:.{from_decimals}f} {from_asset_code}.\n")

    amount_in = swap_amount_1.amount_out
    from_decimals = to_decimals
    from_asset_code = to_asset_code
    to_decimals = assets[asset_ids[a3]].decimals
    to_asset_code = assets[asset_ids[a3]].asset_code

    print(
        f"Getting swap quote from DEXs for {amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}\n")

    swap_amount_2 = get_highest_swap_amount_out(
        amm_clients, swap2_pools, assets[asset_ids[a2]], assets[asset_ids[a3]], amount_in, slippage)

    print(f"Higher swap amount quoted at the {swap_amount_2.dex} DEX at {swap_amount_2.amount_out:.{to_decimals}f} "
          f"({swap_amount_2.amount_out_with_slippage:.{to_decimals}f} with slippage) {to_asset_code} for {amount_in:.{from_decimals}f} {from_asset_code}.\n")

    amount_in = swap_amount_2.amount_out
    from_decimals = to_decimals
    from_asset_code = to_asset_code
    to_decimals = assets[asset_ids[a1]].decimals
    to_asset_code = assets[asset_ids[a1]].asset_code

    print(
        f"Getting swap quote from DEXs for {amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}\n")

    swap_amount_3 = get_highest_swap_amount_out(
        amm_clients, swap3_pools, assets[asset_ids[a3]], assets[asset_ids[a1]], amount_in, slippage)

    print(f"Higher swap amount quoted at the {swap_amount_3.dex} DEX at {swap_amount_3.amount_out:.{to_decimals}f} "
          f"({swap_amount_3.amount_out_with_slippage:.{to_decimals}f} with slippage) {to_asset_code} for {amount_in:.{from_decimals}f} {from_asset_code}.\n")

    if swap_amount_3.amount_out >= (trade_amt + min_profit):
        print(f"Arbitrage condition met. Submitting transactions...")
        print(f"Performing first swap via the {swap_amount_1.dex} DEX for "
              f"{swap_amount_1.amount_in:.{swap_amount_1.from_asset.decimals}f} {swap_amount_1.from_asset.asset_code} "
              f"to {swap_amount_1.to_asset.asset_code}")
        swap_carried_out = submit_swap(
            amm_clients, swap1_pools, account, swap_amount_1, False)

        if swap_carried_out is None:
            print(
                "Encountered too much slippage or account balance insufficient to perform swap. Moving on...\n")
            return False
        else:
            print("")
            swap_to_carry_out = get_highest_swap_amount_out(
                amm_clients, swap2_pools, assets[asset_ids[a2]], assets[asset_ids[a3]], swap_carried_out.amount_out, slippage)
            from_decimals = swap_to_carry_out.from_asset.decimals
            from_asset_code = swap_to_carry_out.from_asset.asset_code
            to_asset_code = swap_to_carry_out.to_asset.asset_code

            print(f"Performing second swap via the {swap_to_carry_out.dex} DEX for "
                  f"{swap_to_carry_out.amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}")

            swap_carried_out = submit_swap(
                amm_clients, swap2_pools, account, swap_to_carry_out, True)

            if swap_carried_out is None:
                print("Unable to perform swap. Terminating bot.\n")
                sys.exit(1)
            else:
                swap_to_carry_out = get_highest_swap_amount_out(
                    amm_clients, swap3_pools, assets[asset_ids[a3]], assets[asset_ids[a1]], swap_carried_out.amount_out, slippage)
                from_decimals = swap_to_carry_out.from_asset.decimals
                from_asset_code = swap_to_carry_out.from_asset.asset_code
                to_asset_code = swap_to_carry_out.to_asset.asset_code

                print(f"Performing third swap via the {swap_to_carry_out.dex} DEX for "
                      f"{swap_to_carry_out.amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}")

                swap_carried_out = submit_swap(
                    amm_clients, swap3_pools, account, swap_to_carry_out, True)

                if swap_carried_out is None:
                    print("Unable to perform swap. Terminating bot.\n")
                    sys.exit(1)

            return True
    else:
        print(f"Arbitrage condition not yet met. Stir and repeat...\n")
        return False


def do_round_trip(account: Account, assets: Dict[int, AlgoAsset], amm_clients: Dict[str, Any], trade_amt: Decimal, price_action_enabled: bool):
    # First round trip is Asset 1 -> Asset 2, Asset 2 -> Asset 3, Asset 3 -> Asset 1
    # If this results in a profit made on Asset 1 then we're good, otherwise let's try the
    # alternative round trip, which is Asset 1 -> Asset 3, Asset 3 -> Asset 2, Asset 2 -> Asset 1.
    arbitrage_fullfilled = do_round_trip_helper(
        account, assets, amm_clients, trade_amt, 0, 1, 2, price_action_enabled)
    if not arbitrage_fullfilled:
        do_round_trip_helper(account, assets, amm_clients,
                             trade_amt, 0, 2, 1, price_action_enabled)


def run_bot():
    account = Account(env("arbitrage:threeway:account:mnemonic"))

    print(
        f"Initializing Arbitrage three-token bot on Algorand {network}...\nChecking for configured assets...")
    assets = get_configured_assets()
    asset_codes = [value.asset_code for value in assets.values()]
    print(f"Configured assets are: {', '.join(asset_codes)}")

    print("Instantiating AMM Clients for each supported Algorand DEX...")
    amm_clients = get_amm_clients(account)

    print("Initialization completed. Starting round trip checks for arbitrage...\n")
    enable_price_action = env("arbitrage:threeway:enable_price_action_variant")
    trade_amt = Decimal(env("arbitrage:threeway:amounts:starting_amt"))

    while (True):
        try:
            do_round_trip(account, assets, amm_clients,
                          trade_amt, enable_price_action)
            print(
                "--------------------------------------------------------------------------------\n")
            time.sleep(2)
        except Exception as e:
            traceback.print_exc()
