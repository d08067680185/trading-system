"""
PnL Attribution Engine.

Decomposes each fill into distinct PnL sources:
  spread    — profit from bid-ask spread capture (arb strategies)
  funding   — profit from funding rate collection (carry strategies)
  execution — slippage / timing edge relative to mid price
  fee       — exchange fees (always negative)

Called from the engine event loop after each FILLED order update.
Persists records to the pnl_attribution DB table via DataStorage.
"""
from __future__ import annotations
import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from data.storage import DataStorage
    from core.types import Order

logger = logging.getLogger("PnLAttribution")

# Source constants
SPREAD    = "spread"
FUNDING   = "funding"
EXECUTION = "execution"
FEE       = "fee"


class PnLAttributor:
    def __init__(self, storage: "DataStorage"):
        self._db = storage
        # Per-strategy pending entry: strategy_id → {symbol: (side, entry_price, qty, ts)}
        self._entries: dict[str, dict] = {}
        # Snapshot of last mid prices: exchange:symbol → mid
        self._mids: dict[str, float] = {}
        # Funding rates cached: exchange:symbol → rate per 8h
        self._funding_rates: dict[str, float] = {}

    def update_mid(self, exchange: str, symbol: str, bid: float, ask: float) -> None:
        self._mids[f"{exchange}:{symbol}"] = (bid + ask) / 2.0

    def update_funding_rate(self, exchange: str, symbol: str, rate_8h: float) -> None:
        self._funding_rates[f"{exchange}:{symbol}"] = rate_8h

    async def record_fill(self, order: "Order") -> None:
        """Analyse a filled order and store attribution records."""
        if not order.strategy_id:
            return
        sid = order.strategy_id
        sym = order.symbol
        ex  = order.exchange.value
        mid_key = f"{ex}:{sym}"

        fill_price = float(order.avg_price or order.price or 0)
        fill_qty   = float(order.filled_qty)
        fee        = float(order.fee)
        mid        = self._mids.get(mid_key, fill_price)

        # ── Fee attribution ────────────────────────────────────────────────
        if fee > 0:
            asyncio.create_task(self._store(
                sid, ex, sym, FEE, -fee, order.order_id
            ))

        # ── Execution attribution (vs mid) ─────────────────────────────────
        if mid > 0:
            from core.types import OrderSide
            if order.side == OrderSide.BUY:
                exec_pnl = (mid - fill_price) * fill_qty   # paid below mid → positive
            else:
                exec_pnl = (fill_price - mid) * fill_qty   # sold above mid → positive
            if abs(exec_pnl) > 1e-8:
                asyncio.create_task(self._store(
                    sid, ex, sym, EXECUTION, exec_pnl, order.order_id
                ))

        # ── Spread / funding attribution via open→close matching ───────────
        entries = self._entries.setdefault(sid, {})
        from core.types import OrderSide

        if order.side == OrderSide.BUY:
            # Check if this closes a short entry
            entry = entries.pop(f"{ex}:{sym}:short", None)
            if entry:
                raw_pnl = (entry["price"] - fill_price) * fill_qty
                source  = FUNDING if entry.get("is_funding") else SPREAD
                asyncio.create_task(self._store(sid, ex, sym, source, raw_pnl, order.order_id))
            else:
                # Opening a long — record entry
                entries[f"{ex}:{sym}:long"] = {
                    "price": fill_price, "qty": fill_qty,
                    "ts": time.time(), "is_funding": self._is_funding_strategy(sid),
                }

        else:  # SELL
            entry = entries.pop(f"{ex}:{sym}:long", None)
            if entry:
                raw_pnl = (fill_price - entry["price"]) * fill_qty
                source  = FUNDING if entry.get("is_funding") else SPREAD
                asyncio.create_task(self._store(sid, ex, sym, source, raw_pnl, order.order_id))
            else:
                entries[f"{ex}:{sym}:short"] = {
                    "price": fill_price, "qty": fill_qty,
                    "ts": time.time(), "is_funding": self._is_funding_strategy(sid),
                }

    async def get_summary(self, days: int = 30) -> dict:
        return await self._db.get_attribution_summary(days)

    async def get_recent(self, strategy_id: Optional[str] = None, limit: int = 100) -> list:
        return await self._db.get_attribution(strategy_id=strategy_id, limit=limit)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _store(self, strategy_id: str, exchange: str, symbol: str,
                     source: str, pnl: float, order_id: str = "") -> None:
        try:
            await self._db.store_attribution(
                strategy_id=strategy_id,
                exchange=exchange,
                symbol=symbol,
                source=source,
                pnl_usdt=round(pnl, 6),
                order_id=order_id or "",
            )
        except Exception as e:
            logger.warning(f"Attribution store error: {e}")

    @staticmethod
    def _is_funding_strategy(strategy_id: str) -> bool:
        return any(kw in strategy_id for kw in ("funding", "carry", "cash"))
