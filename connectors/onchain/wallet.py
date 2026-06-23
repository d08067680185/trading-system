"""EVM wallet management — sign, send, track nonces."""
from __future__ import annotations
import asyncio
import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("EVMWallet")

ERC20_ABI_BALANCE = [{"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"stateMutability":"view","type":"function"},
                     {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"}]


class EVMWallet:
    """
    Async EVM wallet. Requires web3.py >= 6.
    Manages nonces locally to avoid race conditions with concurrent sends.
    """

    def __init__(self, private_key: str, rpc_url: str, chain_id: int = 1):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self._key = private_key
        self._nonce_lock = asyncio.Lock()
        self._nonce: Optional[int] = None
        self._w3 = None
        self.address: Optional[str] = None
        self._gas_manager = None  # optional GasManager — fills EIP-1559 fees when set

    def set_gas_manager(self, gas_manager) -> None:
        """Inject a GasManager so outgoing txs use live EIP-1559 fees."""
        self._gas_manager = gas_manager

    async def connect(self) -> None:
        try:
            from web3 import AsyncWeb3
            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self.rpc_url))
            account = self._w3.eth.account.from_key(self._key)
            self.address = account.address
            self._nonce = await self._w3.eth.get_transaction_count(self.address)
            logger.info(f"Wallet connected: {self.address} (chain {self.chain_id}, nonce={self._nonce})")
        except ImportError:
            raise RuntimeError("web3 not installed. Run: pip install web3>=6.0")

    async def get_eth_balance(self) -> Decimal:
        if not self._w3:
            raise RuntimeError("Wallet not connected")
        bal = await self._w3.eth.get_balance(self.address)
        return Decimal(bal) / Decimal(10 ** 18)

    async def get_token_balance(self, token_address: str) -> Decimal:
        """Returns token balance in human units (divided by decimals)."""
        if not self._w3:
            raise RuntimeError("Wallet not connected")
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(token_address),
            abi=ERC20_ABI_BALANCE,
        )
        raw = await contract.functions.balanceOf(self.address).call()
        decimals = await contract.functions.decimals().call()
        return Decimal(raw) / Decimal(10 ** decimals)

    async def sign_and_send(self, tx: dict) -> str:
        """Sign and broadcast a transaction. Returns tx hash."""
        if not self._w3:
            raise RuntimeError("Wallet not connected")
        async with self._nonce_lock:
            tx.setdefault("chainId", self.chain_id)
            tx.setdefault("from", self.address)
            tx["nonce"] = self._nonce
            # Apply live EIP-1559 fees from the GasManager when present and not
            # already set on the tx (build_transaction may have filled them).
            if self._gas_manager is not None and "maxFeePerGas" not in tx:
                try:
                    fees = await self._gas_manager.get_fees()
                    if fees:
                        tx.update(fees.to_wei())
                except Exception as e:  # never block a send on gas oracle
                    logger.warning(f"GasManager fee fetch failed, using node default: {e}")
            if "gas" not in tx:
                tx["gas"] = await self._w3.eth.estimate_gas(tx)
            signed = self._w3.eth.account.sign_transaction(tx, self._key)
            # web3.py v6 exposes `rawTransaction`; v7 renamed it to `raw_transaction`.
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            try:
                tx_hash = await self._w3.eth.send_raw_transaction(raw)
            except Exception:
                # Resync nonce from chain so a stale local counter (e.g. "nonce too
                # low/high" after a dropped or externally-sent tx) doesn't wedge the
                # wallet permanently. Next call retries with the corrected nonce.
                try:
                    self._nonce = await self._w3.eth.get_transaction_count(self.address)
                except Exception:
                    pass
                raise
            self._nonce += 1
            # hexbytes>=1 .hex() drops the 0x prefix; to_0x_hex() restores it.
            return tx_hash.to_0x_hex() if hasattr(tx_hash, "to_0x_hex") else "0x" + tx_hash.hex().removeprefix("0x")

    async def wait_for_receipt(self, tx_hash: str, timeout: int = 120) -> dict:
        """Wait for a transaction to be mined. Returns receipt dict."""
        if not self._w3:
            raise RuntimeError("Wallet not connected")
        from web3.exceptions import TransactionNotFound
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                receipt = await self._w3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    return dict(receipt)
            except TransactionNotFound:
                pass  # not mined yet — keep polling
            await asyncio.sleep(2)
        raise TimeoutError(f"Transaction {tx_hash} not mined within {timeout}s")

    async def approve_token(self, token_address: str, spender: str, amount: int) -> str:
        """Approve spender to use token. Returns tx hash."""
        approve_abi = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
                        "name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
        contract = self._w3.eth.contract(
            address=self._w3.to_checksum_address(token_address), abi=approve_abi
        )
        tx = await contract.functions.approve(
            self._w3.to_checksum_address(spender), amount
        ).build_transaction({"chainId": self.chain_id, "from": self.address})
        return await self.sign_and_send(tx)
