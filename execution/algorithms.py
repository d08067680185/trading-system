"""
Execution algorithms: TWAP and VWAP order splitting.

TWAP (Time-Weighted Average Price):
  Splits a large order into N equal slices executed over T seconds.
  Reduces market impact by averaging execution price over time.

VWAP (Volume-Weighted Average Price):
  Weights slices by historical volume distribution across the day.
  Aims to match or beat the market's VWAP benchmark.

Both return an async generator of (slice_qty, delay_s) pairs so callers
can cancel mid-way if conditions change.
"""
from __future__ import annotations
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine
    from core.types import Exchange, OrderSide, Order

logger = logging.getLogger("ExecAlgorithms")


@dataclass
class AlgoOrder:
    """Tracks progress of an algorithmic order."""
    algo_id: str
    strategy_id: str
    exchange: str
    symbol: str
    side: str
    total_qty: float
    filled_qty: float = 0.0
    slices_total: int = 0
    slices_done: int = 0
    avg_fill_price: float = 0.0
    status: str = "pending"   # pending | running | done | cancelled | error
    created_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    child_order_ids: list[str] = field(default_factory=list)

    @property
    def remaining_qty(self) -> float:
        return self.total_qty - self.filled_qty

    def to_dict(self) -> dict:
        return {
            "algo_id": self.algo_id,
            "strategy_id": self.strategy_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side,
            "total_qty": round(self.total_qty, 6),
            "filled_qty": round(self.filled_qty, 6),
            "remaining_qty": round(self.remaining_qty, 6),
            "fill_pct": round(self.filled_qty / self.total_qty * 100, 1) if self.total_qty else 0,
            "slices_total": self.slices_total,
            "slices_done": self.slices_done,
            "avg_fill_price": round(self.avg_fill_price, 6),
            "status": self.status,
            "created_at": self.created_at,
            "error": self.error,
        }


