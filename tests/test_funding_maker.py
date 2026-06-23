"""FundingRateArbStrategy maker-execution tests.

Covers the maker-leg executor (passive placement, market fallback, cancel/fill
race), the mode-dependent fee gate, entry promotion/rollback, exit retry, and
runtime feed subscription for scanned alts. All offline via a fake engine.
"""
import asyncio
import time
from decimal import Decimal

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Signal, Ticker, OrderUpdateEvent,
)
from strategies.funding_rate import FundingRateArbStrategy

SYM = "AAA-USDT"


class _FundEngine:
    """Records place/cancel/subscribe calls. LIMIT orders rest (OPEN); MARKET
    orders fill immediately. Calls listed in fail_on_call return None."""

    def __init__(self, fail_on_call=None):
        self.regime_detector = None
        self.position_sizer = None
        self.placed = []
        self.cancelled = []
        self.subscribed = []
        self.fail_on_call = fail_on_call or set()
        self.cancel_result = True
        self._n = 0

    async def place_order(self, exchange, symbol, side, order_type, quantity,
                          price=None, reduce_only=False, strategy_id="", post_only=False):
        self._n += 1
        if self._n in self.fail_on_call:
            return None
        oid = f"f{self._n}"
        self.placed.append(dict(
            exchange=exchange, symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price, reduce_only=reduce_only,
            post_only=post_only, order_id=oid,
        ))
        status = OrderStatus.OPEN if order_type == OrderType.LIMIT else OrderStatus.FILLED
        return Order(exchange=exchange, symbol=symbol, side=side, order_type=order_type,
                     quantity=quantity, price=price, order_id=oid, status=status)

    async def cancel_order(self, exchange, symbol, order_id):
        self.cancelled.append(order_id)
        return self.cancel_result

    async def ensure_symbol_feed(self, exchange, symbol):
        self.subscribed.append((exchange, symbol))
        return True


def _fund(engine=None, **params):
    base = {"symbols": [SYM], "min_rate_diff": 50.0, "position_usdt": 25.0,
            "check_interval_s": 99999, "maker_legs": True, "maker_wait_s": 1.0,
            "cancel_grace_s": 1.0, "quote_warmup_s": 1.0, "min_hold_hours": 8.0}
    base.update(params)
    s = FundingRateArbStrategy("funding_test", base)
    s._predictor = None          # isolate from predictor filtering
    if engine is not None:
        s.engine = engine        # bypass set_engine — must not start the poll loop
    return s


def _tick(ex, sym=SYM, bid="100", ask="100.05"):
    return Ticker(ex, sym, Decimal(bid), Decimal(ask), Decimal(bid), Decimal("0"))


def _sig(ex, side, reduce_only=False):
    return Signal(exchange=ex, symbol=SYM, side=side,
                  order_type=OrderType.MARKET, quantity=Decimal("0.25"),
                  reduce_only=reduce_only, strategy_id="funding_test")


def _meta():
    return {"long_ex": "okx", "short_ex": "binance", "size": 0.25,
            "entry_ts": time.time(), "entry_diff_bps": 10950.0}


# ── Fee gate reflects execution mode ──────────────────────────────────────────

def _gate_signals(maker):
    async def run():
        s = _fund(maker_legs=maker)
        s._next_funding_ts[SYM] = time.time() + 1000   # inside 90-min entry window
        s._tickers[(Exchange.BINANCE, SYM)] = _tick(Exchange.BINANCE)
        s._tickers[(Exchange.OKX, SYM)] = _tick(Exchange.OKX)
        # 10bps/8h diff: > maker gate (4×1.5×1.5=9bps), < taker gate (4×4×1.5=24bps)
        return await s._evaluate_arb(SYM, 0.0010, 0.0)
    return asyncio.run(run())


def test_fee_gate_maker_passes_what_taker_rejects():
    assert len(_gate_signals(maker=True)) == 2
    assert _gate_signals(maker=False) == []


# ── Maker leg executor ────────────────────────────────────────────────────────

def test_maker_leg_rests_passive_and_returns_on_fill():
    async def run():
        eng = _FundEngine()
        s = _fund(eng)
        s._tickers[(Exchange.BINANCE, SYM)] = _tick(Exchange.BINANCE)

        async def fill_soon():
            await asyncio.sleep(0.2)
            oid = eng.placed[0]["order_id"]
            o = Order(exchange=Exchange.BINANCE, symbol=SYM, side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=Decimal("0.25"),
                      order_id=oid, status=OrderStatus.FILLED)
            await s.on_order_update(OrderUpdateEvent(order=o))

        task = asyncio.create_task(fill_soon())
        order = await s._execute_leg(_sig(Exchange.BINANCE, OrderSide.BUY))
        await task
        return eng, order

    eng, order = asyncio.run(run())
    assert order is not None
    assert len(eng.placed) == 1                       # no market fallback
    leg = eng.placed[0]
    assert leg["order_type"] == OrderType.LIMIT and leg["post_only"] is True
    assert leg["price"] == Decimal("100")             # BUY rests on the bid
    assert eng.cancelled == []


def test_maker_leg_timeout_falls_back_to_market():
    async def run():
        eng = _FundEngine()
        s = _fund(eng, maker_wait_s=0.6)
        s._tickers[(Exchange.OKX, SYM)] = _tick(Exchange.OKX)
        order = await s._execute_leg(_sig(Exchange.OKX, OrderSide.SELL))
        return eng, order

    eng, order = asyncio.run(run())
    assert len(eng.cancelled) == 1                    # resting leg cancelled first
    assert len(eng.placed) == 2
    assert eng.placed[0]["price"] == Decimal("100.05")  # SELL rests on the ask
    assert eng.placed[1]["order_type"] == OrderType.MARKET
    assert order is not None and order.order_id == eng.placed[1]["order_id"]


