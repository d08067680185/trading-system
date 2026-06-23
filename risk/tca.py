"""
Transaction Cost Analysis (TCA).

Measures actual execution quality vs. a benchmark (mid-price at order submission).

Per-fill metrics:
  slippage_bps   = (fill_price - mid_price) / mid_price × 10000 × side_sign
                   negative = paid more than mid (bad)  positive = better than mid (good)
  is_maker       = filled as maker (saved fee vs taker)
  over_budget    = |slippage_bps| > slippage_budget_bps

Aggregates per strategy:
  mean_slippage_bps, p50, p99
  maker_rate          = fraction of fills that were maker
  over_budget_rate    = fraction that exceeded slippage budget
  total_cost_usdt     = sum of fees + slippage cost
  execution_score     = 0–100 (higher = better)
"""
from __future__ import annotations
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.types import Order

logger = logging.getLogger("TCA")

_DEFAULT_BUDGET_BPS = 5.0   # typical taker fee


@dataclass
class FillRecord:
    ts: float
    strategy_id: str
    exchange: str
    symbol: str
    side: str            # "buy" | "sell"
    fill_qty: float
    fill_price: float
    mid_price: float     # mid-price at time of order submission
    fee: float
    order_id: str
    slippage_bps: float  # negative = unfavorable (paid more than mid)
    is_maker: bool
    over_budget: bool


@dataclass
class TCAStats:
    strategy_id: str
    n_fills: int
    mean_slippage_bps: float
    median_slippage_bps: float
    p99_slippage_bps: float
    maker_rate_pct: float
    over_budget_rate_pct: float
    total_fee_usdt: float
    total_slippage_cost_usdt: float
    total_cost_usdt: float
    execution_score: float   # 0–100: 100 = perfect execution
    worst_fill: Optional[FillRecord]