class ExecutionAlgorithms:
    def __init__(self, engine: "TradingEngine"):
        self._engine = engine
        self._orders: dict[str, AlgoOrder] = {}
        import uuid
        self._uuid = uuid

    def list_orders(self) -> list[dict]:
        return [o.to_dict() for o in sorted(
            self._orders.values(), key=lambda x: -x.created_at
        )]

    def get_order(self, algo_id: str) -> Optional[AlgoOrder]:
        return self._orders.get(algo_id)

    async def cancel(self, algo_id: str) -> bool:
        order = self._orders.get(algo_id)
        if not order or order.status not in ("pending", "running"):
            return False
        order.status = "cancelled"
        return True

    # ── TWAP ─────────────────────────────────────────────────────────────────

    def place_twap(
        self,
        strategy_id: str,
        exchange: "Exchange",
        symbol: str,
        side: "OrderSide",
        total_qty: Decimal,
        duration_s: int = 300,
        n_slices: int = 10,
        qty_precision: int = 6,
    ) -> AlgoOrder:
        """
        Place a TWAP order. Returns AlgoOrder immediately; runs in background.
        Each slice = total_qty / n_slices, placed every duration_s / n_slices seconds.
        """
        algo_id = self._uuid.uuid4().hex[:10]
        order = AlgoOrder(
            algo_id=algo_id,
            strategy_id=strategy_id,
            exchange=exchange.value,
            symbol=symbol,
            side=side.value,
            total_qty=float(total_qty),
            slices_total=n_slices,
        )
        self._orders[algo_id] = order
        asyncio.create_task(self._run_twap(
            order, exchange, side, total_qty, duration_s, n_slices, qty_precision
        ))
        return order

    async def _run_twap(
        self, order: AlgoOrder, exchange, side, total_qty, duration_s, n_slices, qty_prec
    ) -> None:
        from core.types import OrderType
        order.status = "running"
        interval = duration_s / n_slices
        slice_qty = float(total_qty) / n_slices
        slice_qty = round(slice_qty, qty_prec)

        for i in range(n_slices):
            if order.status == "cancelled":
                logger.info(f"TWAP {order.algo_id} cancelled at slice {i}")
                return
            if slice_qty <= 0:
                break

            try:
                filled = await self._engine.place_order(
                    exchange=exchange,
                    symbol=order.symbol,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=Decimal(str(slice_qty)),
                    strategy_id=order.strategy_id,
                )
                if filled:
                    order.child_order_ids.append(filled.order_id)
                    order.filled_qty += float(filled.filled_qty)
                    order.slices_done += 1
                    fill_price = float(filled.avg_price or filled.price or 0)
                    if fill_price > 0:
                        # Update rolling avg fill price
                        prev = order.avg_fill_price * (order.slices_done - 1)
                        order.avg_fill_price = (prev + fill_price) / order.slices_done
                    logger.debug(
                        f"TWAP {order.algo_id} slice {i+1}/{n_slices}: "
                        f"qty={slice_qty} @ {fill_price:.4f}"
                    )
            except Exception as e:
                logger.warning(f"TWAP slice {i+1} failed: {e}")
                order.error = str(e)

            if i < n_slices - 1:
                await asyncio.sleep(interval)

        order.status = "done" if order.filled_qty > 0 else "error"
        logger.info(
            f"TWAP {order.algo_id} done: filled {order.filled_qty:.6f} "
            f"avg @ {order.avg_fill_price:.4f}"
        )

    # ── VWAP ─────────────────────────────────────────────────────────────────

    def place_vwap(
        self,
        strategy_id: str,
        exchange: "Exchange",
        symbol: str,
        side: "OrderSide",
        total_qty: Decimal,
        duration_s: int = 300,
        n_slices: int = 10,
        qty_precision: int = 6,
    ) -> AlgoOrder:
        """
        Place a VWAP order. Weights slices by a U-shaped intraday volume profile
        (higher volume at open/close, lower in the middle — typical crypto pattern).
        """
        algo_id = self._uuid.uuid4().hex[:10]
        order = AlgoOrder(
            algo_id=algo_id,
            strategy_id=strategy_id,
            exchange=exchange.value,
            symbol=symbol,
            side=side.value,
            total_qty=float(total_qty),
            slices_total=n_slices,
        )
        self._orders[algo_id] = order
        asyncio.create_task(self._run_vwap(
            order, exchange, side, total_qty, duration_s, n_slices, qty_precision
        ))
        return order

    async def _run_vwap(
        self, order: AlgoOrder, exchange, side, total_qty, duration_s, n_slices, qty_prec
    ) -> None:
        from core.types import OrderType
        order.status = "running"
        interval = duration_s / n_slices
        total = float(total_qty)

        # U-shaped volume weights (more at start/end, less in middle)
        weights = self._u_shape_weights(n_slices)
        slice_qtys = [round(total * w, qty_prec) for w in weights]
        # Fix rounding residuals: add remainder to first slice
        diff = round(total - sum(slice_qtys), qty_prec)
        slice_qtys[0] = round(slice_qtys[0] + diff, qty_prec)

        for i, qty in enumerate(slice_qtys):
            if order.status == "cancelled":
                return
            if qty <= 0:
                order.slices_done += 1
                continue
            try:
                filled = await self._engine.place_order(
                    exchange=exchange,
                    symbol=order.symbol,
                    side=side,
                    order_type=OrderType.MARKET,
                    quantity=Decimal(str(qty)),
                    strategy_id=order.strategy_id,
                )
                if filled:
                    order.child_order_ids.append(filled.order_id)
                    order.filled_qty += float(filled.filled_qty)
                    order.slices_done += 1
                    fill_price = float(filled.avg_price or filled.price or 0)
                    if fill_price > 0:
                        prev = order.avg_fill_price * (order.slices_done - 1)
                        order.avg_fill_price = (prev + fill_price) / order.slices_done
            except Exception as e:
                logger.warning(f"VWAP slice {i+1} failed: {e}")
                order.error = str(e)

            if i < n_slices - 1:
                await asyncio.sleep(interval)

        order.status = "done" if order.filled_qty > 0 else "error"
        logger.info(
            f"VWAP {order.algo_id} done: filled {order.filled_qty:.6f} "
            f"avg @ {order.avg_fill_price:.4f}"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _u_shape_weights(n: int) -> list[float]:
        """Generate U-shaped normalized weights for n slices."""
        if n == 1:
            return [1.0]
        # Cosine-based U-shape over a FULL period: weight = 1 + cos(2π × i / (n-1)).
        # High at both ends (open/close), trough in the middle — matches the
        # intraday volume smile. (A half period, π, would just ramp 2→0.)
        raw = [1.0 + math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
        total = sum(raw)
        return [w / total for w in raw]
