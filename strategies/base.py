from __future__ import annotations
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from core.types import (
    TickerEvent, OrderBookEvent, OrderUpdateEvent,
    PositionUpdateEvent, BalanceUpdateEvent, Signal,
)

if TYPE_CHECKING:
    from core.engine import TradingEngine


class BaseStrategy(ABC):
    # Strategies that intentionally keep limit orders resting (grid, market maker)
    # set this True so the engine's stale-order reaper leaves their orders alone.
    keeps_resting_orders: bool = False

    def __init__(self, strategy_id: str, params: dict[str, Any]):
        self.strategy_id = strategy_id
        self.params = params
        self.engine: TradingEngine | None = None
        self.logger = logging.getLogger(f"strategy.{strategy_id}")
        self._enabled = True
        self._realized_pnl: float = 0.0
        self._trade_count: int = 0
        self._start_time: float = time.time()
        self._halted: bool = False
        self._halt_reason: str = ""
        self._paused: bool = False
        self._consecutive_losses: int = 0   # reset on profitable trade
        self._last_trade_pnl: float = 0.0
        # FIFO inventory per symbol: {'long': deque([(qty, price),...]), 'short': deque(...)}
        self._inventory: dict = defaultdict(lambda: {"long": deque(), "short": deque()})

    def set_engine(self, engine: TradingEngine) -> None:
        self.engine = engine

    def _is_backtest(self) -> bool:
        """True when running under the backtest replay driver (BacktestRunner sets
        `engine.is_backtest`). Lets a strategy disable wall-clock safety timers
        that are meaningless in deterministic, faster-than-real-time replay."""
        return bool(getattr(self.engine, "is_backtest", False))

    def _now(self) -> float:
        """Current time: wall-clock live, replayed bar time in backtest. Use this
        instead of time.time() for cooldowns/timeouts so a strategy paces itself
        correctly under faster-than-real-time replay. Falls back to wall-clock if
        the engine is absent or a test stub doesn't implement now()."""
        import time as _t
        now_fn = getattr(self.engine, "now", None) if self.engine is not None else None
        return now_fn() if callable(now_fn) else _t.time()

    def regime_pos_mult(self, symbol: str) -> float:
        """Return position size multiplier from current market regime (1.0 if unavailable)."""
        if self.engine and self.engine.regime_detector:
            return self.engine.regime_detector.pos_size_mult(symbol)
        return 1.0

    def regime_threshold_mult(self, symbol: str) -> float:
        """Return threshold multiplier from current market regime (1.0 if unavailable)."""
        if self.engine and self.engine.regime_detector:
            return self.engine.regime_detector.threshold_mult(symbol)
        return 1.0

    def enable(self) -> None:
        self._enabled = True
        self.logger.info(f"{self.strategy_id} enabled")

    def disable(self) -> None:
        self._enabled = False
        self.logger.info(f"{self.strategy_id} disabled")

    def update_params(self, new_params: dict[str, Any]) -> None:
        self.params.update(new_params)
        self.logger.info(f"{self.strategy_id} params updated: {new_params}")
        self.on_params_updated(new_params)

    def on_params_updated(self, changed: dict[str, Any]) -> None:
        """Override to react to hot-reloaded params."""

    def record_fill(self, order) -> None:
        """Called by engine when one of this strategy's orders is filled."""
        self._trade_count += 1
        _pnl_before = self._realized_pnl   # snapshot before FIFO updates it
        try:
            qty = float(order.filled_qty or order.quantity or 0)
            price = float(order.avg_price or order.price or 0)
            if qty > 0 and price > 0:
                inv = self._inventory[order.symbol]
                remaining = qty
                if order.side.value.lower() == "buy":
                    # Close short lots FIFO, remainder opens long
                    while remaining > 0 and inv["short"]:
                        lot_qty, lot_price = inv["short"][0]
                        closed = min(lot_qty, remaining)
                        self._realized_pnl += (lot_price - price) * closed
                        remaining -= closed
                        if closed < lot_qty:
                            inv["short"][0] = (lot_qty - closed, lot_price)
                        else:
                            inv["short"].popleft()
                    if remaining > 0:
                        inv["long"].append((remaining, price))
                else:
                    # Close long lots FIFO, remainder opens short
                    while remaining > 0 and inv["long"]:
                        lot_qty, lot_price = inv["long"][0]
                        closed = min(lot_qty, remaining)
                        self._realized_pnl += (price - lot_price) * closed
                        remaining -= closed
                        if closed < lot_qty:
                            inv["long"][0] = (lot_qty - closed, lot_price)
                        else:
                            inv["long"].popleft()
                    if remaining > 0:
                        inv["short"].append((remaining, price))
        except Exception as e:
            self.logger.warning(f"record_fill PnL error: {e}")

        # Per-strategy circuit breakers
        max_loss = float(self.params.get("max_loss_usdt", 0))
        if max_loss > 0 and self._realized_pnl < -max_loss and not self._halted:
            self._halted = True
            self._halt_reason = (
                f"realized PnL {self._realized_pnl:.2f} USDT exceeded limit -{max_loss:.2f} USDT"
            )
            self._enabled = False
            self.logger.warning(f"[{self.strategy_id}] Circuit breaker triggered: {self._halt_reason}")

        # Consecutive loss circuit breaker
        max_consec = int(self.params.get("max_consecutive_losses", 0))
        if max_consec > 0 and not self._halted:
            trade_pnl = self._realized_pnl - _pnl_before
            if trade_pnl < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= max_consec:
                    self._halted = True
                    self._halt_reason = (
                        f"{self._consecutive_losses} consecutive losses — last trade PnL {trade_pnl:.4f} USDT"
                    )
                    self._enabled = False
                    self.logger.warning(f"[{self.strategy_id}] Consecutive loss limit: {self._halt_reason}")
            else:
                self._consecutive_losses = 0  # reset on profitable trade

    def _pnl_status(self) -> dict:
        uptime_h = (time.time() - self._start_time) / 3600
        return {
            "realized_pnl_usdt": round(self._realized_pnl, 4),
            "trade_count": self._trade_count,
            "uptime_h": round(uptime_h, 1),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "consecutive_losses": self._consecutive_losses,
        }

    # ── Event dispatch ────────────────────────────────────────────────────────

    async def on_event(self, event: object) -> list[Signal]:
        if not self._enabled:
            return []
        if self._paused:
            return []
        if isinstance(event, TickerEvent):
            return await self.on_ticker(event) or []
        if isinstance(event, OrderBookEvent):
            return await self.on_orderbook(event) or []
        if isinstance(event, OrderUpdateEvent):
            await self.on_order_update(event)
        if isinstance(event, PositionUpdateEvent):
            await self.on_position_update(event)
        if isinstance(event, BalanceUpdateEvent):
            await self.on_balance_update(event)
        return []

    # ── Override these ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        return []

    async def on_orderbook(self, event: OrderBookEvent) -> list[Signal]:
        return []

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        pass

    async def on_position_update(self, event: PositionUpdateEvent) -> None:
        pass

    async def on_balance_update(self, event: BalanceUpdateEvent) -> None:
        pass

    @abstractmethod
    def get_status(self) -> dict: ...
