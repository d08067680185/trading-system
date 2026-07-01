"""Cross-exchange spread arbitrage: buy cheaper leg, sell expensive leg.

Key safety features added vs naive dual-signal approach:
  - Leg tracking: records both order IDs with timestamps
  - Mismatch detection: if leg1 fills but leg2 times out → hedge fill
  - Mismatch counter: pause symbol after N consecutive mismatches
  - Partial fill protection: only commit arb when both legs ≥ min_fill_pct
"""
from __future__ import annotations
import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Signal, TickerEvent, OrderUpdateEvent, PositionUpdateEvent,
    Ticker,
)
from strategies.base import BaseStrategy

logger = logging.getLogger("SpreadArbStrategy")


class _ArbLeg:
    """Tracks one side of an open arb."""
    __slots__ = ("order_id", "exchange", "symbol", "side", "qty", "ts", "filled", "fill_price")

    def __init__(self, order_id: str, exchange: Exchange, symbol: str,
                 side: OrderSide, qty: Decimal, ts: float):
        self.order_id   = order_id
        self.exchange   = exchange
        self.symbol     = symbol
        self.side       = side
        self.qty        = qty
        self.ts         = ts
        self.filled     = False
        self.fill_price: Optional[Decimal] = None


class _OpenArb:
    """Tracks both legs of one arb attempt."""
    __slots__ = ("legs", "ts", "symbol", "hedged", "leg_timeout_s", "trigger_id")

    def __init__(self, legs: list[_ArbLeg], symbol: str, leg_timeout_s: float = 10.0,
                 trigger_id: Optional[int] = None, ts: Optional[float] = None):
        self.legs         = legs   # [buy_leg, sell_leg]
        self.ts           = ts if ts is not None else time.time()
        self.symbol       = symbol
        self.hedged       = False
        self.leg_timeout_s = leg_timeout_s
        self.trigger_id   = trigger_id  # arb_triggers row for quality tracking

    @property
    def both_filled(self) -> bool:
        return all(l.filled for l in self.legs)

    @property
    def any_filled(self) -> bool:
        return any(l.filled for l in self.legs)

    def filled_legs(self) -> list[_ArbLeg]:
        return [l for l in self.legs if l.filled]

    def unfilled_legs(self) -> list[_ArbLeg]:
        return [l for l in self.legs if not l.filled]


