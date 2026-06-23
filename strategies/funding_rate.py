"""Funding rate arbitrage: exploit the periodic funding payments paid between
long and short holders on perpetual futures.

When exchange A charges a high positive funding rate, longs pay shorts on A.
The strategy opens:
  - SHORT on high-rate exchange  (collects funding)
  - LONG  on low-rate exchange   (pays less / receives if negative)

The net delta is zero; profit comes purely from the rate differential minus
fees.

Exit conditions:
  1. Rate differential collapses below exit_rate_diff threshold
  2. Accumulated profit target hit
  3. Position held longer than max_hold_hours
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

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Signal, TickerEvent, OrderUpdateEvent, PositionUpdateEvent,
    Ticker,
)
from strategies.base import BaseStrategy


import datetime as _dt

_BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BINANCE_TICKER24_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
_OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"


def _to_binance_sym(symbol: str) -> str:
    return symbol.replace("-", "")


def _to_okx_sym(symbol: str) -> str:
    base, quote = symbol.split("-")
    return f"{base}-{quote}-SWAP"


def _next_funding_time() -> float:
    """Return next 8h funding settlement UTC timestamp (0:00, 8:00, 16:00 UTC)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    for h in (8, 16, 24):
        if now.hour < h:
            target = now.replace(hour=h % 24, minute=0, second=0, microsecond=0)
            if h == 24:
                target += _dt.timedelta(days=1)
            return target.timestamp()
    return (now + _dt.timedelta(hours=8)).replace(minute=0, second=0, microsecond=0).timestamp()


