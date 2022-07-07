import os
import sys
import time
import traceback
from decimal import Decimal
from typing import Any, Dict
from json_environ import Environ
from algosdk.future import transaction
from algofi_amm.v0.asset import Asset
from src.classes.account import Account
from src.classes.asset import AlgoAsset, SwapAmount
from src.classes.exceptions import AlgoTradeBotError

from src.helpers import get_algofi_swap_amount_out_scaled, get_amm_clients, get_asa_balance, get_asset_details, \
    get_highest_swap_amount_out, get_liquidity_pools, get_network, get_pact_swap_amount_out_scaled, \
    is_algofi_nanoswap_stable_asset_pair

network = get_network()
file_path = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(file_path, f"../../env/env-{network}.json")
env = Environ(path=env_path)


def get_configured_assets() -> Dict[int, AlgoAsset]:
    asset1_id = env("arbitrage:twoway:assets:asset1_id")
    asset2_id = env("arbitrage:twoway:assets:asset2_id")
    asset_ids = (int(asset1_id), int(asset2_id))

    try:
        assets = get_asset_details(asset_ids)
        if len(assets) == 2:
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
            elif dex == "pact":
                amount_out_with_slippage = swap_to_carry_out.quote["amount_out_with_slippage"]

                swap_tx_group = swap_to_carry_out.quote["prepared_swap"].prepare_tx_group(
                    account.getAddress())
                signed_group = swap_tx_group.sign(account.getPrivateKey())
                tx_id = amm_clients["pactfi"].algod.send_transactions(
                    signed_group)

                # wait for confirmation
                try:
                    transaction.wait_for_confirmation(
                        amm_clients["pactfi"].algod, tx_id, 10)
                except Exception as err:
                    print(err)
                    sys.exit(1)

                # Note: We get our indexer.IndexerClient instance from our Algofi client instance because the Pact client
                # does not store an indexer client instance that we can use.
                amt = get_pact_swap_amount_out_scaled(
                    tx_id, amm_clients["algofi"].indexer, account)
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
            print(f"Error: {e}")
            if retry_with_new_quote:
                lps = get_liquidity_pools(
                    amm_clients, swap_to_carry_out.from_asset.asset_onchain_id, swap_to_carry_out.to_asset.asset_onchain_id)
                swap_to_carry_out = get_highest_swap_amount_out(amm_clients, lps, swap_to_carry_out.from_asset,
                                                                swap_to_carry_out.to_asset, swap_to_carry_out.amount_in, slippage)
            else:
                leave_loop = True

    return swap_carried_out


