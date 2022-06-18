from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Tuple
from src.classes.exceptions import SupportedAssetsLookupError

from src.helpers import get_db_client


@dataclass
class AlgoAsset:
    """Simple class to store details of an Algorand asset."""
    id: str
    asset_name: str
    asset_code: str
    asset_onchain_id: int
    decimals: int
    is_native: bool
    is_active: bool

    def get_scaled_amount(self, amount: Decimal) -> int:
        """Returns an integer representation of asset amount scaled by asset's decimals.

        Parameters:
        amount (decimal.Decimal): Amount of asset

        Returns:
        int
        """
        return int(amount * Decimal(10**self.num_decimal_places))

    def get_unscaled_from_scaled_amount(self, amount_scaled: int) -> Decimal:
        """Takes an asset amount that has been scaled by asset's decimals and returns the amount before it was scaled.

        Parameters:
        amount_scaled (int): Amount of asset, scaled to it's  decimals

        Returns:
        decimal.Decimal
        """
        return Decimal(amount_scaled / (10**self.num_decimal_places))

    @staticmethod
    def get_supported_algo_assets():
        """Returns a dictionary with details of the Algorand assets that can be traded with our bots."""
        assets: Dict[str, AlgoAsset] = {}
        client = get_db_client()
        db = client.aggrefidb

        cursor = db.assets.find({'is_active': True})
        for doc in cursor:
            assets[str(doc["_id"])] = AlgoAsset(
                id=str(doc["_id"]),
                asset_name=doc["asset_name"],
                asset_code=doc["asset_code"],
                asset_onchain_id=doc["asset_onchain_id"],
                decimals=doc["decimals"],
                is_native=doc["is_native"],
                is_active=doc["is_active"]
            )

        return assets

    @staticmethod
    def get_asset_details(asset_ids: Tuple[int, ...]):
        """Get details of a tuple of specified assets."""
        supported_assets_dict = AlgoAsset.get_supported_algo_assets()

        if supported_assets_dict is None:
            raise SupportedAssetsLookupError(
                'Unable to retrieve information on supported assets')
        else:
            supported_assets = supported_assets_dict.values()
            assets = {
                value.asset_onchain_id: value for value in supported_assets if value.asset_onchain_id in asset_ids
            }
            return assets


@dataclass
class SwapAmount:
    """Simple class to store details for an asset out amount from a DEX swap."""
    to_asset: AlgoAsset
    from_asset: AlgoAsset
    quote: Any
    amount_in: Decimal
    amount_out: Decimal
    amount_out_with_slippage: Decimal
    dex: str
    slippage: float
