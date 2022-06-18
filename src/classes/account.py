from algosdk import mnemonic


class Account:
    """Simple class that takes an Algorand wallet mnemonic and stores the corresponding
       address and private key for easy access.
    """
    address: str
    private_key: str

    def __init__(self, mnemonic_phrase: str) -> None:
        self.address = mnemonic.to_public_key(mnemonic_phrase)
        self.private_key = mnemonic.to_private_key(mnemonic_phrase)

    def getAddress(self) -> str:
        return self.address

    def getPrivateKey(self) -> str:
        return self.private_key
