from dataclasses import dataclass
from decimal import Decimal
from typing import Any


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
        return int(amount * Decimal(10**self.decimals))

    def get_unscaled_from_scaled_amount(self, amount_scaled: int) -> Decimal:
        """Takes an asset amount that has been scaled by asset's decimals and returns the amount before it was scaled.

        Parameters:
        amount_scaled (int): Amount of asset, scaled to it's  decimals

        Returns:
        decimal.Decimal
        """
        return Decimal(amount_scaled / (10**self.decimals))


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
