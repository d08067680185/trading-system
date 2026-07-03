"""Signal-based futures auto-trading strategy.

Opens and closes perpetual futures positions based on one of three signal types:
  rsi      — buy when RSI < oversold threshold; sell when RSI > overbought
  breakout — buy on upward price breakout from N-period high; sell on breakdown
  ma_cross — fast/slow MA crossover (same logic as FuturesTrendStrategy but
              accessible through a single configurable signal_type param)

One position at a time (long or short).  Exits via stop-loss or take-profit
market orders.  Supports Binance USDT-M and OKX swap.
"""
from __future__ import annotations

import json
from collections import deque
from decimal import Decimal
from typing import Optional

from core.types import Exchange, OrderSide, OrderType, TickerEvent, OrderUpdateEvent
from strategies.base import BaseStrategy

# OKX public candles endpoint (no auth required)
_OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
# Binance spot/futures klines
_BINANCE_SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
_BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

# bar_interval_s → OKX bar string
_OKX_BAR = {60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m",
             3600: "1H", 14400: "4H", 86400: "1D"}
# bar_interval_s → Binance interval string
_BINANCE_BAR = {60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m",
                3600: "1h", 14400: "4h", 86400: "1d"}


class FuturesSignalStrategy(BaseStrategy):
    """
    Params:
      exchange         str    "binance" (USDT-M) or "okx" (swap)   default "binance"
      symbol           str    trading pair                           default "BTC-USDT"
      position_usdt    float  USDT per trade                        default 50
      signal_type      str    "rsi" | "breakout" | "ma_cross"      default "rsi"
      bar_interval_s   int    seconds per OHLCV bar for RSI/MA     default 900 (15m)
      rsi_period       int    RSI bars (not ticks)                  default 14
      rsi_oversold     float  RSI level that triggers a long        default 30
      rsi_overbought   float  RSI level that triggers a short       default 70
      breakout_period  int    N-bar high/low lookback window        default 20
      fast_period      int    fast MA period (ma_cross only)        default 10
      slow_period      int    slow MA period (ma_cross only)        default 30
      stop_loss_pct    float  stop-loss %                          default 2.0
      take_profit_pct  float  take-profit %                        default 6.0
      direction        str    "both" | "long_only" | "short_only"  default "both"
      cooldown_s       float  min seconds between signals           default 120
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "exchange": "binance",
            "symbol": "BTC-USDT",
            "position_usdt": 50.0,
            "signal_type": "rsi",
            "bar_interval_s": 900,
            "rsi_period": 14,
            "rsi_oversold": 30.0,
            "rsi_overbought": 70.0,
            "breakout_period": 20,
            "fast_period": 10,
            "slow_period": 30,
            "stop_loss_pct": 2.0,
            "take_profit_pct": 6.0,
            "direction": "both",
            "cooldown_s": 120.0,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        # Bar-close price history for signal computation (one entry per closed bar)
        max_period = max(
            int(self.params["rsi_period"]) + 1,
            int(self.params["breakout_period"]),
            int(self.params["slow_period"]) + 2,
        )
        self._prices: deque = deque(maxlen=max_period + 10)

        self._position_side: Optional[str] = None   # "long" | "short" | None
        self._entry_price: Optional[float] = None
        self._last_signal_t: float = 0.0
        self._last_price: float = 0.0
        self._total_trades: int = 0

        # For RSI incremental calculation
        self._avg_gain: Optional[float] = None
        self._avg_loss: Optional[float] = None
        self._last_rsi: Optional[float] = None

        # For MA cross tracking
        self._prev_fast_ma: Optional[float] = None
        self._prev_slow_ma: Optional[float] = None

        # K-line bar aggregation: track current bar boundary
        self._bar_ts: float = 0.0    # epoch-second start of current bar
        self._bar_close: float = 0.0  # last price seen in current bar

        # Position state is persisted to DB settings so it survives restarts.
        # Loaded lazily on first tick (set_engine is sync; storage is async).
        self._state_loaded: bool = False

    def _state_key(self) -> str:
        return f"futures_signal:{self.strategy_id}:position"

    async def _load_state(self) -> None:
        """Restore position from DB + pre-fill bar history from exchange REST API."""
        self._state_loaded = True
        if self._is_backtest():
            return
        storage = getattr(self.engine, "storage", None) if self.engine else None
        if storage is not None:
            try:
                raw = await storage.get_setting(self._state_key())
                if raw:
                    state = json.loads(raw)
                    self._position_side = state.get("position_side")
                    self._entry_price   = state.get("entry_price")
                    if self._position_side:
                        self.logger.info(
                            f"[FuturesSignal] Restored position: {self._position_side} "
                            f"@ {self._entry_price} from DB"
                        )
            except Exception as e:
                self.logger.warning(f"[FuturesSignal] State restore failed: {e}")

        # Pre-fill bar history so RSI is ready immediately instead of waiting ~3.75h
        await self._warmup_from_history()

    async def _warmup_from_history(self) -> None:
        """Fetch recent completed bars from exchange REST and seed _prices."""
        import asyncio
        import aiohttp
        interval = int(self.params.get("bar_interval_s", 900))
        needed = int(self.params.get("rsi_period", 14)) + 5  # a few extra for Wilder smoothing

        ex = self._exchange()
        sym = self._symbol()
        closes: list[float] = []

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                if ex in (Exchange.OKX, Exchange.OKX_SPOT):
                    bar_str = _OKX_BAR.get(interval)
                    if bar_str is None:
                        return
                    # OKX spot instId = "BTC-USDT"; swap instId = "BTC-USDT-SWAP"
                    inst_id = sym if ex == Exchange.OKX_SPOT else f"{sym}-SWAP"
                    async with session.get(
                        _OKX_CANDLES_URL,
                        params={"instId": inst_id, "bar": bar_str, "limit": str(needed)},
                    ) as resp:
                        if resp.status != 200:
                            return
                        body = await resp.json()
                        rows = body.get("data", [])
                        # OKX returns newest-first; only take confirmed bars (confirm=="1")
                        confirmed = [r for r in rows if r[8] == "1"]
                        closes = [float(r[4]) for r in reversed(confirmed)]

                elif ex == Exchange.BINANCE:
                    bar_str = _BINANCE_BAR.get(interval)
                    if bar_str is None:
                        return
                    async with session.get(
                        _BINANCE_FUTURES_KLINES_URL,
                        params={"symbol": sym.replace("-", ""), "interval": bar_str,
                                "limit": str(needed)},
                    ) as resp:
                        if resp.status != 200:
                            return
                        rows = await resp.json()
                        # Binance returns oldest-first; last row = current (incomplete) bar
                        closes = [float(r[4]) for r in rows[:-1]]
                else:
                    return
        except Exception as e:
            self.logger.warning(f"[FuturesSignal] History warmup failed: {e}")
            return

        if len(closes) < 2:
            return

        # Seed _prices with historical closes (oldest first, drop partial current bar)
        self._prices.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._last_rsi = None
        for c in closes:
            self._prices.append(c)

        # Advance bar timestamp so bar aggregation doesn't re-count current bar
        import time as _time
        self._bar_ts = int(_time.time() // interval) * interval
        self._bar_close = closes[-1] if closes else 0.0

        self.logger.info(
            f"[FuturesSignal] History warmup: {len(closes)} bars loaded "
            f"(bar={interval}s), RSI ready={len(closes) >= int(self.params.get('rsi_period',14))+1}"
        )

    async def _save_state(self) -> None:
        """Persist current position to DB."""
        storage = getattr(self.engine, "storage", None) if self.engine else None
        if storage is None:
            return
        try:
            state = {"position_side": self._position_side, "entry_price": self._entry_price}
            await storage.set_setting(self._state_key(), json.dumps(state))
        except Exception as e:
            self.logger.warning(f"[FuturesSignal] State save failed: {e}")

    def _exchange(self) -> Exchange:
        return Exchange(self.params["exchange"])

    def _symbol(self) -> str:
        return self.params["symbol"]

    def _compute_rsi(self) -> Optional[float]:
        period = int(self.params["rsi_period"])
        prices = list(self._prices)
        if len(prices) < period + 1:
            return None

        if self._avg_gain is None:
            # Seed with simple average of first N changes
            changes = [prices[i+1] - prices[i] for i in range(period)]
            gains = [c for c in changes if c > 0]
            losses = [abs(c) for c in changes if c < 0]
            self._avg_gain = sum(gains) / period
            self._avg_loss = sum(losses) / period
        else:
            # Wilder smoothing
            delta = prices[-1] - prices[-2]
            gain = delta if delta > 0 else 0.0
            loss = abs(delta) if delta < 0 else 0.0
            self._avg_gain = (self._avg_gain * (period - 1) + gain) / period
            self._avg_loss = (self._avg_loss * (period - 1) + loss) / period

        if self._avg_loss == 0:
            rsi = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = 100 - (100 / (1 + rs))
        self._last_rsi = rsi
        return rsi

    def _compute_ma(self, period: int) -> Optional[float]:
        prices = list(self._prices)
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def _check_rsi_signal(self) -> Optional[str]:
        """Returns 'long', 'short', or None."""
        rsi = self._compute_rsi()
        if rsi is None:
            return None
        oversold = float(self.params["rsi_oversold"])
        overbought = float(self.params["rsi_overbought"])
        if rsi < oversold:
            return "long"
        if rsi > overbought:
            return "short"
        return None

    def _check_breakout_signal(self) -> Optional[str]:
        period = int(self.params["breakout_period"])
        prices = list(self._prices)
        if len(prices) < period + 1:
            return None
        lookback = prices[-(period+1):-1]
        current = prices[-1]
        high = max(lookback)
        low = min(lookback)
        if current > high:
            return "long"
        if current < low:
            return "short"
        return None

    def _check_ma_cross_signal(self) -> Optional[str]:
        fast_p = int(self.params["fast_period"])
        slow_p = int(self.params["slow_period"])
        fast_ma = self._compute_ma(fast_p)
        slow_ma = self._compute_ma(slow_p)
        if fast_ma is None or slow_ma is None:
            return None
        if self._prev_fast_ma is None:
            self._prev_fast_ma = fast_ma
            self._prev_slow_ma = slow_ma
            return None
        prev_fast, prev_slow = self._prev_fast_ma, self._prev_slow_ma
        self._prev_fast_ma = fast_ma
        self._prev_slow_ma = slow_ma
        if prev_fast <= prev_slow and fast_ma > slow_ma:
            return "long"
        if prev_fast >= prev_slow and fast_ma < slow_ma:
            return "short"
        return None

    async def on_ticker(self, event: TickerEvent) -> list:
        if self._halted:
            return []
        t = event.ticker
        if t.exchange != self._exchange() or t.symbol != self._symbol():
            return []

        mid = float(t.mid)
        self._last_price = mid

        # ── Lazy state restore (first tick after startup) ─────────────────────
        if not self._state_loaded:
            await self._load_state()

        # ── Bar aggregation: accumulate ticks into bars, append close on boundary ──
        interval = int(self.params.get("bar_interval_s", 900))
        tick_ts = t.timestamp
        bar_start = int(tick_ts // interval) * interval
        new_bar = bar_start > self._bar_ts

        if new_bar and self._bar_close:
            # A new bar just opened — close the previous bar and record its close price
            self._prices.append(self._bar_close)
            self._bar_ts = bar_start
        elif self._bar_ts == 0:
            self._bar_ts = bar_start  # initialise on first tick

        self._bar_close = mid  # track running close of current bar

        # ── Real-time SL/TP check (every tick) ────────────────────────────────
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

        # ── Signal evaluation only on bar-close events ────────────────────────
        if not new_bar:
            return []

        # Cooldown check
        now = self._now()
        cooldown = float(self.params.get("cooldown_s", 120))
        if not self._is_backtest() and (now - self._last_signal_t) < cooldown:
            return []

        sig_type = self.params.get("signal_type", "rsi")
        if sig_type == "rsi":
            signal = self._check_rsi_signal()
        elif sig_type == "breakout":
            signal = self._check_breakout_signal()
        else:  # ma_cross
            signal = self._check_ma_cross_signal()

        if signal is None:
            return []

        direction = self.params.get("direction", "both")
        if signal == "long" and direction == "short_only":
            return []
        if signal == "short" and direction == "long_only":
            return []

        if signal == self._position_side:
            return []  # already in the right position

        if self._position_side and self._position_side != signal:
            await self._close_position("reverse")

        await self._open_position(signal, mid)
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
                await self._save_state()
                rsi_str = f" rsi={self._last_rsi:.1f}" if self._last_rsi else ""
                self.logger.info(
                    f"[FuturesSignal] Opened {side} @{price:.2f} qty={qty}"
                    f" signal={self.params['signal_type']}{rsi_str}"
                )
            else:
                rsi_str = f" rsi={self._last_rsi:.1f}" if self._last_rsi else ""
                self.logger.warning(
                    f"[FuturesSignal] Order blocked/rejected: {side} @{price:.2f} qty={qty}"
                    f" exchange={self._exchange().value}{rsi_str} (risk gate or min_size?)"
                )
        except Exception as e:
            self.logger.warning(f"[FuturesSignal] Open {side} failed: {e}")

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
            await self._save_state()
            self.logger.info(f"[FuturesSignal] Closed {prev_side} reason={reason}")
        except Exception as e:
            self.logger.warning(f"[FuturesSignal] Close {prev_side} failed: {e}")
            self._position_side = prev_side

    async def on_order_update(self, event: OrderUpdateEvent) -> None:
        pass

    def on_params_updated(self, changed: dict) -> None:
        # Reset indicator history if key params change
        if any(k in changed for k in (
            "rsi_period", "breakout_period", "fast_period", "slow_period",
            "signal_type", "bar_interval_s",
        )):
            max_period = max(
                int(self.params["rsi_period"]) + 1,
                int(self.params["breakout_period"]),
                int(self.params["slow_period"]) + 2,
            )
            self._prices = deque(list(self._prices), maxlen=max_period + 10)
            self._avg_gain = None
            self._avg_loss = None
            self._last_rsi = None
            self._prev_fast_ma = None
            self._prev_slow_ma = None
            self._bar_ts = 0.0
            self._bar_close = 0.0

    def get_status(self) -> dict:
        sig_type = self.params.get("signal_type", "rsi")
        if sig_type == "rsi":
            needed = int(self.params["rsi_period"]) + 1
        elif sig_type == "breakout":
            needed = int(self.params["breakout_period"]) + 1
        else:  # ma_cross
            needed = int(self.params["slow_period"]) + 2
        return {
            **self._pnl_status(),
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "exchange": self.params["exchange"],
            "symbol": self.params["symbol"],
            "signal_type": sig_type,
            "position_side": self._position_side,
            "entry_price": round(self._entry_price, 2) if self._entry_price else None,
            "last_price": round(self._last_price, 2) if self._last_price else None,
            "current_rsi": round(self._last_rsi, 1) if self._last_rsi is not None else None,
            "total_trades": self._total_trades,
            "bar_closes": len(self._prices),
            "bar_closes_needed": needed,
            "bar_interval_s": self.params.get("bar_interval_s", 900),
        }
