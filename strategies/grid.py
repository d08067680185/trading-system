"""Spot Grid Trading strategy.

Divides a price range into N equal intervals and places limit buy orders below
the current price and limit sell orders above it.  When a buy fills, a sell is
placed at the level above; when a sell fills, a buy is placed at the level below.
This captures the spread between adjacent grid levels.

Works on spot or futures.  For spot use exchange = "binance_spot" / "okx_spot".
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Signal, TickerEvent, OrderUpdateEvent, Ticker,
)
from strategies.base import BaseStrategy


class SpotGridStrategy(BaseStrategy):
    """
    Params:
      exchange        str    connector to use, e.g. "binance_spot" (default "binance_spot")
      symbol          str    trading pair, e.g. "BTC-USDT"  (default "BTC-USDT")
      grid_low        float  lower price bound of the grid
      grid_high       float  upper price bound of the grid
      grid_levels     int    number of grid intervals (min 2, default 10)
      order_usdt      float  USDT value per grid order (default 100)
      qty_precision   int    decimal places for quantity rounding (default 4)
      price_precision int    decimal places for price rounding (default 2)
    """

    keeps_resting_orders = True  # grid levels rest indefinitely — never reap

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "exchange": "binance_spot",
            "symbol": "BTC-USDT",
            "grid_low": 0.0,       # must be set by user
            "grid_high": 0.0,      # must be set by user
            "grid_levels": 10,
            "order_usdt": 100.0,
            "qty_precision": 4,
            "price_precision": 2,
            "max_inventory_usdt": 0.0,   # 0 = disabled; pause buys if net long > this
            "trailing_grid": False,      # shift grid range to follow price when hitting boundary
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        # price_level → order_id (buy orders pending below market)
        self._buy_orders: dict[Decimal, str] = {}
        # price_level → order_id (sell orders pending above market)
        self._sell_orders: dict[Decimal, str] = {}
        # All live order IDs we placed → (side, price_level)
        self._order_map: dict[str, tuple[OrderSide, Decimal]] = {}

        self._grid_prices: list[Decimal] = []
        self._last_ticker: Optional[Ticker] = None
        self._initialized = False
        self._setup_task: Optional[asyncio.Task] = None
        self._total_buy_fills = 0
        self._total_sell_fills = 0

        # Runaway-cascade guard: if a buy/sell fill immediately triggers the
        # opposite order's fill (e.g. a 2-level grid, or a connector that
        # reports synthetic instant fills), on_order_update can recurse into
        # itself forever — this previously wrote >400k log lines in ~10s.
        # Trip the breaker if too many fills land in too short a window.
        self._fill_times: deque = deque(maxlen=20)
        self._cascade_limit_s = 1.0
        self._runaway_tripped = False

        # Inventory tracking: net long position in base asset
        self._net_qty: Decimal = Decimal("0")      # positive = long
        self._net_cost: Decimal = Decimal("0")     # cumulative cost basis

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _build_grid(self) -> list[Decimal]:
        low = Decimal(str(self.params["grid_low"]))
        high = Decimal(str(self.params["grid_high"]))
        levels = int(self.params["grid_levels"])
        if low <= 0 or high <= low or levels < 2:
            return []
        step = (high - low) / levels
        prec = self.params["price_precision"]
        quant = Decimal(10) ** -prec
        return [
            (low + step * i).quantize(quant, rounding=ROUND_DOWN)
            for i in range(levels + 1)
        ]

    def _qty(self, price: Decimal) -> Decimal:
        usdt = Decimal(str(self.params["order_usdt"]))
        prec = self.params["qty_precision"]
        quant = Decimal(10) ** -prec
        return (usdt / price).quantize(quant, rounding=ROUND_DOWN)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_engine(self, engine) -> None:
        super().set_engine(engine)

    def enable(self) -> None:
        super().enable()
        # Re-initialize grid when re-enabled
        self._initialized = False
        self._runaway_tripped = False
        self._fill_times.clear()

    def disable(self) -> None:
        super().disable()
        if self._setup_task and not self._setup_task.done():
            self._setup_task.cancel()

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        t = event.ticker
        if t.exchange != self._exchange() or t.symbol != self._symbol():
            return []
        self._last_ticker = t

        if not self._initialized and self._enabled:
            low = self.params["grid_low"]
            high = self.params["grid_high"]
            if low > 0 and high > low:
                self._initialized = True
                if self._setup_task is None or self._setup_task.done():
                    self._setup_task = asyncio.create_task(self._setup_grid())
        return []

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        if self._runaway_tripped:
            return

        order = event.order
        if order.exchange != self._exchange():
            return
        if order.strategy_id and order.strategy_id != self.strategy_id:
            return
        if order.order_id not in self._order_map:
            return
        if order.status != OrderStatus.FILLED:
            return

        # Runaway-cascade guard (see __init__ comment): a fill that immediately
        # re-triggers the opposite order's fill can recurse indefinitely. This is
        # a wall-clock protection against a live connector reporting synthetic
        # instant fills — meaningless under deterministic backtest replay (where
        # many distinct levels legitimately fill in the same wall-clock instant),
        # so it is disabled there.
        now = time.time()
        self._fill_times.append(now)
        if not self._is_backtest() and \
                len(self._fill_times) == self._fill_times.maxlen and \
                now - self._fill_times[0] < self._cascade_limit_s:
            self._runaway_tripped = True
            self.logger.error(
                f"[Grid] Runaway fill cascade detected "
                f"({self._fill_times.maxlen} fills in "
                f"{now - self._fill_times[0]:.3f}s) — pausing strategy. "
                f"Re-enable via strategy params reset after checking grid_low/"
                f"grid_high/grid_levels for a too-narrow range."
            )
            self.disable()
            return

        side, level = self._order_map.pop(order.order_id, (None, None))
        if side is None or level is None:
            return

        grid = self._grid_prices
        if not grid:
            return

        fill_price = order.avg_price or order.price or level
        fill_qty   = order.filled_qty or self._qty(level)

        if side == OrderSide.BUY:
            self._buy_orders.pop(level, None)
            self._total_buy_fills += 1
            # Update inventory
            self._net_qty  += fill_qty
            self._net_cost += fill_qty * fill_price
            self.logger.info(
                f"[Grid] Buy filled @ {level} qty={fill_qty} "
                f"net_qty={self._net_qty:.4f} — placing sell at next level up"
            )
            await self._place_sell_above(level, grid)

        elif side == OrderSide.SELL:
            self._sell_orders.pop(level, None)
            self._total_sell_fills += 1
            # Update inventory
            self._net_qty  -= fill_qty
            self._net_cost -= fill_qty * fill_price
            self.logger.info(
                f"[Grid] Sell filled @ {level} qty={fill_qty} "
                f"net_qty={self._net_qty:.4f} — placing buy at next level down"
            )
            # Check if inventory is within limits before placing new buy
            max_inv = Decimal(str(self.params.get("max_inventory_usdt", 0)))
            if max_inv > 0 and fill_price and self._net_qty * fill_price >= max_inv:
                self.logger.warning(
                    f"[Grid] Inventory limit {max_inv} USDT reached "
                    f"(net={float(self._net_qty * fill_price):.2f}) — skipping buy replenish"
                )
            else:
                await self._place_buy_below(level, grid)

    # ── Grid management ───────────────────────────────────────────────────────

    async def _setup_grid(self) -> None:
        """Cancel all existing orders and place fresh grid around current price."""
        if not self.engine or not self._last_ticker:
            return

        grid = self._build_grid()
        if not grid:
            self.logger.error("[Grid] Invalid grid params — cannot build grid")
            return

        self._grid_prices = grid
        self._buy_orders.clear()
        self._sell_orders.clear()
        self._order_map.clear()

        # Cancel all open orders first. `self.engine` is the live TradingEngine
        # in production but a lightweight _Proxy in backtests (no `connectors`
        # attr, nothing to cancel) — use getattr so that's a silent no-op
        # instead of a logged warning every grid (re)setup.
        connectors = getattr(self.engine, "connectors", None)
        if connectors is not None:
            try:
                connector = connectors.get(self._exchange())
                if connector:
                    await connector.cancel_all_orders(self._symbol())
            except Exception as e:
                self.logger.warning(f"[Grid] cancel_all_orders failed: {e}")

        mid = self._last_ticker.mid
        ex = self._exchange()
        sym = self._symbol()

        self.logger.info(
            f"[Grid] Setting up {len(grid)-1} levels "
            f"[{grid[0]}..{grid[-1]}] mid={mid:.2f}"
        )

        for price in grid:
            if price < mid:
                # Buy order below market
                qty = self._qty(price)
                if qty <= 0:
                    continue
                try:
                    order = await self.engine.place_order(
                        exchange=ex, symbol=sym,
                        side=OrderSide.BUY, order_type=OrderType.LIMIT,
                        quantity=qty, price=price,
                        strategy_id=self.strategy_id,
                    )
                    if order and order.order_id:
                        self._buy_orders[price] = order.order_id
                        self._order_map[order.order_id] = (OrderSide.BUY, price)
                except Exception as e:
                    self.logger.warning(f"[Grid] Buy order at {price} failed: {e}")
                # Yield only — REST pacing is handled by the connector's rate limiter,
                # and a wall-clock sleep here outruns the backtest candle loop.
                await asyncio.sleep(0)

            elif price > mid:
                # Sell order above market
                qty = self._qty(price)
                if qty <= 0:
                    continue
                try:
                    order = await self.engine.place_order(
                        exchange=ex, symbol=sym,
                        side=OrderSide.SELL, order_type=OrderType.LIMIT,
                        quantity=qty, price=price,
                        strategy_id=self.strategy_id,
                    )
                    if order and order.order_id:
                        self._sell_orders[price] = order.order_id
                        self._order_map[order.order_id] = (OrderSide.SELL, price)
                except Exception as e:
                    self.logger.warning(f"[Grid] Sell order at {price} failed: {e}")
                await asyncio.sleep(0)

        self.logger.info(
            f"[Grid] Placed {len(self._buy_orders)} buys, {len(self._sell_orders)} sells"
        )

    @staticmethod
    def _grid_index(level: Decimal, grid: list[Decimal]) -> int:
        """Safe index lookup — tolerates float rounding by finding closest match."""
        tolerance = Decimal("0.0001")
        for i, p in enumerate(grid):
            if abs(p - level) <= tolerance:
                return i
        return -1

    async def _place_sell_above(self, filled_buy_level: Decimal, grid: list[Decimal]) -> None:
        idx = self._grid_index(filled_buy_level, grid)
        if idx < 0 or idx + 1 >= len(grid):
            self.logger.warning(f"[Grid] Buy fill at {filled_buy_level} is outside grid range — no sell placed")
            return
        sell_price = grid[idx + 1]
        if sell_price in self._sell_orders:
            return  # already placed
        qty = self._qty(sell_price)
        if qty <= 0:
            return
        try:
            order = await self.engine.place_order(
                exchange=self._exchange(), symbol=self._symbol(),
                side=OrderSide.SELL, order_type=OrderType.LIMIT,
                quantity=qty, price=sell_price,
                strategy_id=self.strategy_id,
            )
            if order and order.order_id:
                self._sell_orders[sell_price] = order.order_id
                self._order_map[order.order_id] = (OrderSide.SELL, sell_price)
        except Exception as e:
            self.logger.warning(f"[Grid] Sell replenish at {sell_price} failed: {e}")

    async def _place_buy_below(self, filled_sell_level: Decimal, grid: list[Decimal]) -> None:
        idx = self._grid_index(filled_sell_level, grid)
        if idx < 0 or idx - 1 < 0:
            self.logger.warning(f"[Grid] Sell fill at {filled_sell_level} is outside grid range — no buy placed")
            return
        buy_price = grid[idx - 1]
        if buy_price in self._buy_orders:
            return  # already placed
        qty = self._qty(buy_price)
        if qty <= 0:
            return
        try:
            order = await self.engine.place_order(
                exchange=self._exchange(), symbol=self._symbol(),
                side=OrderSide.BUY, order_type=OrderType.LIMIT,
                quantity=qty, price=buy_price,
                strategy_id=self.strategy_id,
            )
            if order and order.order_id:
                self._buy_orders[buy_price] = order.order_id
                self._order_map[order.order_id] = (OrderSide.BUY, buy_price)
        except Exception as e:
            self.logger.warning(f"[Grid] Buy replenish at {buy_price} failed: {e}")

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        mid = float(self._last_ticker.mid) if self._last_ticker else None
        return {
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "params": self.params,
            "initialized": self._initialized,
            "current_price": mid,
            "grid_range": (
                f"{self._grid_prices[0]}–{self._grid_prices[-1]}"
                if self._grid_prices else "not built"
            ),
            "active_buy_orders": len(self._buy_orders),
            "active_sell_orders": len(self._sell_orders),
            "total_buy_fills": self._total_buy_fills,
            "total_sell_fills": self._total_sell_fills,
        }
