"""Market Making strategy — provides two-sided liquidity on a single exchange.

Places a limit bid and a limit ask symmetrically around an inventory-adjusted
mid price.  Spread widens automatically when volatility rises, and the mid
shifts to reduce directional inventory exposure.

Works on spot (binance_spot / okx_spot) or futures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional

from core.types import (
    Exchange, OrderSide, OrderStatus, OrderType,
    TickerEvent, OrderUpdateEvent, Ticker,
)
from strategies.base import BaseStrategy


class MarketMakerStrategy(BaseStrategy):
    """
    Params:
      exchange              str    connector, e.g. "binance_spot" (default "binance_spot")
      symbol                str    trading pair, e.g. "BTC-USDT" (default "BTC-USDT")
      spread_bps            float  base bid-ask spread in bps (default 10.0)
      order_usdt            float  notional per quote order in USDT (default 50.0)
      max_inventory_usdt    float  net inventory cap before one side is paused (default 200.0)
      inventory_skew_bps    float  max mid-shift in bps at full inventory (default 10.0)
      requote_interval_s    float  seconds between full quote refresh (default 5.0)
      vol_window            int    tick-window for volatility rolling calc (default 30)
      vol_spread_mult       float  extra spread bps per 1 bps of realised vol (default 2.0)
      min_spread_bps        float  minimum spread regardless of volatility (default 5.0)
      max_spread_bps        float  maximum spread (default 50.0)
      qty_precision         int    decimal places for order quantity (default 5)
      price_precision       int    decimal places for order price (default 2)
    """

    keeps_resting_orders = True  # quotes rest until requoted — never reap

    def __init__(self, strategy_id: str, params: dict):
        defaults: dict = {
            "exchange":           "binance_spot",
            "symbol":             "BTC-USDT",
            "spread_bps":         10.0,
            "order_usdt":         50.0,
            "max_inventory_usdt": 200.0,
            "inventory_skew_bps": 10.0,
            "requote_interval_s": 5.0,
            "vol_window":         30,
            "vol_spread_mult":    2.0,
            "min_spread_bps":     5.0,
            "max_spread_bps":     50.0,
            "qty_precision":      5,
            "price_precision":    2,
            # Adverse selection protection
            "adverse_vol_mult":   2.5,  # widen spread when vol spikes this × baseline
            "adverse_obi_thresh": 0.4,  # pause one side if |OBI| > this (strong order flow)
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        vol_window = int(defaults["vol_window"])
        self._mid_history: deque[Decimal] = deque(maxlen=vol_window)
        self._last_ticker: Optional[Ticker] = None

        # Active quote order IDs (None = not currently quoted)
        self._bid_order_id: Optional[str] = None
        self._ask_order_id: Optional[str] = None

        # Net inventory in base currency (positive = long base)
        self._net_inventory: Decimal = Decimal("0")

        # Last posted prices (for status)
        self._last_bid: Optional[Decimal] = None
        self._last_ask: Optional[Decimal] = None

        # Stats
        self._fill_count_bid: int = 0
        self._fill_count_ask: int = 0

        # Adverse selection tracking
        self._vol_baseline: Optional[float] = None   # rolling average vol
        self._adverse_paused_side: Optional[str] = None  # "bid"|"ask"|None

        self._requote_task: Optional[asyncio.Task] = None
        self._last_requote_ts: float = 0.0
        self._requoting: bool = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _round_qty(self, qty: Decimal) -> Decimal:
        prec = int(self.params["qty_precision"])
        return qty.quantize(Decimal(10) ** -prec, rounding=ROUND_DOWN)

    def _round_price(self, price: Decimal) -> Decimal:
        prec = int(self.params["price_precision"])
        return price.quantize(Decimal(10) ** -prec, rounding=ROUND_HALF_UP)

    def _volatility_bps(self) -> Decimal:
        """Rolling std of mid returns in bps."""
        if len(self._mid_history) < 4:
            return Decimal("0")
        mids = list(self._mid_history)
        returns = [(mids[i] - mids[i - 1]) / mids[i - 1] for i in range(1, len(mids))]
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / n
        if variance <= 0:
            return Decimal("0")
        return variance.sqrt() * 10000  # bps

    def _volatility_per_tick(self) -> float:
        """σ per tick (dimensionless price fraction) for A-S model."""
        if len(self._mid_history) < 4:
            return 0.001  # fallback
        mids = [float(m) for m in self._mid_history]
        returns = [(mids[i] - mids[i-1]) / mids[i-1] for i in range(1, len(mids))]
        n = len(returns)
        mean = sum(returns) / n
        var  = sum((r - mean) ** 2 for r in returns) / max(1, n - 1)
        return max(1e-6, var ** 0.5)

    def _compute_quotes(self, mid: Decimal) -> tuple[Decimal, Decimal]:
        """
        Avellaneda-Stoikov reservation price + optimal spread.

        reservation_price = mid - γ × σ² × T × q
        optimal_spread    = γ × σ² × T + (2/γ) × ln(1 + γ/κ)

        where:
          γ = risk aversion (inventory_skew_bps / 10000 as proxy)
          σ = volatility per tick
          T = remaining time in session (simplified to 1.0 constant)
          q = normalized inventory (-1 to +1)
          κ = order arrival rate proxy (1/spread_bps)
        """
        import math
        mid_f   = float(mid)
        sigma   = self._volatility_per_tick()
        gamma   = float(self.params["inventory_skew_bps"]) / 10000  # risk aversion
        T       = 1.0   # normalized session time

        max_inv = float(self.params["max_inventory_usdt"])
        inv_usdt = float(self._net_inventory) * mid_f
        q = max(-1.0, min(1.0, inv_usdt / max_inv)) if max_inv > 0 else 0.0

        # Reservation price: shift mid against inventory to reduce risk. The pure
        # A-S term (γ·σ²·T·q) is vanishingly small at crypto per-tick σ (~1e-6 →
        # σ²~1e-12), so it never skews quotes. Apply the documented skew directly:
        # at full inventory (|q|=1) the mid shifts by `inventory_skew_bps`.
        skew_frac = float(self.params["inventory_skew_bps"]) / 10000.0
        r_price = mid_f * (1.0 - skew_frac * q)

        # Optimal spread: A-S formula
        kappa = 1.0 / max(1.0, float(self.params["spread_bps"]))  # arrival rate proxy
        gamma_safe = max(1e-8, gamma)
        as_spread_bps = (
            gamma_safe * (sigma ** 2) * T * 10000
            + (2.0 / gamma_safe) * math.log(1.0 + gamma_safe / kappa) * 10000
        )

        # Clamp spread between min/max
        lo = float(self.params["min_spread_bps"])
        hi = float(self.params["max_spread_bps"])
        spread_bps = max(lo, min(hi, as_spread_bps))
        half_bps = spread_bps / 2 / 10000

        bid = self._round_price(Decimal(str(r_price * (1 - half_bps))))
        ask = self._round_price(Decimal(str(r_price * (1 + half_bps))))
        return bid, ask

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list:
        t = event.ticker
        if t.exchange != self._exchange() or t.symbol != self._symbol():
            return []
        self._last_ticker = t
        self._mid_history.append(t.mid)
        self._update_adverse_detection(t)

        now = time.time()
        if now - self._last_requote_ts >= float(self.params["requote_interval_s"]):
            if self._requote_task is None or self._requote_task.done():
                self._requote_task = asyncio.create_task(self._requote())
        return []

    def _update_adverse_detection(self, t) -> None:
        """Check for adverse selection signals: vol spike and OBI."""
        # Vol spike detection
        eff_spread = float(self._effective_spread_bps())
        adv_mult   = float(self.params.get("adverse_vol_mult", 2.5))
        if self._vol_baseline is None:
            self._vol_baseline = eff_spread
        else:
            self._vol_baseline = self._vol_baseline * 0.99 + eff_spread * 0.01

        if self._vol_baseline and eff_spread > self._vol_baseline * adv_mult:
            if not getattr(self, "_vol_warned", False):
                self.logger.warning(
                    f"[MM] Vol spike detected ({eff_spread:.1f}bps vs baseline {self._vol_baseline:.1f}bps)"
                    " — spreads widened automatically"
                )
                self._vol_warned = True
        else:
            self._vol_warned = False

        # OBI-based directional flow detection
        obi_thresh = float(self.params.get("adverse_obi_thresh", 0.4))
        if self.engine and self.engine.microstructure:
            obi = self.engine.microstructure.obi(t.exchange.value, t.symbol)
            if obi >= obi_thresh:
                self._adverse_paused_side = "ask"   # strong buy flow → pause ask (don't sell cheap)
            elif obi <= -obi_thresh:
                self._adverse_paused_side = "bid"   # strong sell flow → pause bid (don't buy expensive)
            else:
                self._adverse_paused_side = None

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        order = event.order
        if order.exchange != self._exchange():
            return
        if order.strategy_id and order.strategy_id != self.strategy_id:
            return
        if order.status != OrderStatus.FILLED:
            return

        qty = order.filled_qty
        if order.order_id == self._bid_order_id:
            self._bid_order_id = None
            self._fill_count_bid += 1
            self._net_inventory += qty
            self.logger.info(
                f"[MM] Bid filled @ {order.avg_price} qty={qty} "
                f"inventory={float(self._net_inventory):.5f}"
            )
        elif order.order_id == self._ask_order_id:
            self._ask_order_id = None
            self._fill_count_ask += 1
            self._net_inventory -= qty
            self.logger.info(
                f"[MM] Ask filled @ {order.avg_price} qty={qty} "
                f"inventory={float(self._net_inventory):.5f}"
            )
        else:
            return

        # Immediate requote after a fill — guard against dual-fill race
        if not self._requoting:
            self._requoting = True
            self._requote_task = asyncio.create_task(self._requote())

    # ── Quote lifecycle ───────────────────────────────────────────────────────

    async def _cancel_quotes(self) -> None:
        if not self.engine:
            return
        ex = self._exchange()
        sym = self._symbol()
        for oid in (self._bid_order_id, self._ask_order_id):
            if oid:
                try:
                    await self.engine.cancel_order(ex, sym, oid)
                except Exception as e:
                    self.logger.debug(f"[MM] Cancel {oid}: {e}")
        self._bid_order_id = None
        self._ask_order_id = None

    async def _requote(self) -> None:
        if not self.engine or not self._last_ticker:
            self._requoting = False
            return
        self._last_requote_ts = time.time()

        mid = self._last_ticker.mid
        bid, ask = self._compute_quotes(mid)
        max_inv = Decimal(str(self.params["max_inventory_usdt"]))
        inv_usdt = self._net_inventory * mid

        await self._cancel_quotes()

        ex = self._exchange()
        sym = self._symbol()

        # Pause buying if already long beyond limit; pause selling if short beyond limit
        can_bid = inv_usdt <= max_inv
        can_ask = (-inv_usdt) <= max_inv

        # Adverse selection: pause the side being picked off by informed order flow
        adv = self._adverse_paused_side
        if adv == "bid":
            can_bid = False
            self.logger.debug("[MM] Adverse sell flow detected — pausing bid")
        elif adv == "ask":
            can_ask = False
            self.logger.debug("[MM] Adverse buy flow detected — pausing ask")

        if can_bid:
            qty = self._round_qty(Decimal(str(self.params["order_usdt"])) / bid)
            if qty > 0:
                try:
                    order = await self.engine.place_order(
                        exchange=ex, symbol=sym,
                        side=OrderSide.BUY, order_type=OrderType.LIMIT,
                        quantity=qty, price=bid,
                        strategy_id=self.strategy_id,
                    )
                    if order and order.order_id:
                        self._bid_order_id = order.order_id
                        self._last_bid = bid
                except Exception as e:
                    self.logger.warning(f"[MM] Bid failed: {e}")

        if can_ask:
            qty = self._round_qty(Decimal(str(self.params["order_usdt"])) / ask)
            if qty > 0:
                try:
                    order = await self.engine.place_order(
                        exchange=ex, symbol=sym,
                        side=OrderSide.SELL, order_type=OrderType.LIMIT,
                        quantity=qty, price=ask,
                        strategy_id=self.strategy_id,
                    )
                    if order and order.order_id:
                        self._ask_order_id = order.order_id
                        self._last_ask = ask
                except Exception as e:
                    self.logger.warning(f"[MM] Ask failed: {e}")

        self._requoting = False

    def disable(self) -> None:
        super().disable()
        if self._requote_task and not self._requote_task.done():
            self._requote_task.cancel()
        # Best-effort cancel on disable
        if self.engine:
            asyncio.create_task(self._cancel_quotes())

    def on_params_updated(self, changed: dict) -> None:
        # Resize volatility window if changed
        if "vol_window" in changed:
            new_maxlen = int(changed["vol_window"])
            new_history: deque[Decimal] = deque(self._mid_history, maxlen=new_maxlen)
            self._mid_history = new_history

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        mid = float(self._last_ticker.mid) if self._last_ticker else None
        inv_usdt = (
            round(float(self._net_inventory * self._last_ticker.mid), 2)
            if self._last_ticker else 0.0
        )
        spread_bps_live: Optional[float] = None
        if self._last_bid and self._last_ask:
            spread_bps_live = round(
                float(
                    (self._last_ask - self._last_bid)
                    / ((self._last_ask + self._last_bid) / 2)
                    * 10000
                ),
                2,
            )
        return {
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "params": self.params,
            "current_mid": mid,
            "last_bid": float(self._last_bid) if self._last_bid else None,
            "last_ask": float(self._last_ask) if self._last_ask else None,
            "quoted_spread_bps": spread_bps_live,
            "inventory_base": float(self._net_inventory),
            "inventory_usdt": inv_usdt,
            "fill_count_bid": self._fill_count_bid,
            "fill_count_ask": self._fill_count_ask,
            **self._pnl_status(),
        }
