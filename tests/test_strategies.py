"""Unit tests for strategy core math (grid construction, MM Avellaneda-Stoikov quotes).

These cover the pure decision logic that actually sizes/prices orders — previously
uncovered. No network or engine needed."""
import asyncio
from decimal import Decimal

import pytest

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Ticker, TickerEvent, OrderUpdateEvent,
)
from strategies.grid import SpotGridStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.spread_arb import SpreadArbStrategy


# ── Grid construction ────────────────────────────────────────────────────────

def _grid(**params):
    base = {"grid_low": 100.0, "grid_high": 200.0, "grid_levels": 10,
            "price_precision": 2, "qty_precision": 4, "order_usdt": 100.0}
    base.update(params)
    return SpotGridStrategy("grid_test", base)


def test_grid_level_count_and_bounds():
    g = _grid(grid_low=100, grid_high=200, grid_levels=10)
    prices = g._build_grid()
    assert len(prices) == 11                     # levels+1 boundaries
    assert prices[0] == Decimal("100.00")
    assert prices[-1] == Decimal("200.00")


def test_grid_even_spacing():
    g = _grid(grid_low=100, grid_high=200, grid_levels=10)
    prices = g._build_grid()
    diffs = {prices[i + 1] - prices[i] for i in range(len(prices) - 1)}
    assert diffs == {Decimal("10.00")}           # uniform 10.0 step


def test_grid_monotonic_increasing():
    g = _grid(grid_low=100, grid_high=200, grid_levels=10)
    prices = g._build_grid()
    assert prices == sorted(prices)
    assert len(set(prices)) == len(prices)       # no duplicate levels


@pytest.mark.parametrize("low,high,levels", [
    (0, 200, 10),       # low <= 0
    (-5, 200, 10),      # negative low
    (200, 100, 10),     # high <= low
    (100, 200, 1),      # levels < 2
])
def test_grid_invalid_inputs_return_empty(low, high, levels):
    g = _grid(grid_low=low, grid_high=high, grid_levels=levels)
    assert g._build_grid() == []


def test_grid_qty_matches_order_usdt():
    g = _grid(order_usdt=100.0, qty_precision=4)
    # at price 100, qty ≈ 1.0; notional should not exceed order_usdt
    qty = g._qty(Decimal("100"))
    assert qty == Decimal("1.0000")
    assert qty * Decimal("100") <= Decimal("100")


def test_grid_qty_rounds_down():
    g = _grid(order_usdt=100.0, qty_precision=2)
    qty = g._qty(Decimal("3"))   # 33.333... → floor to 33.33
    assert qty == Decimal("33.33")
    assert qty * Decimal("3") <= Decimal("100")  # never over-spends


# ── Market maker Avellaneda-Stoikov quotes ───────────────────────────────────

def _mm(**params):
    return MarketMakerStrategy("mm_test", params)


def _feed_flat(mm, mid, n=6):
    for _ in range(n):
        mm._mid_history.append(Decimal(str(mid)))


def test_quotes_symmetric_at_zero_inventory():
    mm = _mm(max_inventory_usdt=200.0)
    _feed_flat(mm, 100)
    mm._net_inventory = Decimal("0")
    bid, ask = mm._compute_quotes(Decimal("100"))
    assert bid < Decimal("100") < ask
    # symmetric around mid (reservation price == mid when q==0)
    assert abs((Decimal("100") - bid) - (ask - Decimal("100"))) <= Decimal("0.01")


def test_long_inventory_shifts_quotes_down():
    """Holding a long → reservation price drops → both quotes move down to sell off."""
    mm = _mm(max_inventory_usdt=200.0, inventory_skew_bps=500.0)
    _feed_flat(mm, 100)
    mm._net_inventory = Decimal("0")
    bid0, ask0 = mm._compute_quotes(Decimal("100"))
    mm._net_inventory = Decimal("1.5")  # long 1.5 @ ~100 → near max inventory
    bid1, ask1 = mm._compute_quotes(Decimal("100"))
    assert bid1 < bid0 and ask1 < ask0


def test_short_inventory_shifts_quotes_up():
    mm = _mm(max_inventory_usdt=200.0, inventory_skew_bps=500.0)
    _feed_flat(mm, 100)
    mm._net_inventory = Decimal("0")
    bid0, ask0 = mm._compute_quotes(Decimal("100"))
    mm._net_inventory = Decimal("-1.5")  # short
    bid1, ask1 = mm._compute_quotes(Decimal("100"))
    assert bid1 > bid0 and ask1 > ask0