class FundingRateArbStrategy(BaseStrategy):
    """
    Params:
      symbols           list[str]  symbols to monitor (default ["BTC-USDT","ETH-USDT"])
      min_rate_diff     float      min funding rate diff to enter, annualised bps (default 50)
                                   i.e. 50 bps/year ≈ 0.0014%/8h
      position_usdt     float      notional per symbol (default 500)
      check_interval_s  int        seconds between funding rate polls (default 300)
      exit_rate_diff    float      close when diff drops below this (default 10)
      max_hold_hours    float      force-close after this many hours (default 24)
      min_hold_hours    float      don't exit before collecting at least one period (default 8)
      take_profit_bps   float      exit when estimated collected funding > this annualised bps
                                   (0 = disabled, default 0)
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "symbols": ["BTC-USDT", "ETH-USDT"],
            "min_rate_diff": 50.0,
            "position_usdt": 500.0,
            "check_interval_s": 300,
            "exit_rate_diff": 10.0,
            "max_hold_hours": 72.0,   # hold longer to collect more funding periods
            "min_hold_hours": 8.0,
            "take_profit_bps": 0.0,
            # ── Profitability guards ──────────────────────────────────────────
            "taker_fee_bps":    4.0,   # taker fee per leg (bps)
            "fee_multiple":     1.5,   # require expected_revenue > fees × this
            # ── Maker execution ───────────────────────────────────────────────
            # Funding arb is not latency-sensitive (90-min entry window, hours of
            # exit slack), so legs rest as post-only limits and only fall back to
            # market on timeout. This cuts the 4-leg round trip from 16bps taker
            # to ~0 — the difference between "no symbol ever clears the gate"
            # (live scan 2026-06-11: best cross-exchange diff 12bps/8h < 16bps)
            # and a tradeable strategy.
            "maker_legs":       True,
            "maker_eff_fee_bps": 1.5,  # effective per-leg cost in maker mode (bps);
                                       # >0 to price in the market-fallback probability
            "maker_wait_s":     45.0,  # resting time before market fallback
            "cancel_grace_s":    6.0,  # wait for a racing fill after a rejected cancel
                                       # (2× the 3s REST order-poll interval)
            "quote_warmup_s":   10.0,  # max wait for first tick after subscribing a
                                       # scanned alt's feed
            # ── Entry timing ──────────────────────────────────────────────────
            "entry_window_before_funding_s": 5400,  # enter within 90 min of next settlement
            # ── Multi-symbol management ───────────────────────────────────────
            "max_simultaneous_positions": 3,  # max open arb positions
            # ── Market scan ───────────────────────────────────────────────────
            # BTC/ETH funding diffs (~0.3bps/8h) can never beat the 4-leg fee
            # gate; the real opportunities are high-funding alts. When enabled,
            # each poll ranks ALL Binance USDT perps by |funding rate| (volume-
            # filtered) and evaluates the top N alongside the static symbols.
            "scan_all": False,
            "scan_top_n": 8,
            "min_volume_24h_usdt": 50_000_000.0,
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        self._tickers: dict[tuple[Exchange, str], Ticker] = {}
        self._rates: dict[str, dict[str, float]] = {}
        self._open_arbs: dict[str, dict] = {}
        self._next_funding_ts: dict[str, float] = {}  # symbol → next 8h settlement UTC ts
        # symbol → Binance mark price; sizing fallback for scanned symbols that
        # have no live ticker subscription
        self._mark_prices: dict[str, float] = {}

        self._entry_count = 0
        self._exit_count = 0

        # Entry metadata staged by _evaluate_arb; promoted to _open_arbs only after
        # BOTH legs execute (a rejected leg must not leave a phantom open arb)
        self._pending_entries: dict[str, dict] = {}
        # order_id → "open" | "filled" | "cancelled" | "rejected" for resting maker legs
        self._maker_orders: dict[str, str] = {}

        self._poll_task: Optional[asyncio.Task] = None
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        self._predictor = None
        self._init_predictor()

    def _init_predictor(self) -> None:
        try:
            from signals.funding_predictor import FundingRatePredictor
            self._predictor = FundingRatePredictor(history_len=21 * 4, min_history=6)
        except ImportError:
            pass

    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    def set_engine(self, engine) -> None:
        super().set_engine(engine)
        # Start background polling once we have an engine / event loop
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
        self._tickers[(t.exchange, t.symbol)] = t
        return []

    async def on_order_update(self, event: OrderUpdateEvent) -> list[Signal]:
        """Track fills of our resting maker legs (fill events come from the engine's
        REST order poller since private WS is unavailable)."""
        order = event.order
        oid = order.order_id
        if oid in self._maker_orders:
            if order.status == OrderStatus.FILLED:
                self._maker_orders[oid] = "filled"
            elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                self._maker_orders[oid] = order.status.value
        return []

    async def on_position_update(self, event: PositionUpdateEvent) -> None:
        pos = event.position
        if pos.size == 0 and pos.symbol in self._open_arbs:
            arb = self._open_arbs.pop(pos.symbol)
            self._exit_count += 1
            self.logger.info(
                f"Arb closed for {pos.symbol} "
                f"(was: long={arb['long_ex']}, short={arb['short_ex']})"
            )

    # ── Background funding rate poll ──────────────────────────────────────────

    async def _poll_loop(self) -> None:
        self.logger.info("Funding rate poll loop started")
        while self._enabled:
            try:
                await self._fetch_and_evaluate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Funding rate poll error: {e}")
            await asyncio.sleep(self.params["check_interval_s"])

    async def _fetch_and_evaluate(self) -> None:
        symbols: list[str] = list(self.params["symbols"])
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            if self.params.get("scan_all", False):
                try:
                    candidates = await self._scan_binance_candidates(session)
                except Exception as e:
                    self.logger.warning(f"Funding market scan failed: {e}")
                    candidates = []
                # Always keep open-arb symbols in the list so exits are evaluated
                # even after a symbol drops out of the top-N ranking
                symbols = list(dict.fromkeys(symbols + candidates + list(self._open_arbs)))
            bn_rates, okx_rates = await asyncio.gather(
                self._fetch_binance_rates(session, symbols),
                self._fetch_okx_rates(session, symbols),
                return_exceptions=True,
            )

        if isinstance(bn_rates, Exception):
            self.logger.warning(f"Binance funding fetch failed: {bn_rates}")
            bn_rates = {}
        if isinstance(okx_rates, Exception):
            self.logger.warning(f"OKX funding fetch failed: {okx_rates}")
            okx_rates = {}

        # Store rates + compute diffs for ranking
        sym_diffs: list[tuple[str, float]] = []
        for symbol in symbols:
            bn_rate = bn_rates.get(symbol)
            okx_rate = okx_rates.get(symbol)
            if bn_rate is None or okx_rate is None:
                continue
            self._rates[symbol] = {
                Exchange.BINANCE.value: bn_rate,
                Exchange.OKX.value: okx_rate,
            }
            diff_ann_bps = abs(bn_rate - okx_rate) * 3 * 365 * 10000
            sym_diffs.append((symbol, diff_ann_bps))

        # Sort by rate differential (highest first), evaluate existing positions first
        sym_diffs.sort(key=lambda x: x[1], reverse=True)
        existing = set(self._open_arbs)
        max_pos = int(self.params["max_simultaneous_positions"])
        slots = max_pos - len(existing)
        # Always check existing for exit; only enter new if slots available
        ordered = [s for s, _ in sym_diffs if s in existing] + \
                  [s for s, _ in sym_diffs if s not in existing][:max(0, slots)]

        self._pending_entries.clear()
        for symbol in ordered:
            bn_rate = (self._rates.get(symbol) or {}).get(Exchange.BINANCE.value)
            okx_rate = (self._rates.get(symbol) or {}).get(Exchange.OKX.value)
            if bn_rate is None or okx_rate is None:
                continue
            sigs = await self._evaluate_arb(symbol, bn_rate, okx_rate)
            if not sigs or not self.engine:
                continue
            if sigs[0].reduce_only:
                await self._execute_exit_legs(symbol, sigs)
            else:
                await self._execute_entry_legs(symbol, sigs)

    async def _evaluate_arb(
        self, symbol: str, bn_rate: float, okx_rate: float
    ) -> list[Signal]:
        """Check whether to open, hold, or close an arb for this symbol."""
        # Regime-adaptive: raise threshold in HIGH/EXTREME volatility regimes
        t_mult = self.regime_threshold_mult(symbol)
        p_mult = self.regime_pos_mult(symbol)
        min_diff  = self.params["min_rate_diff"] * t_mult
        exit_diff = self.params["exit_rate_diff"]
        max_hold_h = self.params["max_hold_hours"]
        pos_usdt = Decimal(str(self.params["position_usdt"])) * Decimal(str(p_mult))

        # Convert 8h rate to annualised bps for comparison
        # rate_8h * 3 * 365 * 10000 = annualised bps
        diff_ann_bps = abs(bn_rate - okx_rate) * 3 * 365 * 10000

        # ── Close existing arb if conditions change ───────────────────────────
        if symbol in self._open_arbs:
            arb = self._open_arbs[symbol]
            age_h = (time.time() - arb["entry_ts"]) / 3600
            min_hold_h = self.params.get("min_hold_hours", 8.0)
            take_profit = self.params.get("take_profit_bps", 0.0)

            # Estimate collected funding: entry_diff_bps is annualised, so
            # collected per period = entry_diff_bps / (3 * 365) * periods_held
            # But take_profit is also in annualised bps, so compare directly.
            periods_held = age_h / 8.0
            collected_ann_bps = arb.get("entry_diff_bps", 0) * periods_held / (3 * 365)
            profit_target_hit = take_profit > 0 and collected_ann_bps >= take_profit

            should_exit = age_h >= min_hold_h and (
                diff_ann_bps < exit_diff
                or age_h >= max_hold_h
                or profit_target_hit
            )
            if should_exit:
                self.logger.info(
                    f"Closing arb {symbol}: diff={diff_ann_bps:.1f}bps age={age_h:.1f}h"
                )
                return self._close_signals(symbol, arb)
            return []

        # ── Open new arb ──────────────────────────────────────────────────────
        if diff_ann_bps < min_diff:
            return []

        # ── Fee viability gate ────────────────────────────────────────────────
        # 4 legs × effective fee = round-trip cost as fraction of position.
        # Maker mode: post-only legs pay ~0; maker_eff_fee_bps > 0 prices in the
        # probability that a leg times out and falls back to taker.
        if self.params.get("maker_legs", True):
            leg_bps = float(self.params.get("maker_eff_fee_bps", 1.5))
        else:
            leg_bps = float(self.params.get("taker_fee_bps", 4.0))
        fee_pct    = leg_bps * 4 / 10000
        min_hold_h = float(self.params.get("min_hold_hours", 8.0))
        periods    = min_hold_h / 8.0
        rate_per_period = abs(bn_rate - okx_rate)          # raw 8h rate fraction
        expected_pct    = rate_per_period * periods
        fee_mult   = float(self.params.get("fee_multiple", 1.5))
        if expected_pct < fee_pct * fee_mult:
            self.logger.debug(
                f"Fee gate [{symbol}]: expected {expected_pct*10000:.3f}bps "
                f"< fees {fee_pct*fee_mult*10000:.3f}bps — skip"
            )
            return []

        # ── Entry timing filter ───────────────────────────────────────────────
        # Prefer entering ≤ 90 min before next 8h funding settlement
        window_s = float(self.params.get("entry_window_before_funding_s", 5400))
        next_ts  = self._next_funding_ts.get(symbol, 0.0)
        if next_ts == 0.0:
            next_ts = _next_funding_time()
            self._next_funding_ts[symbol] = next_ts
        time_to_next = next_ts - time.time()
        if time_to_next > window_s:
            self.logger.debug(
                f"Timing [{symbol}]: {time_to_next/3600:.1f}h to next funding "
                f"(window {window_s/3600:.1f}h) — waiting"
            )
            return []
        if 0 < time_to_next < 300:  # < 5 min: too late, skip this cycle
            return []

        # Use predictor to filter low-confidence / late-cycle entries
        if self._predictor:
            # Record both exchange rates and get forecast for the higher-rate exchange
            higher_ex = "binance" if bn_rate > okx_rate else "okx"
            higher_rate = max(bn_rate, okx_rate)
            self._predictor.record_rate(higher_ex, symbol, higher_rate)
            # 8h rate threshold = annualised threshold / (3 * 365 * 10000)
            rate_thresh = min_diff / (3 * 365 * 10000)
            should, reason = self._predictor.should_enter(higher_ex, symbol, higher_rate, rate_thresh)
            if not should:
                self.logger.debug(f"Predictor skip [{symbol}]: {reason}")
                return []

        # Determine which exchange to long/short
        if bn_rate > okx_rate:
            # BN charges more → short BN (collect), long OKX (pay less)
            long_ex, short_ex = Exchange.OKX, Exchange.BINANCE
        else:
            long_ex, short_ex = Exchange.BINANCE, Exchange.OKX

        # Get price for sizing: live ticker if subscribed, else Binance mark
        # price captured during the funding fetch (scanned alts have no ticker feed)
        long_ticker = self._tickers.get((long_ex, symbol))
        if long_ticker is not None:
            ref_price = long_ticker.ask
        else:
            mark = self._mark_prices.get(symbol)
            if not mark:
                return []
            ref_price = Decimal(str(mark))

        # ── Exchange minimum-order floor ────────────────────────────────────────
        # position_usdt * p_mult can land below Binance/OKX min_notional / min_qty
        # for this symbol (especially scanned alts), causing "first leg failed"
        # entry-aborts every cycle. Clamp to the larger leg's minimum (+5% margin).
        if self.engine:
            floor_usdt = Decimal("0")
            for ex in (long_ex, short_ex):
                conn = self.engine.connectors.get(ex)
                if conn:
                    floor_usdt = max(floor_usdt, conn.min_order_usdt(symbol, ref_price))
            if floor_usdt > 0:
                pos_usdt = max(pos_usdt, floor_usdt * Decimal("1.05"))

        # Coarse pre-round; connector snaps to real stepSize (0.001 floors BTC qty to 0)
        qty = (pos_usdt / ref_price).quantize(Decimal("0.000001"))
        if qty <= 0:
            return []

        self.logger.info(
            f"Opening arb {symbol}: long={long_ex.value} short={short_ex.value} "
            f"diff={diff_ann_bps:.1f}bps qty={qty}"
        )

        # Staged only — promoted to _open_arbs by _execute_entry_legs once BOTH
        # legs are actually executed (a rejected/blocked leg must not leave a
        # phantom arb that the exit logic then tries to unwind).
        self._pending_entries[symbol] = {
            "long_ex": long_ex.value,
            "short_ex": short_ex.value,
            "size": float(qty),
            "entry_ts": time.time(),
            "entry_diff_bps": diff_ann_bps,
        }

        reason = f"funding_diff={diff_ann_bps:.1f}bps_ann"
        return [
            Signal(
                exchange=long_ex, symbol=symbol,
                side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=qty, strategy_id=self.strategy_id, reason=reason,
            ),
            Signal(
                exchange=short_ex, symbol=symbol,
                side=OrderSide.SELL, order_type=OrderType.MARKET,
                quantity=qty, strategy_id=self.strategy_id, reason=reason,
            ),
        ]

    def _close_signals(self, symbol: str, arb: dict) -> list[Signal]:
        """Build (but do not execute) the two reduce_only exit legs. Pure — the arb
        is popped from _open_arbs by _execute_exit_legs only after both legs place,
        so a failed exit is retried on the next poll instead of orphaning the
        exchange position."""
        long_ex = Exchange(arb["long_ex"])
        short_ex = Exchange(arb["short_ex"])
        size = Decimal(str(arb["size"]))

        reason = "funding_arb_exit"
        return [
            Signal(
                exchange=long_ex, symbol=symbol,
                side=OrderSide.SELL, order_type=OrderType.MARKET,
                quantity=size, reduce_only=True,
                strategy_id=self.strategy_id, reason=reason,
            ),
            Signal(
                exchange=short_ex, symbol=symbol,
                side=OrderSide.BUY, order_type=OrderType.MARKET,
                quantity=size, reduce_only=True,
                strategy_id=self.strategy_id, reason=reason,
            ),
        ]

    # ── Leg execution (maker with market fallback) ────────────────────────────

    async def _execute_entry_legs(self, symbol: str, sigs: list[Signal]) -> None:
        """Execute both entry legs sequentially; promote the staged arb to
        _open_arbs only if both succeed. A naked first leg is reversed at once."""
        meta = self._pending_entries.pop(symbol, None)
        if meta is None or len(sigs) != 2:
            return

        # Scanned alts have no static ticker subscription; without a live quote the
        # engine staleness guard blocks every entry. Subscribe + warm up first.
        if not await self._ensure_market_data(symbol, [s.exchange for s in sigs]):
            self.logger.info(f"Entry skipped [{symbol}]: no live quotes after warmup")
            return

        first = await self._execute_leg(sigs[0])
        if first is None:
            self.logger.warning(f"Entry aborted [{symbol}]: first leg failed")
            return

        second = await self._execute_leg(sigs[1])
        if second is None:
            # Naked single leg — reverse it immediately, do not register the arb
            self.logger.error(
                f"Entry leg mismatch [{symbol}]: second leg failed — reversing first"
            )
            reverse = (OrderSide.SELL if sigs[0].side == OrderSide.BUY
                       else OrderSide.BUY)
            try:
                await self.engine.place_order(
                    exchange=sigs[0].exchange, symbol=symbol, side=reverse,
                    order_type=OrderType.MARKET, quantity=first.quantity,
                    reduce_only=True, strategy_id=self.strategy_id,
                )
            except Exception as e:
                self.logger.error(
                    f"REVERSE FAILED [{symbol}]: {e} — naked exposure, reconciler/manual"
                )
            return

        self._open_arbs[symbol] = meta
        self._entry_count += 1

    async def _execute_exit_legs(self, symbol: str, sigs: list[Signal]) -> None:
        """Execute both reduce_only exit legs; pop the arb only if both placed so a
        failed exit is retried next poll. reduce_only makes a duplicate retry safe
        (it cannot open a reverse position)."""
        ok = True
        for sig in sigs:
            order = await self._execute_leg(sig)
            if order is None:
                ok = False
        if ok:
            self._open_arbs.pop(symbol, None)
            self._exit_count += 1
        else:
            self.logger.error(
                f"Exit incomplete [{symbol}]: arb kept open for retry next poll"
            )

    async def _execute_leg(self, sig: Signal) -> Optional[Order]:
        """Place one leg. Maker mode: rest a post-only limit at the passive price
        (BUY→bid, SELL→ask), wait for the fill event, market-fallback on timeout.
        Without a fresh local ticker the leg goes straight to market."""
        eng = self.engine
        if eng is None:
            return None

        tk = self._tickers.get((sig.exchange, sig.symbol))
        fresh = tk is not None and (time.time() - tk.timestamp) < 30.0
        if not (self.params.get("maker_legs", True) and fresh):
            return await eng.place_order(
                exchange=sig.exchange, symbol=sig.symbol, side=sig.side,
                order_type=OrderType.MARKET, quantity=sig.quantity,
                reduce_only=sig.reduce_only, strategy_id=self.strategy_id,
            )

        price = tk.bid if sig.side == OrderSide.BUY else tk.ask
        order = await eng.place_order(
            exchange=sig.exchange, symbol=sig.symbol, side=sig.side,
            order_type=OrderType.LIMIT, quantity=sig.quantity, price=price,
            reduce_only=sig.reduce_only, strategy_id=self.strategy_id,
            post_only=True,
        )
        if order is None or not order.order_id:
            return order
        if order.status == OrderStatus.FILLED:
            return order

        oid = order.order_id
        self._maker_orders[oid] = "open"
        try:
            deadline = time.time() + float(self.params.get("maker_wait_s", 45.0))
            while time.time() < deadline:
                await asyncio.sleep(0.5)
                state = self._maker_orders.get(oid)
                if state == "filled":
                    return order
                if state in ("cancelled", "rejected"):
                    # killed externally (reaper / post-only reject) → market fallback
                    break
            else:
                # Timed out while resting — cancel, minding the cancel/fill race
                cancelled = await eng.cancel_order(sig.exchange, sig.symbol, oid)
                if not cancelled:
                    # Cancel rejected → on Binance/OKX that almost always means the
                    # order just filled. Wait for the poller's fill event; if none
                    # arrives, assume filled — a wrong market fallback here would
                    # DOUBLE the position, the reconciler corrects a miss instead.
                    grace = time.time() + float(self.params.get("cancel_grace_s", 6.0))
                    while time.time() < grace:
                        await asyncio.sleep(0.5)
                        if self._maker_orders.get(oid) == "filled":
                            return order
                    self.logger.warning(
                        f"Maker leg {oid} cancel rejected, no fill event — assuming "
                        f"filled (reconciler will correct a miss)"
                    )
                    return order
        finally:
            self._maker_orders.pop(oid, None)

        self.logger.info(
            f"Maker leg fallback → market [{sig.exchange.value}:{sig.symbol} "
            f"{sig.side.value}]"
        )
        return await eng.place_order(
            exchange=sig.exchange, symbol=sig.symbol, side=sig.side,
            order_type=OrderType.MARKET, quantity=sig.quantity,
            reduce_only=sig.reduce_only, strategy_id=self.strategy_id,
        )

    async def _ensure_market_data(
        self, symbol: str, exchanges: list[Exchange]
    ) -> bool:
        """Make sure both venues have a live ticker for symbol, subscribing the
        feed at runtime for scanned alts. Returns False if quotes don't arrive
        within quote_warmup_s."""
        eng = self.engine
        if eng is None:
            return False

        def _fresh(ex: Exchange) -> bool:
            tk = self._tickers.get((ex, symbol))
            return tk is not None and (time.time() - tk.timestamp) < 30.0

        missing = [ex for ex in exchanges if not _fresh(ex)]
        if not missing:
            return True
        for ex in missing:
            if not await eng.ensure_symbol_feed(ex, symbol):
                return False
        deadline = time.time() + float(self.params.get("quote_warmup_s", 10.0))
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if all(_fresh(ex) for ex in exchanges):
                return True
        return False

    # ── REST fetchers ─────────────────────────────────────────────────────────

    async def _scan_binance_candidates(self, session: aiohttp.ClientSession) -> list[str]:
        """Rank all Binance USDT perps by |funding rate| and return the top N
        that clear the 24h volume filter (internal symbol format)."""
        async with session.get(_BINANCE_FUNDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            prem = await r.json()
        async with session.get(_BINANCE_TICKER24_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            tickers = await r.json()
        volume = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in tickers}
        min_vol = float(self.params.get("min_volume_24h_usdt", 50e6))

        ranked: list[tuple[float, str]] = []
        for item in prem:
            bsym = item.get("symbol", "")
            # USDT-margined perps only (skips USDC pairs and dated futures like BTCUSDT_260626)
            if not bsym.endswith("USDT"):
                continue
            if volume.get(bsym, 0.0) < min_vol:
                continue
            try:
                rate = float(item.get("lastFundingRate") or 0)
            except (TypeError, ValueError):
                continue
            if rate == 0.0:
                continue
            ranked.append((abs(rate), f"{bsym[:-4]}-USDT"))

        ranked.sort(reverse=True)
        top = [sym for _, sym in ranked[: int(self.params.get("scan_top_n", 8))]]
        if top:
            self.logger.debug(f"Funding scan candidates: {top}")
        return top

    async def _fetch_binance_rates(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        async with session.get(_BINANCE_FUNDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data = await r.json()
        rate_map: dict[str, dict] = {item["symbol"]: item for item in data}
        for sym in symbols:
            bn_sym = _to_binance_sym(sym)
            if bn_sym in rate_map:
                item = rate_map[bn_sym]
                result[sym] = float(item["lastFundingRate"])
                # Capture next funding time if available
                nft = item.get("nextFundingTime")
                if nft:
                    self._next_funding_ts[sym] = int(nft) / 1000  # ms → s
                # Mark price = sizing fallback for symbols without a live ticker feed
                mp = item.get("markPrice")
                if mp:
                    try:
                        self._mark_prices[sym] = float(mp)
                    except (TypeError, ValueError):
                        pass
        return result

    async def _fetch_okx_rates(
        self, session: aiohttp.ClientSession, symbols: list[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        for sym in symbols:
            okx_sym = _to_okx_sym(sym)
            params = {"instId": okx_sym}
            async with session.get(
                _OKX_FUNDING_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
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
            "execution_mode": "maker" if self.params.get("maker_legs", True) else "taker",
            "entry_count": self._entry_count,
            "exit_count": self._exit_count,
            "open_arbs": {
                sym: {
                    **arb,
                    "age_h": round((time.time() - arb["entry_ts"]) / 3600, 2),
                }
                for sym, arb in self._open_arbs.items()
            },
            "latest_rates": self._rates,
        }