class TransactionCostAnalyzer:
    def __init__(
        self,
        slippage_budget_bps: float = _DEFAULT_BUDGET_BPS,
        max_history_per_strategy: int = 5000,
    ):
        self._budget_bps = slippage_budget_bps
        self._max_hist   = max_history_per_strategy

        # strategy_id → deque of FillRecord
        self._fills: dict[str, deque] = {}
        # Pending mid-price snapshot: order_id → mid_price (set when order is placed)
        self._pending_mids: dict[str, float] = {}
        # Latest mid prices: "exchange:symbol" → mid
        self._mids: dict[str, float] = {}

    def snapshot_mid(self, order_id: str, exchange: str, symbol: str) -> None:
        """Call just before placing an order to record the reference mid-price."""
        mid = self._mids.get(f"{exchange}:{symbol}", 0.0)
        if mid > 0:
            self._pending_mids[order_id] = mid

    def update_mid(self, exchange: str, symbol: str, bid: float, ask: float) -> None:
        self._mids[f"{exchange}:{symbol}"] = (bid + ask) / 2.0

    def record_fill(self, order: "Order") -> None:
        """Called after a FILLED order update. Computes and stores TCA record."""
        order_id = order.order_id or ""
        mid = self._pending_mids.pop(order_id, 0.0)
        if mid <= 0:
            mid = self._mids.get(f"{order.exchange.value}:{order.symbol}", 0.0)

        fill_price = float(order.avg_price or order.price or 0)
        fill_qty   = float(order.filled_qty)
        fee        = float(order.fee)
        sid        = order.strategy_id or "unknown"

        if fill_price <= 0 or mid <= 0:
            return

        # Slippage: for BUY, paying above mid is negative (bad)
        #           for SELL, getting below mid is negative (bad)
        if order.side.value == "buy":
            slippage_bps = (mid - fill_price) / mid * 10000  # negative if overpaid
        else:
            slippage_bps = (fill_price - mid) / mid * 10000  # negative if undersold

        # Maker heuristic: if slippage_bps is significantly positive, likely a maker fill
        is_maker = slippage_bps > 1.0

        over_budget = abs(slippage_bps) > self._budget_bps and not is_maker

        rec = FillRecord(
            ts=time.time(),
            strategy_id=sid,
            exchange=order.exchange.value,
            symbol=order.symbol,
            side=order.side.value,
            fill_qty=fill_qty,
            fill_price=fill_price,
            mid_price=mid,
            fee=fee,
            order_id=order_id,
            slippage_bps=round(slippage_bps, 3),
            is_maker=is_maker,
            over_budget=over_budget,
        )

        if sid not in self._fills:
            self._fills[sid] = deque(maxlen=self._max_hist)
        self._fills[sid].append(rec)

    def get_stats(self, strategy_id: Optional[str] = None) -> dict[str, TCAStats]:
        """Return TCA statistics per strategy (or single strategy if specified)."""
        if strategy_id:
            sids = [strategy_id] if strategy_id in self._fills else []
        else:
            sids = list(self._fills.keys())

        result = {}
        for sid in sids:
            records = list(self._fills[sid])
            if not records:
                continue
            result[sid] = self._compute_stats(sid, records)
        return result

    def get_recent_fills(
        self, strategy_id: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """Return most recent fill records as dicts."""
        if strategy_id:
            records = list(self._fills.get(strategy_id, []))[-limit:]
        else:
            all_fills = sorted(
                (r for fills in self._fills.values() for r in fills),
                key=lambda r: r.ts,
            )
            records = all_fills[-limit:]
        return [self._rec_to_dict(r) for r in reversed(records)]

    def all_stats_dict(self) -> list[dict]:
        stats = self.get_stats()
        return [self._stats_to_dict(s) for s in stats.values()]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _compute_stats(self, sid: str, records: list[FillRecord]) -> TCAStats:
        slippages    = [r.slippage_bps for r in records]
        n            = len(records)
        sorted_sl    = sorted(slippages)
        mean_sl      = sum(slippages) / n
        median_sl    = sorted_sl[n // 2]
        p99_sl       = sorted_sl[min(n - 1, int(n * 0.99))]
        maker_count  = sum(1 for r in records if r.is_maker)
        over_count   = sum(1 for r in records if r.over_budget)
        total_fee    = sum(r.fee for r in records)
        slippage_cost = sum(
            -r.slippage_bps / 10000 * r.fill_price * r.fill_qty
            for r in records
        )
        worst = min(records, key=lambda r: r.slippage_bps) if records else None

        # Execution score: 100 = perfect (no slippage, all maker)
        score = 100.0
        score -= max(0.0, -mean_sl) * 5           # penalty per bps of avg slippage
        score -= (over_count / n * 100) * 0.3     # penalty for over-budget fills
        score += (maker_count / n * 100) * 0.1    # bonus for maker fills
        score = max(0.0, min(100.0, score))

        return TCAStats(
            strategy_id=sid,
            n_fills=n,
            mean_slippage_bps=round(mean_sl, 3),
            median_slippage_bps=round(median_sl, 3),
            p99_slippage_bps=round(p99_sl, 3),
            maker_rate_pct=round(maker_count / n * 100, 1),
            over_budget_rate_pct=round(over_count / n * 100, 1),
            total_fee_usdt=round(total_fee, 4),
            total_slippage_cost_usdt=round(slippage_cost, 4),
            total_cost_usdt=round(total_fee + slippage_cost, 4),
            execution_score=round(score, 1),
            worst_fill=worst,
        )

    @staticmethod
    def _rec_to_dict(r: FillRecord) -> dict:
        return {
            "ts": r.ts, "strategy_id": r.strategy_id,
            "exchange": r.exchange, "symbol": r.symbol,
            "side": r.side, "fill_qty": r.fill_qty,
            "fill_price": r.fill_price, "mid_price": r.mid_price,
            "fee": r.fee, "slippage_bps": r.slippage_bps,
            "is_maker": r.is_maker, "over_budget": r.over_budget,
        }

    @staticmethod
    def _stats_to_dict(s: TCAStats) -> dict:
        d = {
            "strategy_id": s.strategy_id, "n_fills": s.n_fills,
            "mean_slippage_bps": s.mean_slippage_bps,
            "median_slippage_bps": s.median_slippage_bps,
            "p99_slippage_bps": s.p99_slippage_bps,
            "maker_rate_pct": s.maker_rate_pct,
            "over_budget_rate_pct": s.over_budget_rate_pct,
            "total_fee_usdt": s.total_fee_usdt,
            "total_slippage_cost_usdt": s.total_slippage_cost_usdt,
            "total_cost_usdt": s.total_cost_usdt,
            "execution_score": s.execution_score,
        }
        if s.worst_fill:
            d["worst_fill"] = TransactionCostAnalyzer._rec_to_dict(s.worst_fill)
        return d