def test_spread_within_configured_bounds():
    mm = _mm(min_spread_bps=5.0, max_spread_bps=50.0, max_inventory_usdt=200.0)
    _feed_flat(mm, 100)
    mm._net_inventory = Decimal("0")
    bid, ask = mm._compute_quotes(Decimal("100"))
    spread_bps = float((ask - bid) / Decimal("100") * 10000)
    assert 5.0 - 1e-6 <= spread_bps <= 50.0 + 1e-6


def test_volatility_zero_when_flat():
    mm = _mm()
    _feed_flat(mm, 100, n=10)
    assert mm._volatility_bps() == Decimal("0")


def test_volatility_positive_when_moving():
    mm = _mm()
    for p in (100, 101, 99, 102, 98, 103):
        mm._mid_history.append(Decimal(str(p)))
    assert mm._volatility_bps() > Decimal("0")


# ── SpreadArb double-maker execution ──────────────────────────────────────────

class _FakeEngine:
    """Records place/cancel calls. Maker (resting) legs return NEW; reduce_only
    hedge orders fill immediately (they are market reversals)."""

    def __init__(self, cancel_ok=True):
        self.regime_detector = None
        self.position_sizer = None
        self.microstructure = None
        self.connectors = {}  # Exchange -> connector; empty = no min-order floor
        self.placed = []      # list[dict]
        self.cancelled = []   # list[str]
        self.cancel_ok = cancel_ok
        self._n = 0

    async def place_order(self, exchange, symbol, side, order_type, quantity,
                          price=None, reduce_only=False, strategy_id="", post_only=False):
        self._n += 1
        oid = f"o{self._n}"
        self.placed.append(dict(
            exchange=exchange, symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price, reduce_only=reduce_only,
            post_only=post_only, order_id=oid,
        ))
        status = OrderStatus.FILLED if reduce_only else OrderStatus.OPEN
        return Order(exchange=exchange, symbol=symbol, side=side, order_type=order_type,
                     quantity=quantity, price=price, order_id=oid, status=status,
                     avg_price=(price or Decimal("0")))

    async def cancel_order(self, exchange, symbol, order_id):
        self.cancelled.append(order_id)
        return self.cancel_ok


def _arb(engine, **params):
    base = {"min_profit_bps": 5.0, "fee_bps": 4.0, "order_size_usdt": 25.0,
            "cooldown_s": 0.0, "leg_timeout_s": 8.0, "maker_both_legs": True,
            "maker_fee_bps": 0.0, "spread_confirm_ticks": 1, "min_book_depth_mult": 0.0}
    base.update(params)
    s = SpreadArbStrategy("arb_test", base)
    s.set_engine(engine)
    return s


def _feed_arb_open(s):
    """Feed a spread that triggers BUY@OKX / SELL@BINANCE (bn.bid >> okx.ask)."""
    okx = Ticker(Exchange.OKX_SPOT, "BTC-USDT", Decimal("100"), Decimal("100.01"),
                 Decimal("100"), Decimal("0"))
    bn = Ticker(Exchange.BINANCE_SPOT, "BTC-USDT", Decimal("100.10"), Decimal("100.11"),
                Decimal("100.10"), Decimal("0"))

    async def run():
        await s.on_ticker(TickerEvent(ticker=okx))
        await s.on_ticker(TickerEvent(ticker=bn))
    asyncio.run(run())


def test_entry_threshold_reflects_execution_mode():
    eng = _FakeEngine()
    maker = _arb(eng, maker_both_legs=True, maker_fee_bps=0.0, min_profit_bps=5.0)
    taker = _arb(eng, maker_both_legs=False, fee_bps=4.0, min_profit_bps=5.0)
    assert maker.get_status()["entry_threshold_bps"] == 5.0    # 5 + 0*2
    assert taker.get_status()["entry_threshold_bps"] == 13.0   # 5 + 4*2


def test_double_maker_places_two_passive_postonly_limits():
    eng = _FakeEngine()
    s = _arb(eng)
    _feed_arb_open(s)
    assert len(eng.placed) == 2
    buy = next(p for p in eng.placed if p["side"] == OrderSide.BUY)
    sell = next(p for p in eng.placed if p["side"] == OrderSide.SELL)
    # Both rest as post-only limits, never market
    assert buy["order_type"] == OrderType.LIMIT and buy["post_only"] is True
    assert sell["order_type"] == OrderType.LIMIT and sell["post_only"] is True
    # Passive side: buy rests on the buy-exchange bid, sell on the sell-exchange ask
    assert buy["exchange"] == Exchange.OKX_SPOT and buy["price"] == Decimal("100")        # okx.bid
    assert sell["exchange"] == Exchange.BINANCE_SPOT and sell["price"] == Decimal("100.11")  # bn.ask


