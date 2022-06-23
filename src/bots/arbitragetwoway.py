import os
import sys
from decimal import Decimal
from typing import Dict
from json_environ import Environ
from src.classes.account import Account
from src.classes.asset import AlgoAsset

from src.helpers import get_amm_clients, get_asset_details, get_network

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


def run_bot():
    account = Account(env("arbitrage:twoway:account:mnemonic"))

    print(
        f"Initializing Arbitrage two-token bot on Algorand {network}...\nChecking for configured assets...")
    assets = get_configured_assets()
    asset_codes = [value.asset_code for value in assets.values()]
    print(f"Configured assets are: {', '.join(asset_codes)}")

    print("Instantiating AMM Clients for each supported Algorand DEX...")
    amm_clients = get_amm_clients(account)

    print("Initialization completed. Starting round trip checks for arbitrage...\n")
    trade_amt = Decimal(env("arbitrage:threeway:amounts:starting_amt"))
