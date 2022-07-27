import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from json_environ import Environ
from algosdk.future import transaction
from algofi_amm.v0.asset import Asset
from src.classes.account import Account

from src.helpers import get_algofi_swap_amount_out_scaled, get_amm_clients, get_db_client, \
    get_liquidity_pools, get_network, get_pact_swap_amount_out_scaled, get_supported_algo_assets, \
    get_swap_quotes, is_algofi_nanoswap_stable_asset_pair

network = get_network()
file_path = os.path.abspath(os.path.dirname(__file__))
env_path = os.path.join(file_path, f"../../env/env-{network}.json")
env = Environ(path=env_path)

# Get client connection to off-chain DB.
client = get_db_client()
if client is None:
    print("Abandoning this run of the Spot Trades (Order Book) bot since connection to the off-chain DB failed")
    sys.exit(1)


def run_bot():
    user_id = env("arbitrage:orderbook:user_id")
    account = Account(env("arbitrage:orderbook:account:mnemonic"))
    supported_assets = get_supported_algo_assets()
    amm_clients = get_amm_clients(account)
    db = client.aggrefidb

    if supported_assets is None:
        print("Unable to retrieve information on supported assets. Abandoning bot run.")
        sys.exit(1)

    # Declare a list that will be used to keep track of the trades that are completed
    # in this run.
    swaps_completed = []

    while (True):
        # Query for the swaps that need to be carried out.
        try:
            cursor = db.orderbook.find({
                'user_id': user_id,
                'is_active': True,
                'is_completed': False
            })
        except RuntimeError as e:
            print(f"Error attempting to query order book: {e}")
            sys.exit(1)

        # Loop through the swaps to be carried out.
        for doc in cursor:
            order_type = doc["order_type"]
            amt_to_buy_sell = doc["amt_to_buy_sell"]
            asset_in_id = doc["asset_to_buy_with_sell_to"] if doc["order_type"] == "buy" else doc["asset_to_buy_sell"]
            asset_out_id = doc["asset_to_buy_sell"] if doc["order_type"] == "buy" else doc["asset_to_buy_with_sell_to"]

            from_asset_decimals = supported_assets[asset_in_id].decimals
            from_asset_token_code = supported_assets[asset_in_id].asset_code

            to_asset_decimals = supported_assets[asset_out_id].decimals
            to_asset_token_code = supported_assets[asset_out_id].asset_code

            try:
                # Get the LPs
                lps = get_liquidity_pools(
                    amm_clients, supported_assets[asset_in_id].asset_onchain_id, supported_assets[asset_out_id].asset_onchain_id)

                # Get a quote for a swap of asset_in amt to asset_out with the configured slippage tolerance.
                quotes = get_swap_quotes(amm_clients, lps, supported_assets[asset_in_id], supported_assets[asset_out_id], Decimal(
                    amt_to_buy_sell), float(doc["slippage"]))
                tinyman_amount_out_with_slippage = supported_assets[asset_out_id].get_unscaled_from_scaled_amount(
                    quotes["tinyman"].amount_out_with_slippage.amount)

                #
                # If doc["min_amt_to_receive_per_unit"] is set then we want to buy/sell only if we can get
                # at least doc["min_amt_to_receive_per_unit"] of asset_out from the swap.
                #
                # If doc["max_amt_to_receive_per_unit"] is set then we want to buy/sell only if we will get
                # no more than doc["max_amt_to_receive_per_unit"] of asset_out from the swap
                #
                requirement_met = False
                if "min_amt_to_receive_per_unit" in doc:
                    more_or_less_wording = "at least"
                    buy_sell_amt = Decimal(
                        doc["min_amt_to_receive_per_unit"]) * Decimal(amt_to_buy_sell)
                    print(
                        f"Swap must produce {more_or_less_wording} {buy_sell_amt:.{to_asset_decimals}f} {to_asset_token_code} to meet {order_type} requirements.")

                    if tinyman_amount_out_with_slippage >= buy_sell_amt or quotes["algofi"]["amount_out_with_slippage"] >= buy_sell_amt or quotes["pactfi"]["amount_out_with_slippage"] >= buy_sell_amt:
                        requirement_met = True
                elif "max_amt_to_receive_per_unit" in doc:
                    more_or_less_wording = "no more than"
                    buy_sell_amt = Decimal(
                        doc["max_amt_to_receive_per_unit"]) * Decimal(amt_to_buy_sell)
                    print(
                        f"Swap must produce {more_or_less_wording} {buy_sell_amt:.{to_asset_decimals}f} {to_asset_token_code} to meet {order_type} requirements.")

                    if tinyman_amount_out_with_slippage <= buy_sell_amt or quotes["algofi"]["amount_out_with_slippage"] <= buy_sell_amt or quotes["pactfi"]["amount_out_with_slippage"] <= buy_sell_amt:
                        requirement_met = True
                else:
                    print("Order not configured properly. Skipping this order.")
                    continue

                if requirement_met:
                    # Let's figure out if we'll do the swap on Algofi, Tinyman or Pact
                    print(
                        f"Swap condition met. Deciding whether to do the swap via Algofi, Tinyman or Pact...")

                    if quotes["algofi"]["amount_out_with_slippage"] >= tinyman_amount_out_with_slippage and quotes["algofi"]["amount_out_with_slippage"] >= quotes["pactfi"]["amount_out_with_slippage"]:
                        print("Executing swap via the Algofi DEX...")

                        total_asset_out_received = quotes["algofi"]["amount_out_with_slippage"]
                        print(
                            f"Swapping {amt_to_buy_sell:.{from_asset_decimals}f} {from_asset_token_code} for {more_or_less_wording} {total_asset_out_received:.{to_asset_decimals}f} {to_asset_token_code}")

                        from_asset_id = 1 if supported_assets[
                            asset_in_id].asset_onchain_id == 0 else supported_assets[asset_in_id].asset_onchain_id
                        swap_input_asset = Asset(
                            amm_clients["algofi"], from_asset_id)
                        swap_asset_scaled_amount = swap_input_asset.get_scaled_amount(
                            amt_to_buy_sell)

                        to_asset_id = 1 if supported_assets[
                            asset_out_id].asset_onchain_id == 0 else supported_assets[asset_out_id].asset_onchain_id
                        asset_out = Asset(amm_clients["algofi"], to_asset_id)
                        min_scaled_amount_to_receive = asset_out.get_scaled_amount(
                            quotes["algofi"]["amount_out_with_slippage"])

                        if is_algofi_nanoswap_stable_asset_pair(from_asset_id, to_asset_id):
                            swap_exact_for_txn = lps["algofi"].get_swap_exact_for_txns(
                                account.getAddress(), swap_input_asset, swap_asset_scaled_amount, min_amount_to_receive=min_scaled_amount_to_receive, fee=5000)
                        else:
                            swap_exact_for_txn = lps["algofi"].get_swap_exact_for_txns(
                                account.getAddress(), swap_input_asset, swap_asset_scaled_amount, min_amount_to_receive=min_scaled_amount_to_receive)

                        swap_exact_for_txn.sign_with_private_key(
                            account.getAddress(), account.getPrivateKey())
                        result = swap_exact_for_txn.submit(
                            amm_clients["algofi"].algod, wait=True)

                        amt = get_algofi_swap_amount_out_scaled(
                            result, amm_clients["algofi"], account)
                        if amt is not None:
                            total_asset_out_received = supported_assets[asset_out_id].get_unscaled_from_scaled_amount(
                                amt)
                    elif quotes["pactfi"]["amount_out_with_slippage"] >= tinyman_amount_out_with_slippage and quotes["pactfi"]["amount_out_with_slippage"] >= quotes["algofi"]["amount_out_with_slippage"]:
                        print("Executing swap via the Pact DEX...")

                        total_asset_out_received = quotes["pactfi"]["amount_out_with_slippage"]
                        print(
                            f"Swapping {amt_to_buy_sell:.{from_asset_decimals}f} {from_asset_token_code} for {more_or_less_wording} {total_asset_out_received:.{to_asset_decimals}f} {to_asset_token_code}")

                        # Yo, let's do the swap!
                        swap_tx_group = quotes["pactfi"]["prepared_swap"].prepare_tx_group(
                            account.getAddress())
                        signed_group = swap_tx_group.sign(
                            account.getPrivateKey())
                        tx_id = amm_clients["pactfi"].algod.send_transactions(
                            signed_group)

                        # wait for confirmation
                        try:
                            transaction.wait_for_confirmation(
                                amm_clients["pactfi"].algod, tx_id)
                        except Exception as err:
                            print(err)
                            sys.exit(1)

                        # Note: We get our indexer.IndexerClient instance from our Algofi client instance because the Pact client
                        # does not store an indexer client instance that we can use.
                        amt = get_pact_swap_amount_out_scaled(
                            tx_id, amm_clients["algofi"].indexer, account)
                        if amt is not None:
                            total_asset_out_received = supported_assets[asset_out_id].get_unscaled_from_scaled_amount(
                                amt)
                    else:
                        print("Executing swap via the Tinyman DEX...")

                        total_asset_out_received = quotes["tinyman"].amount_out_with_slippage.amount
                        print(
                            f"Swapping {amt_to_buy_sell:.{from_asset_decimals}f} {from_asset_token_code} to {quotes['tinyman'].amount_out_with_slippage}")

                        # Prepare a transaction group.
                        transaction_group = lps["tinyman"].prepare_swap_transactions_from_quote(
                            quotes["tinyman"])

                        # Sign the group with the wallet's key.
                        transaction_group.sign_with_private_key(
                            account.getAddress(), account.getPrivateKey())

                        # Submit transactions to the network and wait for confirmation.
                        amm_clients["tinyman"].submit(
                            transaction_group, wait=True)

                        # Check if any excess remains after the swap.
                        to_asset_id = 0 if supported_assets[
                            asset_out_id].asset_onchain_id == 1 else supported_assets[asset_out_id].asset_onchain_id
                        tinyman_asset = amm_clients["tinyman"].fetch_asset(
                            to_asset_id)
                        excess = lps["tinyman"].fetch_excess_amounts()

                        if tinyman_asset in excess:
                            amount = excess[tinyman_asset]
                            total_asset_out_received += amount.amount
                            print(f'Excess: {amount}')

                            transaction_group = lps["tinyman"].prepare_redeem_transactions(
                                amount)
                            transaction_group.sign_with_private_key(
                                account.getAddress(), account.getPrivateKey())
                            amm_clients["tinyman"].submit(
                                transaction_group, wait=True)

                        total_asset_out_received = supported_assets[asset_out_id].get_unscaled_from_scaled_amount(
                            total_asset_out_received)

                    print(
                        f"\nSwap completed! You received {total_asset_out_received:.{to_asset_decimals}f} {to_asset_token_code}.\n")

                    # Swap will be marked as completed in the off-chain DB.
                    swaps_completed.append(
                        (doc["_id"], total_asset_out_received))
                else:
                    print("Swap requirement not yet met.\n")

                print(
                    "--------------------------------------------------------------------------------\n")

            except Exception as e:
                print(f"Error: {e}")

        # Update completed flag for the completed swaps.
        for swap in swaps_completed:
            db.orderbook.update_one(
                {"_id": swap[0]},
                {'$set':
                    {
                        'is_completed': True,
                        'amt_received': float(swap[1]),
                        'completed_date': datetime.today().replace(microsecond=0)
                    }
                 }
            )

        swaps_completed.clear()

        time.sleep(float(env("arbitrage:orderbook:delay")))
