"""Cash-and-Carry (Basis) arbitrage — delta-neutral spot/futures strategy.

Buys the asset on the spot market and simultaneously shorts equal size on the
perpetual futures market.  Because funding payments on perps flow from longs to
shorts when the market is in contango (positive funding rate), the position
earns the funding rate risk-free.

Entry: funding_rate_8h > min_rate_8h (absolute, not annualised)
Exit:  funding_rate_8h < exit_rate_8h  OR  age > max_hold_hours

The strategy requires two connectors registered in the engine:
  spot_exchange    — Exchange.BINANCE_SPOT  (or OKX_SPOT)
  futures_exchange — Exchange.BINANCE       (or OKX)
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from decimal import Decimal
from typing import Optional

import aiohttp
import certifi

from core.types import Exchange, OrderSide, OrderType, Signal, TickerEvent, Ticker
from strategies.base import BaseStrategy

_BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"

def _to_binance_sym(symbol: str) -> str:
    return symbol.replace("-", "")

def _to_okx_swap_sym(symbol: str) -> str:
    base, quote = symbol.split("-")
    return f"{base}-{quote}-SWAP"


class CashCarryStrategy(BaseStrategy):
    """
    Params:
      symbols           list[str]   symbols to monitor (default ["BTC-USDT"])
      spot_exchange     str         exchange ID for spot leg (default "binance_spot")
      futures_exchange  str         exchange ID for futures leg (default "binance")
      min_rate_8h       float       min 8h funding rate to enter, e.g. 0.0003 = 0.03% (default 0.0003)
      exit_rate_8h      float       close when funding rate drops below this (default 0.0001)
      position_usdt     float       USDT notional per trade (default 300)
      check_interval_s  int         seconds between funding polls (default 300)
      max_hold_hours    float       force exit after N hours (default 72)
      min_hold_hours    float       minimum hold time before checking exit (default 8)
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "symbols": ["BTC-USDT"],
            "spot_exchange": "binance_spot",
            "futures_exchange": "binance",
            "min_rate_8h": 0.0003,
            "exit_rate_8h": 0.0001,
            "position_usdt": 300.0,
            "check_interval_s": 300,
            "max_hold_hours": 72.0,
            "min_hold_hours": 8.0,
            # ── Basis risk auto-exit ──────────────────────────────────────────
            "basis_exit_threshold_pct": 1.0,  # auto-exit if basis drifts > 1% adverse
            "taker_fee_bps": 4.0,             # for fee viability check
            "fee_multiple": 1.5,              # expected_revenue > fees × this
            # Funding is collected every 8h for the WHOLE hold, but the 4-leg
            # entry+exit fee is paid ONCE. The viability gate must amortize that
            # fixed cost over the periods we expect to collect — not over
            # min_hold (1 period), which demanded a ~262% annualized rate and
            # blocked every realistic entry. Conservative: half the max hold.
            "fee_amortize_hours": 0.0,        # 0 = auto (max_hold_hours / 2)
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        self._tickers: dict[tuple[str, str], Ticker] = {}
        # symbol → {"size", "entry_ts", "entry_rate", "spot_entry", "futures_entry"}
        self._open_positions: dict[str, dict] = {}
        self._entry_count = 0
        self._exit_count = 0
        self._last_rates: dict[str, float] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        # Basis (spot-futures spread) tracking
        # key: symbol → recent basis values (pct of spot price)
        self._basis_history: dict[str, list[float]] = {}
        self._max_basis_deviation = 0.005   # 0.5% basis widening triggers alert

    def _spot_ex(self) -> Exchange:
        return Exchange(self.params["spot_exchange"])

    def _futures_ex(self) -> Exchange:
        return Exchange(self.params["futures_exchange"])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_engine(self, engine) -> None:
        super().set_engine(engine)
        try:
            self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
        except RuntimeError:
            pass

    def enable(self) -> None:
        super().enable()
        if self._poll_task is None or self._poll_task.done():
            try:
                self._poll_task = asyncio.get_running_loop().create_task(self._poll_loop())
            except RuntimeError:
                pass

    def disable(self) -> None:
        super().disable()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        t = event.ticker
        self._tickers[(t.exchange.value, t.symbol)] = t
        self._update_basis(t.symbol)
        return []

    def _update_basis(self, symbol: str) -> None:
        """Track spot-futures basis for open carry positions."""
        spot_key    = (self._spot_ex().value, symbol)
        futures_key = (self._futures_ex().value, symbol)
        spot_t    = self._tickers.get(spot_key)
        futures_t = self._tickers.get(futures_key)
        if not spot_t or not futures_t:
            return

        spot_mid    = float(spot_t.mid)
        futures_mid = float(futures_t.mid)
        if spot_mid <= 0:
            return

        basis_pct = (futures_mid - spot_mid) / spot_mid
        if symbol not in self._basis_history:
            self._basis_history[symbol] = []
        hist = self._basis_history[symbol]
        hist.append(basis_pct)
        if len(hist) > 100:
            hist.pop(0)

        # Check basis risk for open carry positions
        pos = self._open_positions.get(symbol)
        if pos and len(hist) >= 5:
            entry_basis = pos.get("entry_basis", 0.0)
            basis_drift = abs(basis_pct - entry_basis)
            if basis_drift > self._max_basis_deviation:
                self.logger.warning(
                    f"[CashCarry] {symbol} basis risk: "
                    f"entry_basis={entry_basis:.4%} current={basis_pct:.4%} "
                    f"drift={basis_drift:.4%} > {self._max_basis_deviation:.4%}"
                )
            # Auto-exit if basis goes significantly adverse (beyond configurable threshold)
            exit_thresh = float(self.params.get("basis_exit_threshold_pct", 1.0)) / 100
            # Adverse = basis moved in wrong direction (entry assumed positive contango)
            adverse_drift = entry_basis - basis_pct  # negative means basis shrank
            if adverse_drift < -exit_thresh:
                self.logger.warning(
                    f"[CashCarry] {symbol} auto-exit: basis drifted {adverse_drift:.4%} "
                    f"(threshold -{exit_thresh:.4%}) — closing to limit loss"
                )
                asyncio.create_task(self._close_position(symbol, pos))

    # ── Background poll ───────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        self.logger.info("CashCarry poll loop started")
        while self._enabled:
            try:
                await self._fetch_and_evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"CashCarry poll error: {e}")
            await asyncio.sleep(self.params["check_interval_s"])

    async def _fetch_and_evaluate(self) -> None:
        futures_ex = self._futures_ex()
        symbols: list[str] = self.params["symbols"]

        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            if futures_ex in (Exchange.BINANCE,):
                rates = await self._fetch_binance_rates(session, symbols)
            else:
                rates = await self._fetch_okx_rates(session, symbols)

        for symbol, rate in rates.items():
            self._last_rates[symbol] = rate
            await self._evaluate(symbol, rate)

    def _fee_gate(self, rate_8h: float, min_hold_h: float,
                  max_hold_h: float) -> tuple[bool, str]:
        """Whether projected funding revenue clears the amortized fee hurdle.

        Fees are a one-time 4-leg cost (buy spot, short futures, then unwind
        both); funding accrues every 8h over the whole hold. Amortizing over
        ``fee_amortize_hours`` (default = max_hold/2, floored at min_hold) is the
        fix for the old gate, which projected over min_hold (1 period) and so
        demanded a ~262% annualized rate — blocking every realistic entry.
        """
        taker_bps  = float(self.params.get("taker_fee_bps", 4.0))
        fee_pct    = taker_bps * 4 / 10000   # 4 legs round-trip
        amortize_h = float(self.params.get("fee_amortize_hours", 0.0)) or (max_hold_h / 2.0)
        amortize_h = max(amortize_h, min_hold_h)   # never below the guaranteed hold
        periods    = amortize_h / 8.0
        expected   = rate_8h * periods
        fee_mult   = float(self.params.get("fee_multiple", 1.5))
        hurdle     = fee_pct * fee_mult
        detail = (f"rate={rate_8h*10000:.3f}bps/8h × {periods:.1f}p "
                  f"= {expected*10000:.3f}bps vs hurdle {hurdle*10000:.3f}bps")
        return expected >= hurdle, detail

    async def _evaluate(self, symbol: str, rate_8h: float) -> None:
        min_rate = self.params["min_rate_8h"]
        exit_rate = self.params["exit_rate_8h"]
        max_hold_h = self.params["max_hold_hours"]
        min_hold_h = self.params["min_hold_hours"]
        pos_usdt = Decimal(str(self.params["position_usdt"]))
        spot_ex = self._spot_ex()
        futures_ex = self._futures_ex()

        if symbol in self._open_positions:
            pos = self._open_positions[symbol]
            age_h = (time.time() - pos["entry_ts"]) / 3600
            should_exit = age_h >= min_hold_h and (rate_8h < exit_rate or age_h >= max_hold_h)
            if should_exit:
                self.logger.info(
                    f"[CashCarry] Closing {symbol}: rate={rate_8h:.4%} age={age_h:.1f}h"
                )
                await self._close_position(symbol, pos)
            return

        if rate_8h < min_rate:
            return

        # ── Fee viability gate ────────────────────────────────────────────────
        ok, detail = self._fee_gate(rate_8h, min_hold_h, max_hold_h)
        if not ok:
            self.logger.debug(f"Fee gate [{symbol}]: {detail} — skip")
            return

        # Get spot price for sizing
        spot_ticker = self._tickers.get((spot_ex.value, symbol))
        if spot_ticker is None:
            self.logger.debug(f"[CashCarry] No spot ticker for {symbol}, skipping")
            return

        qty = (pos_usdt / spot_ticker.ask).quantize(Decimal("0.0001"))
        if qty <= 0:
            return

        self.logger.info(
            f"[CashCarry] Opening {symbol}: rate={rate_8h:.4%} qty={qty} "
            f"spot={spot_ex.value} futures={futures_ex.value}"
        )

        # Capture basis at entry for basis-risk monitoring
        spot_hist    = (self._spot_ex().value, symbol)
        futures_hist = (self._futures_ex().value, symbol)
        spot_t    = self._tickers.get(spot_hist)
        futures_t = self._tickers.get(futures_hist)
        entry_basis = 0.0
        if spot_t and futures_t and float(spot_t.mid) > 0:
            entry_basis = (float(futures_t.mid) - float(spot_t.mid)) / float(spot_t.mid)

        self._open_positions[symbol] = {
            "size": float(qty),
            "entry_ts": time.time(),
            "entry_rate": rate_8h,
            "entry_basis": entry_basis,
            "legs": set(),
        }
        self._entry_count += 1

        if self.engine:
            try:
                await self.engine.place_order(
                    exchange=spot_ex,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=qty,
                    strategy_id=self.strategy_id,
                )
                self._open_positions[symbol]["legs"].add("spot")
            except Exception as e:
                self.logger.warning(f"[CashCarry] Spot leg failed for {symbol}: {e}")

            try:
                await self.engine.place_order(
                    exchange=futures_ex,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=qty,
                    strategy_id=self.strategy_id,
                )
                self._open_positions[symbol]["legs"].add("futures")
            except Exception as e:
                self.logger.warning(f"[CashCarry] Futures leg failed for {symbol}: {e}")

            if not self._open_positions[symbol]["legs"]:
                self._open_positions.pop(symbol, None)
                self._entry_count -= 1

    async def _close_position(self, symbol: str, pos: dict) -> None:
        size = Decimal(str(pos["size"]))
        legs = pos.get("legs", {"spot", "futures"})
        spot_ex = self._spot_ex()
        futures_ex = self._futures_ex()

        self._open_positions.pop(symbol, None)
        self._exit_count += 1

        if self.engine:
            if "spot" in legs:
                await self.engine.place_order(
                    exchange=spot_ex,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=size,
                    strategy_id=self.strategy_id,
                )
            else:
                self.logger.warning(f"[CashCarry] Skipping spot close for {symbol} — leg was not opened")

            if "futures" in legs:
                await self.engine.place_order(
                    exchange=futures_ex,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=size,
                    reduce_only=True,
                    strategy_id=self.strategy_id,
                )
            else:
                self.logger.warning(f"[CashCarry] Skipping futures close for {symbol} — leg was not opened")

    # ── REST fetchers ─────────────────────────────────────────────────────────

    async def _fetch_binance_rates(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        async with session.get(_BINANCE_FUNDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data = await r.json()
        rate_map = {item["symbol"]: float(item["lastFundingRate"]) for item in data}
        for sym in symbols:
            bn_sym = _to_binance_sym(sym)
            if bn_sym in rate_map:
                result[sym] = rate_map[bn_sym]
        return result

    async def _fetch_okx_rates(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        for sym in symbols:
            okx_sym = _to_okx_swap_sym(sym)
            params = {"instId": okx_sym}
            async with session.get(
                _OKX_FUNDING_URL, params=params, timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                r.raise_for_status()
                data = await r.json()
            if data.get("code") == "0" and data.get("data"):
                result[sym] = float(data["data"][0]["fundingRate"])
        return result

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "enabled": self._enabled,
            "params": self.params,
            "entry_count": self._entry_count,
            "exit_count": self._exit_count,
            "open_positions": {
                sym: {**pos, "age_h": round((time.time() - pos["entry_ts"]) / 3600, 2)}
                for sym, pos in self._open_positions.items()
            },
            "latest_rates_8h": self._last_rates,
        }
