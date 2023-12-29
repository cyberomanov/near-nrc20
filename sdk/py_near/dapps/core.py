NEAR = 1_000_000_000_000_000_000_000_000

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sdk.py_near.account import Account


class DappClient:
    def __init__(self, account: "Account"):
        self._account = account