def do_round_trip(account: Account, assets: Dict[int, AlgoAsset], amm_clients: Dict[str, Any], trade_amt: Decimal):
    # Round trip is Asset 1 -> Asset 2, Asset 2 -> Asset 1.
    asset_ids = [key for key in assets.keys()]
    slippage = float(env("arbitrage:twoway:amounts:slippage"))
    amount_in = trade_amt
    min_profit = Decimal(env("arbitrage:twoway:amounts:min_profit"))
    from_decimals = assets[asset_ids[0]].decimals
    from_asset_code = assets[asset_ids[0]].asset_code
    to_decimals = assets[asset_ids[1]].decimals
    to_asset_code = assets[asset_ids[1]].asset_code

    print(
        f"Fetching liquidity pools (LP) for the {from_asset_code}/{to_asset_code} token pair...")
    lps = get_liquidity_pools(amm_clients, asset_ids[0], asset_ids[1])
    print("LPs fetched successfully.")

    print(
        f"Getting highest swap quote from DEXs for {amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}\n")

    swap_amount_1 = get_highest_swap_amount_out(
        amm_clients, lps, assets[asset_ids[0]], assets[asset_ids[1]], amount_in, slippage)

    print(f"Highest swap amount quoted at the {swap_amount_1.dex} DEX at {swap_amount_1.amount_out:.{to_decimals}f} "
          f"({swap_amount_1.amount_out_with_slippage:.{to_decimals}f} with slippage) {to_asset_code} for {amount_in:.{from_decimals}f} {from_asset_code}.\n")

    amount_in = swap_amount_1.amount_out
    from_decimals = assets[asset_ids[1]].decimals
    from_asset_code = assets[asset_ids[1]].asset_code
    to_decimals = assets[asset_ids[0]].decimals
    to_asset_code = assets[asset_ids[0]].asset_code

    print(
        f"Getting highest swap quote from DEXs for {amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}\n")

    swap_amount_2 = get_highest_swap_amount_out(
        amm_clients, lps, assets[asset_ids[1]], assets[asset_ids[0]], amount_in, slippage)

    print(f"Highest swap amount quoted at the {swap_amount_2.dex} DEX at {swap_amount_2.amount_out:.{to_decimals}f} "
          f"({swap_amount_2.amount_out_with_slippage:.{to_decimals}f} with slippage) {to_asset_code} for {amount_in:.{from_decimals}f} {from_asset_code}.\n")

    if swap_amount_2.amount_out >= (trade_amt + min_profit):
        print(f"Arbitrage condition met. Submitting transactions...")
        print(f"Performing first swap via the {swap_amount_1.dex} DEX for "
              f"{swap_amount_1.amount_in:.{swap_amount_1.from_asset.decimals}f} {swap_amount_1.from_asset.asset_code} "
              f"to {swap_amount_1.to_asset.asset_code}")

        swap_carried_out = submit_swap(
            amm_clients, lps, account, swap_amount_1, False)

        if swap_carried_out is None:
            print(
                "Encountered too much slippage or account balance insufficient to perform swap. Moving on...\n")
        else:
            print("")
            swap_to_carry_out = get_highest_swap_amount_out(
                amm_clients, lps, assets[asset_ids[1]], assets[asset_ids[0]], swap_carried_out.amount_out, slippage)
            from_decimals = swap_to_carry_out.from_asset.decimals
            from_asset_code = swap_to_carry_out.from_asset.asset_code
            to_asset_code = swap_to_carry_out.to_asset.asset_code

            print(f"Performing second swap via the {swap_to_carry_out.dex} DEX for "
                  f"{swap_to_carry_out.amount_in:.{from_decimals}f} {from_asset_code} to {to_asset_code}")

            swap_carried_out = submit_swap(
                amm_clients, lps, account, swap_to_carry_out, True)

            if swap_carried_out is None:
                print("Unable to perform swap. Terminating bot.\n")
                sys.exit(1)
    else:
        print(f"Arbitrage condition not yet met. Stir and repeat...\n")


def run_bot():
    account = Account(env("arbitrage:twoway:account:mnemonic"))
    asset1_id = env("arbitrage:twoway:assets:asset1_id")

    print(
        f"Initializing Arbitrage two-token bot on Algorand {network}...\nChecking for configured assets...")
    assets = get_configured_assets()
    asset_codes = [value.asset_code for value in assets.values()]
    print(f"Configured assets are: {', '.join(asset_codes)}")

    print("Instantiating AMM Clients for each supported Algorand DEX...")
    amm_clients = get_amm_clients(account)

    print("Initialization completed. Starting round trip checks for arbitrage...\n")
    trade_amt = env("arbitrage:twoway:amounts:starting_amt")
    trading_all = False

    if trade_amt == "all":
        # Let's only allow the user to trade all the asset if it's an ASA.
        if asset_codes[0].lower() == "algo":
            error_msg = ' '.join((
                "The \"all\" configuration value for the 'arbitrage.twoway.amounts.starting_amt'",
                "environment variable is only allowed to be set for ASAs."
            ))
            raise AlgoTradeBotError(error_msg)
        else:
            trading_all = True
            trade_amt = get_asa_balance(
                account.getAddress(), asset1_id, amm_clients["algofi"].algod)
            trade_amt = assets[asset1_id].get_unscaled_from_scaled_amount(
                trade_amt)
    else:
        trade_amt = Decimal(trade_amt)

    while (True):
        try:
            do_round_trip(account, assets, amm_clients, trade_amt)
            if trading_all:
                trade_amt = get_asa_balance(
                    account.getAddress(), asset1_id, amm_clients["algofi"].algod)
                trade_amt = assets[asset1_id].get_unscaled_from_scaled_amount(
                    trade_amt)

            print(
                "--------------------------------------------------------------------------------\n")
            time.sleep(1)
        except Exception as e:
            traceback.print_exc()
