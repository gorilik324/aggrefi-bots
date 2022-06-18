class AlgoTradeBotError(Exception):
    """Base class for other custom exceptions defined by Block Adjacent's AggreFi bots.

    Attributes:
        message -- Explanation of the error
    """

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class SupportedAssetsLookupError(AlgoTradeBotError):
    """Exception raised when the bot is unable to determine the Algorand assets that can be traded with it."""
    pass


class AlgofiLPNotFoundError(AlgoTradeBotError):
    """Exception raised when an LP for a specific Algo/ASA or ASA/ASA asset pair could not be found on Algofi's DEX.

    Attributes:
        asset1_id -- ASA ID of the first asset in the pair
        asset2_id -- ASA ID of the second asset in the pair
    """

    def __init__(self, asset1_id, asset2_id):
        self.asset1_id = asset1_id
        self.asset2_id = asset2_id
        self.message = f"Pool for the asset pair with IDs {asset1_id} and {asset2_id} has not been created and/or initialized on the Algofi DEX as yet!"
        super().__init__(self.message)


class TinymanLPNotFoundError(AlgoTradeBotError):
    """Exception raised when an LP for a specific Algo/ASA or ASA/ASA asset pair could not be found on Tinyman's DEX.

    Attributes:
        asset1_id -- ASA ID of the first asset in the pair
        asset2_id -- ASA ID of the second asset in the pair
    """

    def __init__(self, asset1_id, asset2_id):
        self.asset1_id = asset1_id
        self.asset2_id = asset2_id
        self.message = f"Pool for the asset pair with IDs {asset1_id} and {asset2_id} has not been created and/or initialized on the Tinyman DEX as yet!"
        super().__init__(self.message)
