import asyncio
import base64
import json
from typing import Optional

import aiohttp
from aiohttp import ClientResponseError, ClientConnectorError, ServerDisconnectedError
from loguru import logger
import datetime
from sdk.py_near import constants
from sdk.py_near.constants import TIMEOUT_WAIT_RPC
from sdk.py_near.exceptions.exceptions import RpcNotAvailableError
from sdk.py_near.exceptions.provider import (
    UnknownBlockError,
    InvalidAccount,
    NoContractCodeError,
    UnknownAccount,
    TooLargeContractStateError,
    UnavailableShardError,
    NoSyncedBlocksError,
    InternalError,
    NoSyncedYetError,
    InvalidTransactionError,
    RPCTimeoutError,
    UnknownAccessKeyError,
    ERROR_CODE_TO_EXCEPTION,
)
from sdk.py_near.models import TransactionResult

PROVIDER_CODE_TO_EXCEPTION = {
    "UNKNOWN_BLOCK": UnknownBlockError,
    "INVALID_ACCOUNT": InvalidAccount,
    "UNKNOWN_ACCOUNT": UnknownAccount,
    "NO_CONTRACT_CODE": NoContractCodeError,
    "TOO_LARGE_CONTRACT_STATE": TooLargeContractStateError,
    "UNAVAILABLE_SHARD": UnavailableShardError,
    "NO_SYNCED_BLOCKS": NoSyncedBlocksError,
    "INTERNAL_ERROR": InternalError,
    "NOT_SYNCED_YET": NoSyncedYetError,
    "INVALID_TRANSACTION": InvalidTransactionError,
    "TIMEOUT_ERROR": RPCTimeoutError,
    "UNKNOWN_ACCESS_KEY": UnknownAccessKeyError,
}