def test_cancel_race_assumes_filled_no_double_placement():
    """Cancel rejected + fill event arrives in the grace window → the leg is
    complete; a market fallback here would double the position."""
    box = []

    class _RaceEngine(_FundEngine):
        async def cancel_order(self, exchange, symbol, order_id):
            self.cancelled.append(order_id)
            box[0]._maker_orders[order_id] = "filled"   # fill raced the cancel
            return False

    async def run():
        eng = _RaceEngine()
        eng.cancel_result = False
        s = _fund(eng, maker_wait_s=0.6)
        box.append(s)
        s._tickers[(Exchange.BINANCE, SYM)] = _tick(Exchange.BINANCE)
        order = await s._execute_leg(_sig(Exchange.BINANCE, OrderSide.BUY))
        return eng, order

    eng, order = asyncio.run(run())
    assert order is not None
    assert len(eng.placed) == 1                       # maker leg only — no fallback


def test_no_fresh_ticker_goes_straight_to_market():
    async def run():
        eng = _FundEngine()
        s = _fund(eng)                                # no ticker cached at all
        return eng, await s._execute_leg(_sig(Exchange.BINANCE, OrderSide.BUY))

    eng, order = asyncio.run(run())
    assert order is not None
    assert eng.placed[0]["order_type"] == OrderType.MARKET


# ── Entry promotion / rollback ────────────────────────────────────────────────

def test_entry_both_legs_promote_open_arb():
    async def run():
        eng = _FundEngine()
        s = _fund(eng, maker_legs=False)
        s._tickers[(Exchange.OKX, SYM)] = _tick(Exchange.OKX)
        s._tickers[(Exchange.BINANCE, SYM)] = _tick(Exchange.BINANCE)
        s._pending_entries[SYM] = _meta()
        await s._execute_entry_legs(
            SYM, [_sig(Exchange.OKX, OrderSide.BUY), _sig(Exchange.BINANCE, OrderSide.SELL)])
        return eng, s

    eng, s = asyncio.run(run())
    assert len(eng.placed) == 2
    assert SYM in s._open_arbs and s._entry_count == 1


def test_entry_second_leg_failure_reverses_first():
    async def run():
        eng = _FundEngine(fail_on_call={2})           # second leg rejected
        s = _fund(eng, maker_legs=False)
        s._tickers[(Exchange.OKX, SYM)] = _tick(Exchange.OKX)
        s._tickers[(Exchange.BINANCE, SYM)] = _tick(Exchange.BINANCE)
        s._pending_entries[SYM] = _meta()
        await s._execute_entry_legs(
            SYM, [_sig(Exchange.OKX, OrderSide.BUY), _sig(Exchange.BINANCE, OrderSide.SELL)])
        return eng, s

    eng, s = asyncio.run(run())
    assert len(eng.placed) == 2                       # leg1 + reverse (leg2 returned None)
    rev = eng.placed[1]
    assert rev["reduce_only"] is True
    assert rev["side"] == OrderSide.SELL              # reverses the BUY first leg
    assert rev["order_type"] == OrderType.MARKET
    assert SYM not in s._open_arbs and s._entry_count == 0


# ── Exit retry semantics ──────────────────────────────────────────────────────

def test_exit_failure_keeps_arb_for_retry():
    async def run():
        eng = _FundEngine(fail_on_call={1, 2})
        s = _fund(eng, maker_legs=False)
        meta = _meta()
        s._open_arbs[SYM] = meta
        await s._execute_exit_legs(SYM, s._close_signals(SYM, meta))
        return s

    s = asyncio.run(run())
    assert SYM in s._open_arbs and s._exit_count == 0


def test_exit_success_pops_arb():
    async def run():
        eng = _FundEngine()
        s = _fund(eng, maker_legs=False)
        meta = _meta()
        s._open_arbs[SYM] = meta
        await s._execute_exit_legs(SYM, s._close_signals(SYM, meta))
        return eng, s

    eng, s = asyncio.run(run())
    assert SYM not in s._open_arbs and s._exit_count == 1
    assert all(p["reduce_only"] for p in eng.placed)


# ── Runtime feed subscription for scanned alts ────────────────────────────────

def test_ensure_market_data_subscribes_scanned_symbol():
    sym = "NEW-USDT"
    box = []

    class _SubEngine(_FundEngine):
        async def ensure_symbol_feed(self, exchange, symbol):
            self.subscribed.append((exchange, symbol))
            box[0]._tickers[(exchange, symbol)] = _tick(exchange, symbol)
            return True

    eng = _SubEngine()
    s = _fund(eng)
    box.append(s)
    ok = asyncio.run(s._ensure_market_data(sym, [Exchange.BINANCE, Exchange.OKX]))
    assert ok is True
    assert sorted(e.value for e, _ in eng.subscribed) == ["binance", "okx"]


def test_ensure_market_data_fails_without_quotes():
    class _DeafEngine(_FundEngine):
        async def ensure_symbol_feed(self, exchange, symbol):
            self.subscribed.append((exchange, symbol))
            return True                                # subscribed but no tick arrives

    eng = _DeafEngine()
    s = _fund(eng, quote_warmup_s=0.6)
    ok = asyncio.run(s._ensure_market_data("NEW-USDT", [Exchange.BINANCE, Exchange.OKX]))
    assert ok is False