class SpreadArbStrategy(BaseStrategy):
    """
    Params:
      min_profit_bps    float  minimum NET profit in bps after fees (default 5.0)
      fee_bps           float  taker fee per leg in bps (default 4.0)
      order_size_usdt   float  notional per leg in USDT (default 25.0)
      cooldown_s        float  seconds between arb attempts per symbol (default 30)
      max_position_usdt float  max total notional per symbol (default 50.0)
      leg_timeout_s     float  seconds to wait for second leg before hedge (default 8.0)
      max_mismatches    int    pause symbol after this many consecutive leg mismatches (default 3)
    """

    def __init__(self, strategy_id: str, params: dict):
        defaults = {
            "min_profit_bps":    5.0,
            "fee_bps":           4.0,
            "order_size_usdt":  25.0,
            "cooldown_s":       30.0,
            "max_position_usdt": 50.0,
            "leg_timeout_s":     8.0,
            "max_mismatches":    3,
            "use_maker_leg":     True,
            "maker_timeout_s":   3.0,
            # ── Double-maker mode ─────────────────────────────────────────────
            # When True, BOTH legs rest as post-only limits (≈0 fee, even rebate).
            # Legs are placed non-blocking and resolved via on_order_update +
            # _check_leg_timeouts: both fill → arb done; one fills → cancel the
            # resting sibling + market-hedge the filled leg; neither → cancel both.
            "maker_both_legs":   True,
            "maker_fee_bps":     0.0,     # maker fee per leg (post-only ≈ 0; set <0 for rebate)
            # ── Profitability guards ──────────────────────────────────────────
            "min_book_depth_mult": 3.0,   # require book depth ≥ order_size × this
            "spread_confirm_ticks": 2,    # spread must exceed threshold for N consecutive ticks
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)

        self._tickers:    dict[tuple[Exchange, str], Ticker] = {}
        self._last_trade: dict[str, float] = {}
        # Spread persistence counter: symbol → consecutive ticks above threshold
        self._spread_confirm: dict[str, int] = {}

        # symbol → _OpenArb (while legs are in-flight)
        self._open_arbs: dict[str, _OpenArb] = {}

        # order_id → _ArbLeg (reverse lookup for on_order_update)
        self._order_to_leg: dict[str, _ArbLeg] = {}

        # Mismatch counters per symbol
        self._mismatch_count:  dict[str, int] = {}
        self._paused_symbols:  set[str] = set()

        # Stats
        self._arb_count     = 0
        self._mismatch_total = 0
        self._last_spread_bps: dict[str, Decimal] = {}
        self._total_net_bps = Decimal("0")

        # Optional DataStorage for trigger-quality logging (injected in main.py)
        self.storage = None

    # ── Event handlers ────────────────────────────────────────────────────────

    async def on_ticker(self, event: TickerEvent) -> list[Signal]:
        t = event.ticker
        self._tickers[(t.exchange, t.symbol)] = t

        bn  = self._tickers.get((Exchange.BINANCE_SPOT, t.symbol))
        okx = self._tickers.get((Exchange.OKX_SPOT,     t.symbol))
        if bn is None or okx is None:
            return []

        await self._check_leg_timeouts(t.symbol)
        await self._evaluate_spread(t.symbol, bn, okx)
        return []  # signals emitted via direct engine.place_order() calls

    async def on_order_update(self, event: OrderUpdateEvent) -> list[Signal]:
        order = event.order
        leg = self._order_to_leg.get(order.order_id)
        if leg is None:
            return []

        if order.status == OrderStatus.FILLED:
            leg.filled     = True
            leg.fill_price = order.avg_price or order.price
            logger.info(
                f"Leg filled [{leg.exchange.value}] {leg.symbol} "
                f"{leg.side.value} {leg.qty} @ {leg.fill_price}"
            )
            arb = self._open_arbs.get(leg.symbol)
            if arb and arb.both_filled:
                self._complete_arb(arb)

        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            logger.warning(f"Leg {order.status.value}: {order.order_id}")
            await self._handle_failed_leg(leg)

        return []

    # ── Core logic ────────────────────────────────────────────────────────────

    async def _evaluate_spread(self, symbol: str, bn: Ticker, okx: Ticker) -> None:
        if symbol in self._open_arbs or symbol in self._paused_symbols:
            return
        if not self._enabled or self._halted:
            return

        now = self._now()
        if now - self._last_trade.get(symbol, 0) < self.params["cooldown_s"]:
            return

        min_profit = Decimal(str(self.params["min_profit_bps"]))
        fee_bps    = Decimal(str(self.params["fee_bps"]))
        t_mult = Decimal(str(self.regime_threshold_mult(symbol)))
        p_mult = Decimal(str(self.regime_pos_mult(symbol)))
        # Round-trip cost depends on execution mode: double-maker pays ~0 (post-only),
        # single-maker/taker pays one taker fee per leg.
        if self.params.get("maker_both_legs", True):
            cost_bps = Decimal(str(self.params.get("maker_fee_bps", 0.0))) * 2
        else:
            cost_bps = fee_bps * 2
        threshold  = (min_profit + cost_bps) * t_mult

        # Dynamic sizing: use position_sizer if available, otherwise use param
        if self.engine and self.engine.position_sizer:
            vol = self.engine.position_sizer.get_vol(symbol)
            if vol > 0:
                # Scale order size inversely with vol: high vol → smaller size
                vol_mult = min(1.0, 0.60 / vol)  # baseline 60% ann vol
                raw = float(self.params["order_size_usdt"]) * vol_mult
                size_usdt = Decimal(str(max(5.0, raw))) * p_mult
            else:
                size_usdt = Decimal(str(self.params["order_size_usdt"])) * p_mult
        else:
            size_usdt = Decimal(str(self.params["order_size_usdt"])) * p_mult

        # ── Exchange minimum-order floor ────────────────────────────────────────
        # vol_mult/p_mult can shrink size_usdt well below what Binance/OKX will
        # accept (min_notional / min_qty), causing every order to be rejected
        # locally and the symbol to auto-pause after max_mismatches. Clamp to
        # the larger exchange's minimum (+5% margin for price drift) so we either
        # trade a viable size or (if that exceeds risk limits) get rejected by
        # RiskManager — not silently fail at the connector every time.
        if self.engine:
            bn_conn  = self.engine.connectors.get(Exchange.BINANCE_SPOT)
            okx_conn = self.engine.connectors.get(Exchange.OKX_SPOT)
            floor_usdt = Decimal("0")
            if bn_conn:
                floor_usdt = max(floor_usdt, bn_conn.min_order_usdt(symbol, bn.ask))
            if okx_conn:
                floor_usdt = max(floor_usdt, okx_conn.min_order_usdt(symbol, okx.ask))
            if floor_usdt > 0:
                size_usdt = max(size_usdt, floor_usdt * Decimal("1.05"))

        # ── Book depth check ──────────────────────────────────────────────────
        depth_mult = float(self.params.get("min_book_depth_mult", 3.0))
        min_depth  = float(size_usdt) * depth_mult
        if self.engine and self.engine.microstructure:
            ms = self.engine.microstructure
            bn_bid_d  = ms.bid_depth_usdt(Exchange.BINANCE_SPOT.value, symbol)
            bn_ask_d  = ms.ask_depth_usdt(Exchange.BINANCE_SPOT.value, symbol)
            okx_bid_d = ms.bid_depth_usdt(Exchange.OKX_SPOT.value, symbol)
            okx_ask_d = ms.ask_depth_usdt(Exchange.OKX_SPOT.value, symbol)
            if min(bn_bid_d, bn_ask_d, okx_bid_d, okx_ask_d) < min_depth:
                self._spread_confirm[symbol] = 0  # reset on thin book
                return

        spread_bn_over_okx = (bn.bid - okx.ask) / okx.ask * 10000
        spread_okx_over_bn = (okx.bid - bn.ask) / bn.ask * 10000
        best_spread = max(spread_bn_over_okx, spread_okx_over_bn)
        self._last_spread_bps[symbol] = best_spread

        # ── Spread persistence: require N consecutive ticks above threshold ────
        confirm_needed = int(self.params.get("spread_confirm_ticks", 2))
        if best_spread >= threshold:
            self._spread_confirm[symbol] = self._spread_confirm.get(symbol, 0) + 1
        else:
            self._spread_confirm[symbol] = 0

        if self._spread_confirm.get(symbol, 0) < confirm_needed:
            return  # not yet confirmed

        self._spread_confirm[symbol] = 0  # reset after entry

        # Coarse pre-round only; the connector snaps to the symbol's real stepSize.
        # 0.001 here would floor a 25-USDT BTC qty (~0.0003) to zero and block all BTC arbs.
        if spread_bn_over_okx >= threshold:
            qty = (size_usdt / okx.ask).quantize(Decimal("0.000001"))
            if qty > 0:
                await self._execute_arb(
                    symbol,
                    buy_ex=Exchange.OKX_SPOT,   buy_price=okx.ask, buy_qty=qty,
                    sell_ex=Exchange.BINANCE_SPOT, sell_price=bn.bid,  sell_qty=qty,
                    spread_bps=spread_bn_over_okx,
                    threshold_bps=threshold,
                )
                return

        if spread_okx_over_bn >= threshold:
            qty = (size_usdt / bn.ask).quantize(Decimal("0.000001"))
            if qty > 0:
                await self._execute_arb(
                    symbol,
                    buy_ex=Exchange.BINANCE_SPOT, buy_price=bn.ask,  buy_qty=qty,
                    sell_ex=Exchange.OKX_SPOT,    sell_price=okx.bid, sell_qty=qty,
                    spread_bps=spread_okx_over_bn,
                    threshold_bps=threshold,
                )

    async def _execute_arb(
        self,
        symbol: str,
        buy_ex: Exchange,   buy_price: Decimal,  buy_qty: Decimal,
        sell_ex: Exchange,  sell_price: Decimal, sell_qty: Decimal,
        spread_bps: Decimal,
        threshold_bps: Decimal = Decimal("0"),
    ) -> None:
        """Place both legs directly via engine; register in leg tracker."""
        if not self.engine:
            return
        timeout = self.params["leg_timeout_s"]
        maker_both = self.params.get("maker_both_legs", True)
        if maker_both:
            cost_bps = Decimal(str(self.params.get("maker_fee_bps", 0.0))) * 2
        else:
            cost_bps = Decimal(str(self.params["fee_bps"])) * 2
        net_bps  = spread_bps - cost_bps

        logger.info(
            f"Arb [{symbol}] spread={spread_bps:.1f}bps net={net_bps:.1f}bps "
            f"mode={'maker2' if maker_both else 'taker'} "
            f"| BUY {buy_qty} @ {buy_ex.value}  SELL {sell_qty} @ {sell_ex.value}"
        )

        if maker_both:
            # ── Double-maker: both legs rest as post-only limits on the PASSIVE side
            # (buy rests on bid, sell rests on ask). They never cross the book, so the
            # exchange accepts them as maker; a fill needs a counterparty, otherwise the
            # leg times out and is cancelled (no loss). Locked spread (ask-bid) ≥ the
            # taker spread we triggered on, so the threshold stays conservative.
            buy_tk  = self._tickers.get((buy_ex,  symbol))
            sell_tk = self._tickers.get((sell_ex, symbol))
            if buy_tk is None or sell_tk is None:
                return
            buy_order = await self.engine.place_order(
                exchange=buy_ex, symbol=symbol, side=OrderSide.BUY,
                order_type=OrderType.LIMIT, quantity=buy_qty, price=buy_tk.bid,
                strategy_id=self.strategy_id, post_only=True,
            )
            sell_order = await self.engine.place_order(
                exchange=sell_ex, symbol=symbol, side=OrderSide.SELL,
                order_type=OrderType.LIMIT, quantity=sell_qty, price=sell_tk.ask,
                strategy_id=self.strategy_id, post_only=True,
            )
        else:
            # ── Single-maker (buy) + taker (sell) — legacy behaviour ──────────
            # Post-Only on the buy side saves ~6bps; sell leg Market for immediate fill.
            use_maker = self.params.get("use_maker_leg", True)
            if use_maker and buy_price is not None:
                buy_order = await self._place_maker_with_fallback(
                    exchange=buy_ex, symbol=symbol, side=OrderSide.BUY,
                    qty=buy_qty, price=buy_price,
                    timeout_s=self.params.get("maker_timeout_s", 3.0),
                )
            else:
                buy_order = await self.engine.place_order(
                    exchange=buy_ex, symbol=symbol, side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=buy_qty,
                    strategy_id=self.strategy_id,
                )
            sell_order = await self.engine.place_order(
                exchange=sell_ex, symbol=symbol, side=OrderSide.SELL,
                order_type=OrderType.MARKET, quantity=sell_qty,
                strategy_id=self.strategy_id,
            )

        # Trigger-quality log (after order placement — keeps the hot path fast)
        trigger_id: Optional[int] = None
        if self.storage:
            mode = "maker2" if maker_both else ("maker1" if self.params.get("use_maker_leg", True) else "taker")
            try:
                trigger_id = await self.storage.record_arb_trigger(
                    self.strategy_id, symbol, float(spread_bps), float(threshold_bps),
                    mode, buy_ex.value, sell_ex.value,
                )
            except Exception as e:
                logger.debug(f"Trigger log failed: {e}")

        now = self._now()
        legs: list[_ArbLeg] = []
        self._last_trade[symbol] = now
        self._arb_count += 1
        self._total_net_bps += net_bps

        for order, ex, side, qty in [
            (buy_order,  buy_ex,  OrderSide.BUY,  buy_qty),
            (sell_order, sell_ex, OrderSide.SELL, sell_qty),
        ]:
            if isinstance(order, Order) and order.order_id:
                leg = _ArbLeg(order.order_id, ex, symbol, side, qty, now)
                legs.append(leg)
                self._order_to_leg[order.order_id] = leg
                # MARKET fills are often confirmed via REST response directly
                if order.status == OrderStatus.FILLED:
                    leg.filled     = True
                    leg.fill_price = order.avg_price or order.price
            elif isinstance(order, Exception) or order is None:
                logger.error(f"Leg placement failed [{ex.value}:{side.value}]: {order}")

        if len(legs) < 2:
            # One or both legs failed to place — reverse any that did place
            for leg in legs:
                await self._hedge_single_leg(leg)
            self._record_mismatch(symbol)
            if self.storage and trigger_id is not None:
                tmp = _OpenArb(legs, symbol, trigger_id=trigger_id)
                self._finish_trigger(tmp, "place_failed")
            return

        arb = _OpenArb(legs, symbol, leg_timeout_s=timeout, trigger_id=trigger_id, ts=now)
        self._open_arbs[symbol] = arb

        if arb.both_filled:
            self._complete_arb(arb)

    async def _check_leg_timeouts(self, symbol: str) -> None:
        """Check if any open arb has an unfilled leg past its timeout."""
        arb = self._open_arbs.get(symbol)
        if arb is None or arb.hedged:
            return
        age = self._now() - arb.ts
        if age < arb.leg_timeout_s:
            return
        if arb.both_filled:
            self._complete_arb(arb)
            return

        # Past timeout with ≥1 leg unfilled. In double-maker mode those unfilled legs
        # are LIVE resting orders — cancel them first so a late fill can't become naked
        # exposure, then re-check (a cancel may have raced a fill on the exchange).
        await self._cancel_unfilled_legs(arb)
        if arb.both_filled:
            self._complete_arb(arb)
            return

        filled = arb.filled_legs()
        if not filled:
            # Neither leg filled — resting orders cancelled, nothing to unwind
            logger.warning(f"Arb timeout [{symbol}]: no legs filled after {age:.0f}s, cleaning up")
            self._finish_trigger(arb, "timeout")
            self._cleanup_arb(symbol)
            return

        # One leg filled, sibling unfilled (now cancelled) — hedge the filled leg
        logger.error(
            f"LEG MISMATCH [{symbol}]: {len(filled)} filled after {age:.0f}s — hedging"
        )
        arb.hedged = True
        for leg in filled:
            await self._hedge_single_leg(leg)
        self._record_mismatch(symbol)
        self._finish_trigger(arb, "hedged")
        self._cleanup_arb(symbol)

    async def _handle_failed_leg(self, leg: _ArbLeg) -> None:
        """Called when a leg order is rejected/cancelled."""
        arb = self._open_arbs.get(leg.symbol)
        if arb is None or arb.hedged:
            return
        arb.hedged = True
        # Double-maker: a sibling leg may still be resting — cancel it before we unwind
        # so it can't fill into naked exposure.
        await self._cancel_unfilled_legs(arb)
        filled = arb.filled_legs()
        if filled:
            logger.error(f"Leg rejected/cancelled — hedging {len(filled)} filled leg(s)")
            for fl in filled:
                await self._hedge_single_leg(fl)
            self._record_mismatch(leg.symbol)
            self._finish_trigger(arb, "hedged")
        else:
            self._finish_trigger(arb, "timeout")
        self._cleanup_arb(leg.symbol)

    async def _cancel_unfilled_legs(self, arb: _OpenArb) -> bool:
        """Cancel any still-resting (unfilled) maker legs.

        Returns True if every cancel succeeded — those legs are then guaranteed
        not to fill. A False return means a cancel was rejected, which on Binance/OKX
        usually means that leg already filled in the meantime; callers must re-check
        ``arb.both_filled`` before deciding to hedge.
        """
        all_ok = True
        for leg in arb.unfilled_legs():
            if not self.engine:
                return False
            try:
                ok = await self.engine.cancel_order(leg.exchange, leg.symbol, leg.order_id)
                if not ok:
                    all_ok = False
            except Exception as e:
                logger.warning(
                    f"Cancel resting leg failed [{leg.exchange.value}] {leg.order_id}: {e}"
                )
                all_ok = False
        return all_ok

    async def _hedge_single_leg(self, leg: _ArbLeg) -> None:
        """Reverse a filled leg to eliminate naked exposure."""
        if not self.engine or not leg.filled:
            return
        reverse_side = OrderSide.SELL if leg.side == OrderSide.BUY else OrderSide.BUY
        try:
            hedge = await self.engine.place_order(
                exchange=leg.exchange,
                symbol=leg.symbol,
                side=reverse_side,
                order_type=OrderType.MARKET,
                quantity=leg.qty,
                reduce_only=True,
                strategy_id=self.strategy_id,
            )
            logger.info(
                f"Hedge filled [{leg.exchange.value}] {leg.symbol} "
                f"{reverse_side.value} {leg.qty} — leg mismatch corrected"
            )
        except Exception as e:
            logger.error(f"HEDGE FAILED for {leg.symbol}: {e} — MANUAL INTERVENTION NEEDED")

    def _complete_arb(self, arb: _OpenArb) -> None:
        logger.info(
            f"Arb complete [{arb.symbol}]: both legs filled, "
            f"prices={[str(l.fill_price) for l in arb.legs]}"
        )
        self._mismatch_count[arb.symbol] = 0  # reset on clean completion
        self._finish_trigger(arb, "completed", realized_bps=self._realized_bps(arb))
        self._cleanup_arb(arb.symbol)

    def _realized_bps(self, arb: _OpenArb) -> Optional[float]:
        """Net spread actually captured, from both legs' fill prices."""
        buy  = next((l for l in arb.legs if l.side == OrderSide.BUY  and l.fill_price), None)
        sell = next((l for l in arb.legs if l.side == OrderSide.SELL and l.fill_price), None)
        if not buy or not sell:
            return None
        gross = (sell.fill_price - buy.fill_price) / buy.fill_price * 10000
        if self.params.get("maker_both_legs", True):
            cost = Decimal(str(self.params.get("maker_fee_bps", 0.0))) * 2
        else:
            cost = Decimal(str(self.params["fee_bps"])) * 2
        return float(gross - cost)

    def _finish_trigger(self, arb: _OpenArb, outcome: str,
                        realized_bps: Optional[float] = None) -> None:
        """Fire-and-forget outcome update for the arb_triggers row."""
        if not self.storage or arb.trigger_id is None:
            return
        legs_filled = len(arb.filled_legs())
        duration = self._now() - arb.ts

        async def _upd():
            try:
                await self.storage.update_arb_trigger(
                    arb.trigger_id, outcome, legs_filled, realized_bps, duration
                )
            except Exception as e:
                logger.debug(f"Trigger update failed: {e}")

        try:
            asyncio.get_running_loop().create_task(_upd())
        except RuntimeError:
            pass

    async def _place_maker_with_fallback(
        self,
        exchange: Exchange, symbol: str, side: OrderSide,
        qty: Decimal, price: Decimal, timeout_s: float,
    ) -> Optional[Order]:
        """Place a Post-Only limit order; fall back to Market if not filled in timeout_s."""
        order = await self.engine.place_order(
            exchange=exchange, symbol=symbol, side=side,
            order_type=OrderType.LIMIT, quantity=qty, price=price,
            strategy_id=self.strategy_id, post_only=True,
        )
        if order is None:
            return None
        if order.status == OrderStatus.FILLED:
            return order
        # Wait for fill via order-update; poll order_id
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(0.25)
            leg = self._order_to_leg.get(order.order_id)
            if leg and leg.filled:
                return order
        # Timeout: cancel maker and send market
        await self.engine.cancel_order(exchange, symbol, order.order_id)
        logger.debug(f"Maker order timeout on {symbol}, falling back to Market")
        return await self.engine.place_order(
            exchange=exchange, symbol=symbol, side=side,
            order_type=OrderType.MARKET, quantity=qty,
            strategy_id=self.strategy_id,
        )

    def _cleanup_arb(self, symbol: str) -> None:
        arb = self._open_arbs.pop(symbol, None)
        if arb:
            for leg in arb.legs:
                self._order_to_leg.pop(leg.order_id, None)

    def _record_mismatch(self, symbol: str) -> None:
        self._mismatch_total += 1
        count = self._mismatch_count.get(symbol, 0) + 1
        self._mismatch_count[symbol] = count
        max_mm = self.params["max_mismatches"]
        if count >= max_mm:
            self._paused_symbols.add(symbol)
            logger.error(
                f"PAUSING {symbol} after {count} consecutive leg mismatches "
                f"(max={max_mm}) — resume via strategy params reset"
            )

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        maker_both = self.params.get("maker_both_legs", True)
        cost_bps = (
            self.params.get("maker_fee_bps", 0.0) * 2 if maker_both
            else self.params.get("fee_bps", 4.0) * 2
        )
        avg_net = (
            float(self._total_net_bps / self._arb_count)
            if self._arb_count > 0 else 0.0
        )
        return {
            "params":               self.params,
            "arb_count":            self._arb_count,
            "avg_net_profit_bps":   avg_net,
            "maker_both_legs":      maker_both,
            "entry_threshold_bps":  self.params.get("min_profit_bps", 5.0) + cost_bps,
            "last_spreads_bps":     {sym: float(bps) for sym, bps in self._last_spread_bps.items()},
            "open_arbs":            list(self._open_arbs.keys()),
            "paused_symbols":       list(self._paused_symbols),
            "mismatch_counts":      dict(self._mismatch_count),
            "total_mismatches":     self._mismatch_total,
            "latest_rates":         {},
        }
