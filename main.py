import asyncio

from loguru import logger

from config import accounts
from sdk.py_near.account import Account
from sdk.py_near.dapps.core import NEAR
from utils.add_logger import add_logger


async def send_transaction(account: dict):
    while True:
        try:
            acc = Account(account_id=account["account_id"], private_key=account["private_key"])
            await acc.startup()
            balance_int = await acc.get_balance()
            balance_float = round(balance_int / NEAR, 4)

            if balance_float > 1:
                tr = await acc.function_call(
                    contract_id="inscription.near",
                    method_name="inscribe",
                    args={
                        "p": "nrc-20",
                        "op": "mint",
                        "tick": "1dragon",
                        "amt": "100000000"
                    },
                    nowait=True
                )
                logger.success(f'{account["account_id"]}: {balance_float} $NEAR, hash: https://nearblocks.io/address/{tr}.')

                new_balance_int = await acc.get_balance()
                while new_balance_int == balance_int:
                    new_balance_int = await acc.get_balance()
                    await asyncio.sleep(1)

            else:
                logger.warning(f'low balance: {balance_float} $NEAR.')
                break
        except Exception as e:
            logger.exception(e)


async def main(accounts: [dict]):
    tasks = [asyncio.create_task(send_transaction(acc)) for acc in accounts]
    await asyncio.gather(*tasks)


if __name__ == '__main__':
    add_logger()
    try:
        asyncio.run(main(accounts))
    except Exception as e:
        logger.exception(e)
