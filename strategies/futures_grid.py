"""Futures grid trading strategy.

Places a grid of limit orders on perpetual futures.  When a buy fills an equal
sell is placed one level above; when a sell fills an equal buy is placed one
level below.  Three modes:
  neutral — buy orders below current price (open long), sell orders above
             (open short); each fill triggers a reduce_only close on the
             opposite side at the adjacent level.
  long    — only buy orders below current price; fills trigger reduce_only
             sells at the next level up (harvest upside).
  short   — only sell orders above current price; fills trigger reduce_only
             buys at the next level down (harvest downside).

Supports Binance USDT-M (exchange="binance") and OKX swap (exchange="okx").
Set grid_low=0 to leave the strategy inactive until params are configured.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from core.types import Exchange, OrderSide, OrderStatus, OrderType, TickerEvent, OrderUpdateEvent
from strategies.base import BaseStrategy


class FuturesGridStrategy(BaseStrategy):
    """
    Params:
      exchange       str    "binance" (USDT-M) or "okx" (swap)         default "binance"
      symbol         str    trading pair e.g. "BTC-USDT"                default "BTC-USDT"
      grid_low       float  lower price bound  (0 = inactive)           default 0
      grid_high      float  upper price bound  (0 = inactive)           default 0
      grid_count     int    number of grid intervals (min 2)            default 10
      grid_usdt      float  USDT notional per grid order                default 30
      mode           str    "neutral" | "long" | "short"                default "neutral"
      qty_precision  int    quantity decimal places                      default 6
      price_precision int   price decimal places                        default 2
    """

    keeps_resting_orders = True   # never let the engine reap grid limit orders

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "exchange": "binance",
            "symbol": "BTC-USDT",
            "grid_low": 0.0,
            "grid_high": 0.0,
            "grid_count": 10,
            "grid_usdt": 30.0,
            "mode": "neutral",
            "qty_precision": 6,
            "price_precision": 2,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        # price_level → order_id
        self._open_orders: dict[Decimal, str] = {}
        # order_id → (side, price_level, is_close)
        self._order_map: dict[str, tuple[OrderSide, Decimal, bool]] = {}

        self._grid_prices: list[Decimal] = []
        self._last_ticker: Optional[object] = None
        self._initialized = False
        self._setup_task: Optional[asyncio.Task] = None
        self._total_buy_fills = 0
        self._total_sell_fills = 0

        # Runaway fill-cascade guard (same pattern as SpotGrid)
        self._fill_times: deque = deque(maxlen=20)
        self._cascade_limit_s = 1.0
        self._runaway_tripped = False

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _qty(self, price: Decimal) -> Decimal:
        usdt = Decimal(str(self.params["grid_usdt"]))
        prec = int(self.params.get("qty_precision", 6))
        q = (usdt / price).quantize(Decimal(10) ** -prec, rounding=ROUND_DOWN)
        return q if q > 0 else Decimal("0")

    def _round_price(self, price: Decimal) -> Decimal:
        prec = int(self.params.get("price_precision", 2))
        return price.quantize(Decimal(10) ** -prec, rounding=ROUND_DOWN)

    def _build_grid(self) -> list[Decimal]:
        low = Decimal(str(self.params["grid_low"]))
        high = Decimal(str(self.params["grid_high"]))
        n = int(self.params["grid_count"])
        if low <= 0 or high <= low or n < 2:
            return []
        step = (high - low) / n
        prec = int(self.params.get("price_precision", 2))
        prices = [
            (low + step * i).quantize(Decimal(10) ** -prec, rounding=ROUND_DOWN)
            for i in range(n + 1)
        ]
        return prices

    async def on_ticker(self, event: TickerEvent) -> list:
        if self._halted:
            return []
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

        now = time.time()
        self._fill_times.append(now)
        if not self._is_backtest() and \
                len(self._fill_times) == self._fill_times.maxlen and \
                now - self._fill_times[0] < self._cascade_limit_s:
            self._runaway_tripped = True
            self.logger.error("[FuturesGrid] Runaway fill cascade — pausing")
            self.disable()
            return

        side, level, is_close = self._order_map.pop(order.order_id, (None, None, None))
        if side is None:
            return

        self._open_orders.pop(level, None)
        grid = self._grid_prices
        if not grid:
            return

        mode = self.params.get("mode", "neutral")
        fill_qty = order.filled_qty or self._qty(level)

        if side == OrderSide.BUY:
            self._total_buy_fills += 1
            self.logger.info(f"[FuturesGrid] Buy filled @ {level} qty={fill_qty} close={is_close}")
            if is_close:
                # close short → re-place short at this level
                await self._place_order(level, OrderSide.SELL, is_close=False)
            else:
                # opened long → place close sell above
                await self._place_close_above(level, grid, fill_qty)
        else:
            self._total_sell_fills += 1
            self.logger.info(f"[FuturesGrid] Sell filled @ {level} qty={fill_qty} close={is_close}")
            if is_close:
                # close long → re-place long at this level
                await self._place_order(level, OrderSide.BUY, is_close=False)
            else:
                # opened short → place close buy below
                await self._place_close_below(level, grid, fill_qty)

    async def _setup_grid(self) -> None:
        if not self.engine or not self._last_ticker:
            return
        grid = self._build_grid()
        if not grid:
            self.logger.error("[FuturesGrid] Invalid grid params")
            return

        self._grid_prices = grid
        self._open_orders.clear()
        self._order_map.clear()

        # Cancel existing orders
        connectors = getattr(self.engine, "connectors", None)
        if connectors is not None:
            try:
                connector = connectors.get(self._exchange())
                if connector:
                    await connector.cancel_all_orders(self._symbol())
            except Exception as e:
                self.logger.warning(f"[FuturesGrid] cancel_all failed: {e}")

        mid = self._last_ticker.mid
        mode = self.params.get("mode", "neutral")
        self.logger.info(
            f"[FuturesGrid] Setup {len(grid)-1} levels [{grid[0]}..{grid[-1]}] "
            f"mid={mid:.2f} mode={mode}"
        )

        for price in grid:
            if mode in ("neutral", "long") and price < mid:
                # Open long entry below market
                await self._place_order(price, OrderSide.BUY, is_close=False)
                await asyncio.sleep(0)
            elif mode in ("neutral", "short") and price > mid:
                # Open short entry above market
                await self._place_order(price, OrderSide.SELL, is_close=False)
                await asyncio.sleep(0)

        self.logger.info(f"[FuturesGrid] Placed {len(self._open_orders)} orders")

    async def _place_order(self, price: Decimal, side: OrderSide, is_close: bool) -> None:
        qty = self._qty(price)
        if qty <= 0:
            return
        try:
            order = await self.engine.place_order(
                exchange=self._exchange(), symbol=self._symbol(),
                side=side, order_type=OrderType.LIMIT,
                quantity=qty, price=price,
                reduce_only=is_close,
                strategy_id=self.strategy_id,
            )
            if order and order.order_id:
                self._open_orders[price] = order.order_id
                self._order_map[order.order_id] = (side, price, is_close)
        except Exception as e:
            self.logger.warning(f"[FuturesGrid] Place {side.value} @{price} failed: {e}")

    async def _place_close_above(self, filled_level: Decimal, grid: list[Decimal], qty: Decimal) -> None:
        """After a long open fills, place a close-long sell at the next level up."""
        idx = self._grid_index(filled_level, grid)
        if idx < 0 or idx + 1 >= len(grid):
            return
        next_price = grid[idx + 1]
        if next_price not in self._open_orders:
            await self._place_order(next_price, OrderSide.SELL, is_close=True)

    async def _place_close_below(self, filled_level: Decimal, grid: list[Decimal], qty: Decimal) -> None:
        """After a short open fills, place a close-short buy at the next level down."""
        idx = self._grid_index(filled_level, grid)
        if idx <= 0:
            return
        next_price = grid[idx - 1]
        if next_price not in self._open_orders:
            await self._place_order(next_price, OrderSide.BUY, is_close=True)

    @staticmethod
    def _grid_index(level: Decimal, grid: list[Decimal]) -> int:
        if level in grid:
            return grid.index(level)
        # Find closest match (float rounding tolerance)
        best, best_idx = None, -1
        for i, p in enumerate(grid):
            diff = abs(p - level)
            if best is None or diff < best:
                best, best_idx = diff, i
        return best_idx

    def enable(self) -> None:
        super().enable()
        # Always re-initialize on enable so the grid recovers cleanly when
        # orders were cancelled or positions changed while the strategy was off.
        self._initialized = False
        self._grid_prices = []
        self._open_orders.clear()
        self._order_map.clear()
        self._runaway_tripped = False

    def on_params_updated(self, changed: dict) -> None:
        if any(k in changed for k in ("grid_low", "grid_high", "grid_count", "grid_usdt", "mode")):
            self._initialized = False
            self._grid_prices = []
            self._open_orders.clear()
            self._order_map.clear()
            self._runaway_tripped = False
            self.logger.info("[FuturesGrid] Grid params changed — will reinitialize on next tick")

    def get_status(self) -> dict:
        return {
            **self._pnl_status(),
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "exchange": self.params["exchange"],
            "symbol": self.params["symbol"],
            "grid_low": self.params["grid_low"],
            "grid_high": self.params["grid_high"],
            "grid_count": self.params["grid_count"],
            "mode": self.params.get("mode", "neutral"),
            "initialized": self._initialized,
            "open_orders": len(self._open_orders),
            "total_buy_fills": self._total_buy_fills,
            "total_sell_fills": self._total_sell_fills,
            "runaway_tripped": self._runaway_tripped,
        }
