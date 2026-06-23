"""EIP-1559 gas price manager with exponential moving average tracking."""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("GasManager")


@dataclass
class GasFees:
    base_fee_gwei: float
    max_priority_fee_gwei: float
    max_fee_gwei: float
    estimated_gwei: float     # recommended total
    timestamp: float = field(default_factory=time.time)

    def to_wei(self) -> dict:
        return {
            "maxFeePerGas": int(self.max_fee_gwei * 1e9),
            "maxPriorityFeePerGas": int(self.max_priority_fee_gwei * 1e9),
        }


class GasManager:
    """
    Tracks EIP-1559 gas prices with EMA smoothing.
    Refreshes automatically in background.
    """

    def __init__(self, w3, priority_multiplier: float = 1.2, base_multiplier: float = 1.1):
        self._w3 = w3
        self._priority_multiplier = priority_multiplier
        self._base_multiplier = base_multiplier
        self._latest: Optional[GasFees] = None
        self._ema_base: Optional[float] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._refresh()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def get_fees(self) -> GasFees:
        if not self._latest:
            await self._refresh()
        return self._latest

    async def estimate_gas(self, tx: dict) -> int:
        try:
            return await self._w3.eth.estimate_gas(tx)
        except Exception as e:
            logger.warning(f"Gas estimation failed: {e}, using 200000")
            return 200_000

    async def _refresh(self) -> None:
        try:
            block = await self._w3.eth.get_block("latest")
            base_fee_wei = block.get("baseFeePerGas", 0)
            base_fee_gwei = base_fee_wei / 1e9

            # EMA smoothing of base fee
            alpha = 0.3
            if self._ema_base is None:
                self._ema_base = base_fee_gwei
            else:
                self._ema_base = alpha * base_fee_gwei + (1 - alpha) * self._ema_base

            # Suggest priority fee (tip)
            try:
                priority = await self._w3.eth.max_priority_fee
                priority_gwei = priority / 1e9
            except Exception:
                priority_gwei = 1.5  # fallback 1.5 gwei

            priority_gwei *= self._priority_multiplier
            max_fee_gwei = self._ema_base * self._base_multiplier + priority_gwei

            self._latest = GasFees(
                base_fee_gwei=self._ema_base,
                max_priority_fee_gwei=priority_gwei,
                max_fee_gwei=max_fee_gwei,
                estimated_gwei=max_fee_gwei,
            )
            logger.debug(f"Gas: base={self._ema_base:.2f} priority={priority_gwei:.2f} max={max_fee_gwei:.2f} gwei")
        except Exception as e:
            logger.warning(f"Gas refresh failed: {e}")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(12)  # ~1 ETH block
            await self._refresh()