class JsonProvider(object):
    def __init__(self, rpc_addr):
        if isinstance(rpc_addr, tuple):
            self._rpc_addresses = ["http://{}:{}".format(*rpc_addr)]
        elif isinstance(rpc_addr, list):
            self._rpc_addresses = rpc_addr
        else:
            self._rpc_addresses = [rpc_addr]
        self._available_rpcs = self._rpc_addresses.copy()
        self._last_rpc_addr_check = 0

    async def check_available_rpcs(self):
        available_rpcs = []
        for rpc_addr in self._rpc_addresses:
            try:
                async with aiohttp.ClientSession() as session:
                    timestamp_start = datetime.datetime.utcnow().timestamp()
                    r = await session.get(
                        "%s/status" % rpc_addr, timeout=TIMEOUT_WAIT_RPC
                    )
                    if r.status == 200:
                        data = json.loads(await r.text())
                        if not data["sync_info"]["syncing"]:
                            available_rpcs.append(
                                (
                                    rpc_addr,
                                    datetime.datetime.utcnow().timestamp()
                                    - timestamp_start,
                                )
                            )
                            continue
                    if rpc_addr in self._available_rpcs:
                        if r.status == 200:
                            logger.error(f"Remove async RPC : {rpc_addr}")
                        else:
                            logger.error(
                                f"Remove rpc because of error {r.status}: {rpc_addr}"
                            )
            except Exception as e:
                if rpc_addr in self._available_rpcs:
                    logger.error(f"Remove rpc: {e}")
        self._available_rpcs = [
            r[0] for r in sorted(available_rpcs, key=lambda x: x[1])
        ]

    async def call_rpc_request(self, method, params, timeout=TIMEOUT_WAIT_RPC):
        if (
            self._last_rpc_addr_check < datetime.datetime.now().timestamp() - 30
            or not self._available_rpcs
        ):
            self._last_rpc_addr_check = datetime.datetime.now().timestamp()
            if self._available_rpcs:
                asyncio.create_task(self.check_available_rpcs())
            else:
                logger.warning("No RPC available, rechecking")
                await self.check_available_rpcs()

        if not self._available_rpcs:
            raise RpcNotAvailableError("No RPC available")

        j = {"method": method, "params": params, "id": "dontcare", "jsonrpc": "2.0"}

        content = None
        for rpc_addr in self._available_rpcs:
            try:
                async with aiohttp.ClientSession() as session:
                    r = await session.post(rpc_addr, json=j, timeout=timeout)
                    r.raise_for_status()
                    content = json.loads(await r.text())
                break
            except (
                RPCTimeoutError,
                ClientResponseError,
                ClientConnectorError,
                ServerDisconnectedError,
                ConnectionError,
            ) as e:
                logger.error(f"Rpc error: {e}")
                continue
        return content

    @staticmethod
    def get_error_from_response(content: dict):
        if "error" in content:
            error_code = content["error"].get("cause", {}).get("name", "")
            body = content["error"]["data"]
            error = PROVIDER_CODE_TO_EXCEPTION.get(error_code, InternalError)(
                body, error_json=content["error"]
            )
            while True:
                if not isinstance(body, dict):
                    break
                if not body:
                    return error
                key, body = list(body.items())[0]
                if key in ERROR_CODE_TO_EXCEPTION:
                    error = ERROR_CODE_TO_EXCEPTION[key](
                        body, error_json=content["error"]
                    )
                else:
                    break
            return error

    async def json_rpc(self, method, params, timeout=TIMEOUT_WAIT_RPC):
        content = await self.call_rpc_request(method, params, timeout)
        if not content:
            raise RpcNotAvailableError("RPC not available")

        error = self.get_error_from_response(content)
        if error:
            raise error
        return content["result"]

    async def send_tx(self, signed_tx: str, timeout: int = constants.TIMEOUT_WAIT_RPC):
        """
        Send a signed transaction to the network and return the hash of the transaction
        :param signed_tx: base64 encoded signed transaction, str.
        :param timeout: rpc request timeout
        :return:
        """
        return await self.json_rpc("broadcast_tx_async", [signed_tx], timeout=timeout)

    async def send_tx_and_wait(
        self,
        signed_tx: str,
        timeout: int = constants.TIMEOUT_WAIT_RPC,
        trx_hash: Optional[str] = None,
        receiver_id: Optional[str] = None,
    ):
        """
        Send a signed transaction to the network and wait for it to be included in a block
        :param signed_tx: base64 encoded signed transaction, str
        :param timeout: rpc request timeout
        :return:
        """
        try:
            return await self.json_rpc(
                "broadcast_tx_commit",
                [signed_tx],
                timeout=timeout,
            )
        except RPCTimeoutError:
            if receiver_id and trx_hash:
                for _ in range(constants.TIMEOUT_WAIT_RPC // 3):
                    await asyncio.sleep(3)
                    try:
                        result = await self.get_tx(trx_hash, receiver_id)
                    except InternalError:
                        continue
                    except Exception as e:
                        # logger.exception(e)
                        continue
                    if result:
                        return result
            raise

    async def get_status(self):

        for rpc_addr in self._available_rpcs:
            try:
                async with aiohttp.ClientSession() as session:
                    r = await session.get(
                        "%s/status" % rpc_addr, timeout=TIMEOUT_WAIT_RPC
                    )
                    if r.status == 200:
                        data = json.loads(await r.text())
                        if not data["sync_info"]["syncing"]:
                            return data
            except (
                ClientResponseError,
                ClientConnectorError,
                ServerDisconnectedError,
                ConnectionError,
            ) as e:
                logger.error(f"Rpc error: {e}")
            except Exception as e:
                logger.exception(e)

        raise RpcNotAvailableError(f"RPC not available {self._available_rpcs}")

    async def get_validators(self):
        return await self.json_rpc("validators", [None])

    async def query(self, query_object):
        return await self.json_rpc("query", query_object)

    async def get_account(self, account_id, finality="optimistic"):
        return await self.json_rpc(
            "query",
            {
                "request_type": "view_account",
                "account_id": account_id,
                "finality": finality,
            },
        )

    async def get_access_key_list(self, account_id, finality="optimistic"):
        return await self.json_rpc(
            "query",
            {
                "request_type": "view_access_key_list",
                "account_id": account_id,
                "finality": finality,
            },
        )

    async def get_access_key(self, account_id, public_key, finality="optimistic"):
        """

        :param account_id:
        :param public_key:
        :param finality:
        :return: {'block_hash': '..', 'block_height': int, 'nonce': int, 'permission': 'FullAccess'}
        """
        return await self.json_rpc(
            "query",
            {
                "request_type": "view_access_key",
                "account_id": account_id,
                "public_key": public_key,
                "finality": finality,
            },
        )

    async def view_call(
        self,
        account_id,
        method_name,
        args,
        finality="optimistic",
        block_id: Optional[int] = None,
    ):
        body = {
            "request_type": "call_function",
            "account_id": account_id,
            "method_name": method_name,
            "args_base64": base64.b64encode(args).decode("utf8"),
        }
        if block_id:
            body["block_id"] = block_id
        else:
            body["finality"] = finality
        return await self.json_rpc("query", body)

    async def get_block(self, block_id):
        return await self.json_rpc("block", [block_id])

    async def get_chunk(self, chunk_id):
        return await self.json_rpc("chunk", [chunk_id])

    async def get_tx(self, tx_hash, tx_recipient_id) -> TransactionResult:
        return TransactionResult(
            **await self.json_rpc("tx", [tx_hash, tx_recipient_id])
        )

    async def get_changes_in_block(self, changes_in_block_request):
        return await self.json_rpc(
            "EXPERIMENTAL_changes_in_block", changes_in_block_request
        )

    async def get_validators_ordered(self, block_hash):
        return await self.json_rpc("EXPERIMENTAL_validators_ordered", [block_hash])

    async def get_light_client_proof(
        self, outcome_type, tx_or_receipt_id, sender_or_receiver_id, light_client_head
    ):
        if outcome_type == "receipt":
            params = {
                "type": "receipt",
                "receipt_id": tx_or_receipt_id,
                "receiver_id": sender_or_receiver_id,
                "light_client_head": light_client_head,
            }
        else:
            params = {
                "type": "transaction",
                "transaction_hash": tx_or_receipt_id,
                "sender_id": sender_or_receiver_id,
                "light_client_head": light_client_head,
            }
        return await self.json_rpc("light_client_proof", params)

    async def get_next_light_client_block(self, last_block_hash):
        return await self.json_rpc("next_light_client_block", [last_block_hash])
