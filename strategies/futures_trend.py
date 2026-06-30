"""Futures trend-following strategy.

Uses fast/slow moving average crossover to go long or short on perpetual futures.
Supports Binance USDT-M (exchange="binance") and OKX swap (exchange="okx").
Tracks entry price and applies stop-loss / take-profit via market close orders.
"""
from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
from typing import Optional

from core.types import Exchange, OrderSide, OrderType, TickerEvent, OrderUpdateEvent
from strategies.base import BaseStrategy


class FuturesTrendStrategy(BaseStrategy):
    """
    Params:
      exchange        str    "binance" (USDT-M) or "okx" (swap)   default "binance"
      symbol          str    trading pair e.g. "BTC-USDT"           default "BTC-USDT"
      fast_period     int    fast MA window (price ticks)            default 10
      slow_period     int    slow MA window (price ticks)            default 30
      position_usdt   float  notional per trade in USDT              default 50
      stop_loss_pct   float  stop loss %  (e.g. 2.0 = 2%)           default 2.0
      take_profit_pct float  take profit % (e.g. 4.0 = 4%)          default 4.0
      direction       str    "both" | "long_only" | "short_only"     default "both"
      cooldown_s      float  min seconds between signals             default 60
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "exchange": "binance",
            "symbol": "BTC-USDT",
            "fast_period": 10,
            "slow_period": 30,
            "position_usdt": 50.0,
            "stop_loss_pct": 2.0,
            "take_profit_pct": 4.0,
            "direction": "both",
            "cooldown_s": 60.0,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        slow = int(self.params["slow_period"])
        self._prices: deque = deque(maxlen=slow + 2)
        self._prev_fast_ma: Optional[float] = None
        self._prev_slow_ma: Optional[float] = None

        self._position_side: Optional[str] = None   # "long" | "short" | None
        self._entry_price: Optional[float] = None
        self._last_signal_t: float = 0.0
        self._total_trades: int = 0
        self._last_price: float = 0.0

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _ma(self, period: int) -> Optional[float]:
        prices = list(self._prices)
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    async def on_ticker(self, event: TickerEvent) -> list:
        if self._halted:
            return []
        t = event.ticker
        if t.exchange != self._exchange() or t.symbol != self._symbol():
            return []

        mid = float(t.mid)
        self._last_price = mid
        self._prices.append(mid)

        fast_p = int(self.params["fast_period"])
        slow_p = int(self.params["slow_period"])

        fast_ma = self._ma(fast_p)
        slow_ma = self._ma(slow_p)
        if fast_ma is None or slow_ma is None:
            return []

        # Check stop-loss / take-profit on open position
        if self._position_side and self._entry_price:
            ep = self._entry_price
            sl = float(self.params["stop_loss_pct"]) / 100
            tp = float(self.params["take_profit_pct"]) / 100
            if self._position_side == "long":
                if mid <= ep * (1 - sl):
                    await self._close_position("stop_loss")
                    return []
                if mid >= ep * (1 + tp):
                    await self._close_position("take_profit")
                    return []
            else:
                if mid >= ep * (1 + sl):
                    await self._close_position("stop_loss")
                    return []
                if mid <= ep * (1 - tp):
                    await self._close_position("take_profit")
                    return []

        # Cooldown guard
        now = self._now()
        cooldown = float(self.params.get("cooldown_s", 60))
        if not self._is_backtest() and (now - self._last_signal_t) < cooldown:
            return []

        # Crossover detection requires a previous tick
        if self._prev_fast_ma is None:
            self._prev_fast_ma = fast_ma
            self._prev_slow_ma = slow_ma
            return []

        prev_fast, prev_slow = self._prev_fast_ma, self._prev_slow_ma
        self._prev_fast_ma = fast_ma
        self._prev_slow_ma = slow_ma

        direction = self.params.get("direction", "both")

        # Golden cross → long
        if prev_fast <= prev_slow and fast_ma > slow_ma:
            if direction in ("both", "long_only") and self._position_side != "long":
                if self._position_side == "short":
                    await self._close_position("reverse")
                await self._open_position("long", mid)
                self._last_signal_t = now

        # Death cross → short
        elif prev_fast >= prev_slow and fast_ma < slow_ma:
            if direction in ("both", "short_only") and self._position_side != "short":
                if self._position_side == "long":
                    await self._close_position("reverse")
                await self._open_position("short", mid)
                self._last_signal_t = now

        return []

    async def _open_position(self, side: str, price: float) -> None:
        if not self.engine:
            return
        usdt = float(self.params["position_usdt"])
        qty = Decimal(str(round(usdt / price, 6)))
        if qty <= 0:
            return
        order_side = OrderSide.BUY if side == "long" else OrderSide.SELL
        try:
            order = await self.engine.place_order(
                exchange=self._exchange(), symbol=self._symbol(),
                side=order_side, order_type=OrderType.MARKET,
                quantity=qty, strategy_id=self.strategy_id,
            )
            if order:
                self._position_side = side
                self._entry_price = price
                self._total_trades += 1
                self.logger.info(
                    f"[FuturesTrend] Opened {side} qty={qty} @{price:.2f} "
                    f"fast={self._prev_fast_ma:.2f} slow={self._prev_slow_ma:.2f}"
                )
        except Exception as e:
            self.logger.warning(f"[FuturesTrend] Open {side} failed: {e}")

    async def _close_position(self, reason: str) -> None:
        if not self.engine or not self._position_side:
            return
        price = self._entry_price or self._last_price or 1
        usdt = float(self.params["position_usdt"])
        qty = Decimal(str(round(usdt / price, 6)))
        if qty <= 0:
            return
        close_side = OrderSide.SELL if self._position_side == "long" else OrderSide.BUY
        prev_side = self._position_side
        self._position_side = None
        self._entry_price = None
        try:
            await self.engine.place_order(
                exchange=self._exchange(), symbol=self._symbol(),
                side=close_side, order_type=OrderType.MARKET,
                quantity=qty, reduce_only=True,
                strategy_id=self.strategy_id,
            )
            self.logger.info(f"[FuturesTrend] Closed {prev_side} reason={reason}")
        except Exception as e:
            self.logger.warning(f"[FuturesTrend] Close {prev_side} failed: {e}")
            # Restore state so we don't lose track
            self._position_side = prev_side

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        pass  # position state is managed purely by local tracking

    def on_params_updated(self, changed: dict) -> None:
        slow = int(self.params["slow_period"])
        self._prices = deque(list(self._prices), maxlen=slow + 2)
        self._prev_fast_ma = None
        self._prev_slow_ma = None

    def get_status(self) -> dict:
        fast_ma = self._ma(int(self.params["fast_period"]))
        slow_ma = self._ma(int(self.params["slow_period"]))
        slow_p = int(self.params["slow_period"])
        return {
            **self._pnl_status(),
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "exchange": self.params["exchange"],
            "symbol": self.params["symbol"],
            "position_side": self._position_side,
            "entry_price": round(self._entry_price, 2) if self._entry_price else None,
            "last_price": round(self._last_price, 2) if self._last_price else None,
            "fast_ma": round(fast_ma, 2) if fast_ma else None,
            "slow_ma": round(slow_ma, 2) if slow_ma else None,
            "total_trades": self._total_trades,
            "price_samples": len(self._prices),
            "price_samples_needed": slow_p + 2,
        }
