"""Trading Competition strategy — generates buy/sell volume on a spot pair.

Repeatedly buys a fixed USDT amount of the asset, then immediately sells it back,
cycling at a configurable interval.  Designed for exchange trading competitions
where volume is rewarded.

State machine:
  idle  →  placing_buy  →  buying  →  placing_sell  →  selling  →  cooling  →  placing_buy …
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from core.types import (
    Exchange, OrderSide, OrderStatus, OrderType,
    TickerEvent, OrderUpdateEvent, Ticker,
)
from strategies.base import BaseStrategy


class TradingCompStrategy(BaseStrategy):
    """
    Params:
      exchange          str    connector (default "binance_spot")
      symbol            str    trading pair (default "BTC-USDT")
      order_usdt        float  USDT per buy/sell cycle (default 50.0)
      cycle_interval_s  float  seconds to wait between cycles (default 60.0)
      max_cycles        int    stop after N cycles, 0 = unlimited (default 0)
      qty_precision     int    quantity decimal places (default 5)
    """

    # Internal state labels
    _ST_IDLE         = "idle"
    _ST_PLACING_BUY  = "placing_buy"
    _ST_BUYING       = "buying"
    _ST_PLACING_SELL = "placing_sell"
    _ST_SELLING      = "selling"
    _ST_COOLING      = "cooling"
    _ST_DONE         = "done"

    def __init__(self, strategy_id: str, params: dict):
        defaults: dict = {
            "exchange":         "binance_spot",
            "symbol":           "BTC-USDT",
            "order_usdt":       50.0,
            "cycle_interval_s": 60.0,
            "max_cycles":       0,        # 0 = unlimited
            "qty_precision":    5,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        self._state: str = self._ST_IDLE
        self._last_ticker: Optional[Ticker] = None

        # Active order tracking
        self._buy_order_id: Optional[str] = None
        self._sell_order_id: Optional[str] = None
        self._bought_qty: Decimal = Decimal("0")
        self._buy_placed_ts: float = 0.0
        self._sell_placed_ts: float = 0.0

        # Cycle timing
        self._cool_start: float = 0.0

        # Stats
        self._cycles_completed: int = 0
        self._total_buy_volume: float = 0.0
        self._total_sell_volume: float = 0.0
        self._total_fees: float = 0.0
        self._last_cycle_pnl: float = 0.0
        self._last_buy_price: float = 0.0
        self._last_sell_price: float = 0.0

        self._place_task: Optional[asyncio.Task] = None

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _qty(self, price: Decimal) -> Decimal:
        usdt = Decimal(str(self.params["order_usdt"]))
        prec = int(self.params["qty_precision"])
        return (usdt / price).quantize(Decimal(10) ** -prec, rounding=ROUND_DOWN)

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list:
        t = event.ticker
        if t.exchange != self._exchange() or t.symbol != self._symbol():
            return []
        self._last_ticker = t

        if self._state == self._ST_IDLE:
            self._state = self._ST_PLACING_BUY
            self._place_task = asyncio.create_task(self._place_buy())

        elif self._state == self._ST_COOLING:
            elapsed = time.time() - self._cool_start
            if elapsed >= float(self.params["cycle_interval_s"]):
                self._state = self._ST_PLACING_BUY
                self._place_task = asyncio.create_task(self._place_buy())

        elif self._state == self._ST_BUYING and self._buy_placed_ts:
            if time.time() - self._buy_placed_ts > 300:
                self.logger.warning("[Comp] Buy order timeout — resetting to idle")
                self._buy_order_id = None
                self._buy_placed_ts = 0.0
                self._state = self._ST_IDLE

        elif self._state == self._ST_SELLING and self._sell_placed_ts:
            if time.time() - self._sell_placed_ts > 300:
                self.logger.warning("[Comp] Sell order timeout — resetting to cooling")
                self._sell_order_id = None
                self._sell_placed_ts = 0.0
                self._cool_start = time.time()
                self._state = self._ST_COOLING

        return []

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        order = event.order
        if order.exchange != self._exchange():
            return
        if order.strategy_id and order.strategy_id != self.strategy_id:
            return
        if order.status != OrderStatus.FILLED:
            return

        if self._state == self._ST_BUYING and order.order_id == self._buy_order_id:
            qty = order.filled_qty
            price = float(order.avg_price or order.price or 0)
            self._bought_qty = qty
            self._buy_placed_ts = 0.0
            self._total_buy_volume += float(qty) * price
            self._total_fees += float(order.fee)
            self._last_buy_price = price
            self.logger.info(
                f"[Comp] Buy filled @ {price:.4f} qty={float(qty):.5f} | "
                f"cycle #{self._cycles_completed + 1}"
            )
            self._state = self._ST_PLACING_SELL
            self._place_task = asyncio.create_task(self._place_sell(qty))

        elif self._state == self._ST_SELLING and order.order_id == self._sell_order_id:
            price = float(order.avg_price or order.price or 0)
            self._sell_placed_ts = 0.0
            self._total_sell_volume += float(order.filled_qty) * price
            self._total_fees += float(order.fee)
            self._last_sell_price = price
            self._last_cycle_pnl = (price - self._last_buy_price) * float(order.filled_qty) - float(order.fee)
            self._cycles_completed += 1
            self.logger.info(
                f"[Comp] Sell filled @ {price:.4f} | "
                f"cycle #{self._cycles_completed} done | "
                f"pnl={self._last_cycle_pnl:.4f} USDT"
            )
            max_c = int(self.params["max_cycles"])
            if max_c > 0 and self._cycles_completed >= max_c:
                self._state = self._ST_DONE
                self.disable()
                self.logger.info(f"[Comp] Max cycles ({max_c}) reached, stopping")
            else:
                self._cool_start = time.time()
                self._state = self._ST_COOLING

    # ── Order placement helpers ───────────────────────────────────────────────

    async def _place_buy(self) -> None:
        if not self.engine or not self._last_ticker:
            self._state = self._ST_IDLE
            return

        qty = self._qty(self._last_ticker.ask)
        if qty <= 0:
            self.logger.warning("[Comp] Computed buy qty <= 0, going idle")
            self._state = self._ST_IDLE
            return

        try:
            order = await self.engine.place_order(
                exchange=self._exchange(),
                symbol=self._symbol(),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=qty,
                strategy_id=self.strategy_id,
            )
            if order and order.order_id:
                self._buy_order_id = order.order_id
                self._buy_placed_ts = time.time()
                self._state = self._ST_BUYING
            else:
                self.logger.warning("[Comp] Buy order blocked (risk?), retrying after interval")
                self._cool_start = time.time()
                self._state = self._ST_COOLING
        except Exception as e:
            self.logger.error(f"[Comp] Buy placement error: {e}")
            self._cool_start = time.time()
            self._state = self._ST_COOLING

    async def _place_sell(self, qty: Decimal) -> None:
        if not self.engine:
            self._cool_start = time.time()
            self._state = self._ST_COOLING
            return

        try:
            order = await self.engine.place_order(
                exchange=self._exchange(),
                symbol=self._symbol(),
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=qty,
                strategy_id=self.strategy_id,
            )
            if order and order.order_id:
                self._sell_order_id = order.order_id
                self._sell_placed_ts = time.time()
                self._state = self._ST_SELLING
            else:
                self.logger.warning("[Comp] Sell order blocked, cooling before retry")
                self._cool_start = time.time()
                self._state = self._ST_COOLING
        except Exception as e:
            self.logger.error(f"[Comp] Sell placement error: {e}")
            self._cool_start = time.time()
            self._state = self._ST_COOLING

    def disable(self) -> None:
        super().disable()
        if self._place_task and not self._place_task.done():
            self._place_task.cancel()
        # Reset state so re-enabling starts fresh
        if self._state not in (self._ST_DONE,):
            self._state = self._ST_IDLE

    def enable(self) -> None:
        super().enable()
        if self._state == self._ST_DONE:
            self._state = self._ST_IDLE

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        total_volume = self._total_buy_volume + self._total_sell_volume
        interval = float(self.params["cycle_interval_s"])
        cool_remaining = max(0.0, interval - (time.time() - self._cool_start)) if self._state == self._ST_COOLING else 0.0
        return {
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "params": self.params,
            "state": self._state,
            "cycles_completed": self._cycles_completed,
            "total_volume_usdt": round(total_volume, 2),
            "total_fees_usdt": round(self._total_fees, 4),
            "last_buy_price": self._last_buy_price or None,
            "last_sell_price": self._last_sell_price or None,
            "last_cycle_pnl": round(self._last_cycle_pnl, 6),
            "cool_remaining_s": round(cool_remaining, 1),
            **self._pnl_status(),
        }