class _FloorConn:
    """Connector stub whose min_order_usdt reports a fixed notional floor."""
    def __init__(self, floor):
        self._floor = Decimal(str(floor))

    def min_order_usdt(self, symbol, ref_price):
        return self._floor


def test_min_order_floor_clamps_size_up():
    """When the configured order size is below the exchange minimum, the qty is
    clamped up to floor*1.05 so the connector won't reject every leg locally."""
    eng = _FakeEngine()
    eng.connectors = {Exchange.BINANCE_SPOT: _FloorConn(60), Exchange.OKX_SPOT: _FloorConn(60)}
    s = _arb(eng, order_size_usdt=25.0)   # below the 60-USDT floor
    _feed_arb_open(s)
    buy = next(p for p in eng.placed if p["side"] == OrderSide.BUY)
    notional = buy["quantity"] * Decimal("100.01")   # qty was sized off okx.ask
    assert notional >= Decimal("60")                  # clamped above floor, not 25


def test_no_floor_when_connectors_absent():
    """Empty connectors registry → no floor, size stays at the configured param."""
    eng = _FakeEngine()  # connectors = {}
    s = _arb(eng, order_size_usdt=25.0)
    _feed_arb_open(s)
    buy = next(p for p in eng.placed if p["side"] == OrderSide.BUY)
    notional = buy["quantity"] * Decimal("100.01")
    assert notional < Decimal("30")                   # ~25, unclamped


def _fill_leg(s, side):
    """Mark the resting leg of the given side as filled via on_order_update."""
    leg = next(l for l in s._open_arbs["BTC-USDT"].legs if l.side == side)
    o = Order(exchange=leg.exchange, symbol="BTC-USDT", side=side,
              order_type=OrderType.LIMIT, quantity=leg.qty, order_id=leg.order_id,
              status=OrderStatus.FILLED, avg_price=Decimal("100"))
    asyncio.run(s.on_order_update(OrderUpdateEvent(order=o)))


def test_one_leg_fill_cancels_resting_sibling_and_hedges():
    eng = _FakeEngine()
    s = _arb(eng, leg_timeout_s=0.0)
    _feed_arb_open(s)
    sell_oid = next(l for l in s._open_arbs["BTC-USDT"].legs
                    if l.side == OrderSide.SELL).order_id
    _fill_leg(s, OrderSide.BUY)               # only the buy leg fills
    asyncio.run(s._check_leg_timeouts("BTC-USDT"))
    assert sell_oid in eng.cancelled          # resting sell leg cancelled
    hedges = [p for p in eng.placed if p["reduce_only"]]
    assert len(hedges) == 1                   # filled buy reversed once
    assert hedges[0]["side"] == OrderSide.SELL
    assert hedges[0]["order_type"] == OrderType.MARKET
    assert "BTC-USDT" not in s._open_arbs      # cleaned up
    assert s._mismatch_total == 1


def test_no_fill_timeout_cancels_both_no_hedge():
    eng = _FakeEngine()
    s = _arb(eng, leg_timeout_s=0.0)
    _feed_arb_open(s)
    asyncio.run(s._check_leg_timeouts("BTC-USDT"))
    assert len(eng.cancelled) == 2            # both resting legs cancelled
    assert not [p for p in eng.placed if p["reduce_only"]]   # no hedge
    assert "BTC-USDT" not in s._open_arbs


def test_cancel_race_completes_instead_of_hedging():
    """If a resting leg fills exactly as we cancel it (cancel rejected), the arb is
    actually complete — we must NOT hedge."""
    box = []

    class _RaceEngine(_FakeEngine):
        async def cancel_order(self, exchange, symbol, order_id):
            self.cancelled.append(order_id)
            leg = box[0]._order_to_leg.get(order_id)
            if leg:                            # the "unfilled" leg actually filled
                leg.filled = True
                leg.fill_price = Decimal("100")
            return False                       # cancel rejected → it had filled

    eng = _RaceEngine(cancel_ok=False)
    s = _arb(eng, leg_timeout_s=0.0)
    box.append(s)
    _feed_arb_open(s)
    _fill_leg(s, OrderSide.BUY)               # buy filled; sell "fills" during cancel
    asyncio.run(s._check_leg_timeouts("BTC-USDT"))
    assert not [p for p in eng.placed if p["reduce_only"]]   # completed, no hedge
    assert "BTC-USDT" not in s._open_arbs
    assert s._mismatch_total == 0             # clean completion, not a mismatch
