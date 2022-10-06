import algosdk


class Account:
    """Simple class that takes an Algorand wallet mnemonic and stores the corresponding
       address and private key for easy access.
    """
    address: str
    private_key: str

    def __init__(self, mnemonic_phrase: str) -> None:
        self.private_key = algosdk.mnemonic.to_private_key(mnemonic_phrase)
        self.address = algosdk.account.address_from_private_key(
            self.private_key)

    def getAddress(self) -> str:
        return self.address

    def getPrivateKey(self) -> str:
        return self.private_key
