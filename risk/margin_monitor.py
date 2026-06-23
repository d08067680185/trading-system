"""
Margin & liquidation price monitor.

Runs as a background task, polling open positions every `interval_s` seconds.
Fires Telegram alerts when safety margin drops below warn/critical thresholds.
Auto-reduces position when critically close to liquidation.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("MarginMonitor")


@dataclass
class MarginStatus:
    exchange: str
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    liq_price: float
    unrealized_pnl: float
    leverage: int
    safety_pct: float        # distance to liq as % of mark price
    margin_ratio: float      # unrealized_pnl / notional


class MarginMonitor:
    def __init__(
        self,
        engine: "TradingEngine",
        warn_safety_pct: float = 15.0,    # warn when < 15% from liq price
        critical_safety_pct: float = 8.0, # auto-reduce when < 8%
        interval_s: int = 30,
    ):
        self._engine = engine
        self._warn_pct = warn_safety_pct
        self._critical_pct = critical_safety_pct
        self._interval = interval_s
        self._task: Optional[asyncio.Task] = None
        self._last_alerts: dict[str, float] = {}  # key → last alert ts (throttle)
        self._alert_cooldown = 300  # 5 min between repeat alerts
        self._statuses: list[MarginStatus] = []

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("MarginMonitor started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_statuses(self) -> list[dict]:
        return [
            {
                "exchange": s.exchange, "symbol": s.symbol, "side": s.side,
                "size": s.size, "entry_price": s.entry_price,
                "mark_price": s.mark_price, "liq_price": s.liq_price,
                "unrealized_pnl": round(s.unrealized_pnl, 4),
                "leverage": s.leverage,
                "safety_pct": round(s.safety_pct, 2),
                "margin_ratio": round(s.margin_ratio, 4),
                "alert_level": self._alert_level(s),
            }
            for s in self._statuses
        ]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"MarginMonitor error: {e}")
            await asyncio.sleep(self._interval)

    async def _check(self) -> None:
        positions = await self._engine.get_positions()
        statuses = []
        for pos in positions:
            if pos.liquidation_price <= 0 or pos.mark_price <= 0:
                continue
            liq  = float(pos.liquidation_price)
            mark = float(pos.mark_price)
            # Safety % = distance from mark to liq as fraction of mark
            safety_pct = abs(mark - liq) / mark * 100.0
            notional = float(pos.notional) if pos.notional else mark * float(pos.size)
            margin_ratio = float(pos.unrealized_pnl) / notional if notional > 0 else 0.0

            status = MarginStatus(
                exchange=pos.exchange.value,
                symbol=pos.symbol,
                side=pos.side.value,
                size=float(pos.size),
                entry_price=float(pos.entry_price),
                mark_price=mark,
                liq_price=liq,
                unrealized_pnl=float(pos.unrealized_pnl),
                leverage=pos.leverage,
                safety_pct=safety_pct,
                margin_ratio=margin_ratio,
            )
            statuses.append(status)
            await self._handle_status(status)
        self._statuses = statuses

    async def _handle_status(self, status: MarginStatus) -> None:
        key = f"{status.exchange}:{status.symbol}"
        now = time.time()
        notifier = getattr(self._engine, "_notifier", None)

        if status.safety_pct < self._critical_pct:
            # Auto-reduce: close 50% of position
            logger.error(
                f"CRITICAL margin [{key}]: safety={status.safety_pct:.1f}% < "
                f"{self._critical_pct}% — auto-reducing position"
            )
            if self._should_alert(key + ":critical", now):
                if notifier:
                    asyncio.create_task(notifier.send(
                        f"🚨 CRITICAL MARGIN {key}\n"
                        f"Safety: {status.safety_pct:.1f}% (liq @ {status.liq_price:.2f})\n"
                        f"Auto-reducing 50% of position"
                    ))
                await self._auto_reduce(status, fraction=0.5)

        elif status.safety_pct < self._warn_pct:
            logger.warning(
                f"Low margin [{key}]: safety={status.safety_pct:.1f}% < {self._warn_pct}%"
            )
            if self._should_alert(key + ":warn", now):
                if notifier:
                    asyncio.create_task(notifier.send(
                        f"⚠️ LOW MARGIN {key}\n"
                        f"Safety: {status.safety_pct:.1f}% | Liq: {status.liq_price:.2f}\n"
                        f"Mark: {status.mark_price:.2f} | PnL: {status.unrealized_pnl:+.2f}"
                    ))

    async def _auto_reduce(self, status: MarginStatus, fraction: float = 0.5) -> None:
        from core.types import Exchange, OrderSide, OrderType
        from decimal import Decimal
        try:
            ex = Exchange(status.exchange)
            reduce_qty = Decimal(str(status.size * fraction))
            # Opposite side to close
            close_side = OrderSide.SELL if status.side == "long" else OrderSide.BUY
            order = await self._engine.place_order(
                exchange=ex,
                symbol=status.symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                quantity=reduce_qty,
                reduce_only=True,
                strategy_id="margin_monitor",
            )
            if order:
                logger.info(f"Auto-reduced {status.symbol} by {fraction:.0%}: order {order.order_id}")
        except Exception as e:
            logger.error(f"Auto-reduce failed [{status.exchange}:{status.symbol}]: {e}")

    def _should_alert(self, key: str, now: float) -> bool:
        last = self._last_alerts.get(key, 0)
        if now - last >= self._alert_cooldown:
            self._last_alerts[key] = now
            return True
        return False

    def _alert_level(self, s: MarginStatus) -> str:
        if s.safety_pct < self._critical_pct:
            return "critical"
        if s.safety_pct < self._warn_pct:
            return "warning"
        return "ok"
