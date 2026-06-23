"""Unit tests for funding-rate prediction, PnL attribution and reconciliation."""
import asyncio
from decimal import Decimal

import pytest

from core.types import Exchange, OrderSide, OrderType, OrderStatus, Order, PositionSide, Position
from signals.funding_predictor import FundingRatePredictor, _sigmoid
from risk.attribution import PnLAttributor, FEE, EXECUTION, SPREAD, FUNDING
from core.reconciler import PositionReconciler


# ── FundingRatePredictor ─────────────────────────────────────────────────────

def _seed(pred, ex, sym, rates):
    for i, r in enumerate(rates):
        pred.record_rate(ex, sym, r, ts=1000.0 + i)


def test_record_rate_history_capped():
    p = FundingRatePredictor(history_len=5)
    _seed(p, "binance", "BTC-USDT", [0.0001] * 10)
    assert len(p._history["binance:BTC-USDT"]) == 5   # deque maxlen


def test_insufficient_history_enters_above_threshold():
    p = FundingRatePredictor(min_history=6)
    fc = p.forecast("binance", "BTC-USDT", current_rate=0.001, min_threshold=0.0005)
    assert fc.recommendation == "enter"
    assert "Insufficient history" in fc.reason


def test_insufficient_history_skips_below_threshold():
    p = FundingRatePredictor(min_history=6)
    fc = p.forecast("binance", "BTC-USDT", current_rate=0.0001, min_threshold=0.0005)
    assert fc.recommendation == "skip"


def test_percentile_and_mean_reversion():
    p = FundingRatePredictor(min_history=6)
    _seed(p, "binance", "BTC-USDT", [0.0001] * 6)   # flat low history
    fc = p.forecast("binance", "BTC-USDT", current_rate=0.001, min_threshold=0.0005)
    assert fc.percentile == 100.0                    # current above all history
    # predicted reverts toward the low historical mean → below current
    assert fc.predicted_rate < 0.001
    assert fc.predicted_rate > 0


def test_confidence_always_bounded():
    p = FundingRatePredictor(min_history=4)
    _seed(p, "binance", "BTC-USDT", [0.0001, 0.0002, 0.0003, 0.0004, 0.0005, 0.0006])
    for cr in (-0.01, 0.0, 0.001, 0.05):
        fc = p.forecast("binance", "BTC-USDT", current_rate=cr)
        assert 0.05 <= fc.confidence <= 0.95


def test_low_rate_recommends_exit():
    p = FundingRatePredictor(min_history=6)
    _seed(p, "binance", "BTC-USDT", [0.0001] * 6)
    # current well below half the threshold → exit
    fc = p.forecast("binance", "BTC-USDT", current_rate=0.00005, min_threshold=0.0005)
    assert fc.recommendation == "exit"


def test_should_enter_exit_wrappers():
    p = FundingRatePredictor(min_history=6)
    enter, _ = p.should_enter("binance", "BTC-USDT", 0.001, min_threshold=0.0005)
    assert enter is True
    _seed(p, "binance", "ETH-USDT", [0.0001] * 6)
    ex_flag, _ = p.should_exit("binance", "ETH-USDT", 0.00005, min_threshold=0.0005)
    assert ex_flag is True


def test_all_forecasts_one_per_symbol():
    p = FundingRatePredictor(min_history=6)
    _seed(p, "binance", "BTC-USDT", [0.0003] * 6)
    _seed(p, "okx", "ETH-USDT", [0.0004] * 6)
    out = p.all_forecasts()
    keys = {(f["exchange"], f["symbol"]) for f in out}
    assert keys == {("binance", "BTC-USDT"), ("okx", "ETH-USDT")}


def test_ann_yield_calc():
    p = FundingRatePredictor(min_history=6)
    fc = p.forecast("binance", "BTC-USDT", current_rate=0.0005)
    # 0.0005 * 3 settlements/day * 365 * 100 = 54.75%
    assert fc.ann_yield_pct == pytest.approx(54.75, abs=0.01)


def test_sigmoid_overflow_safe():
    assert _sigmoid(1000) == 1.0
    assert _sigmoid(-1000) == 0.0
    assert _sigmoid(0) == pytest.approx(0.5)


# ── PnLAttributor ────────────────────────────────────────────────────────────

class _FakeStorage:
    def __init__(self):
        self.records = []

    async def store_attribution(self, **kw):
        self.records.append(kw)


def _order(side, price, qty, fee=0.0, sid="arb1", oid="o1"):
    return Order(
        exchange=Exchange.BINANCE, symbol="BTC-USDT", side=side,
        order_type=OrderType.MARKET, quantity=Decimal(str(qty)),
        order_id=oid, status=OrderStatus.FILLED, strategy_id=sid,
        filled_qty=Decimal(str(qty)), avg_price=Decimal(str(price)),
        fee=Decimal(str(fee)),
    )


async def _record_and_flush(attr, order):
    await attr.record_fill(order)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


def test_attribution_fee_recorded_negative():
    db = _FakeStorage()
    attr = PnLAttributor(db)
    asyncio.run(_record_and_flush(attr, _order(OrderSide.BUY, 100, 1, fee=0.5)))
    fee_recs = [r for r in db.records if r["source"] == FEE]
    assert len(fee_recs) == 1
    assert fee_recs[0]["pnl_usdt"] == -0.5


def test_attribution_execution_vs_mid():
    db = _FakeStorage()
    attr = PnLAttributor(db)
    attr.update_mid("binance", "BTC-USDT", bid=100.0, ask=100.0)   # mid 100
    # buy at 99 → paid below mid by 1 * qty 2 = +2 execution edge
    asyncio.run(_record_and_flush(attr, _order(OrderSide.BUY, 99, 2)))
    exec_recs = [r for r in db.records if r["source"] == EXECUTION]
    assert len(exec_recs) == 1
    assert exec_recs[0]["pnl_usdt"] == pytest.approx(2.0)


def test_attribution_spread_on_round_trip():
    db = _FakeStorage()
    attr = PnLAttributor(db)
    # open long @100 (no mid set → no execution noise, no fee)
    asyncio.run(_record_and_flush(attr, _order(OrderSide.BUY, 100, 1, sid="arb1")))
    # close long by selling @105 → +5 spread
    asyncio.run(_record_and_flush(attr, _order(OrderSide.SELL, 105, 1, sid="arb1")))
    spread_recs = [r for r in db.records if r["source"] == SPREAD]
    assert len(spread_recs) == 1
    assert spread_recs[0]["pnl_usdt"] == pytest.approx(5.0)


def test_attribution_funding_source_for_carry_strategy():
    db = _FakeStorage()
    attr = PnLAttributor(db)
    asyncio.run(_record_and_flush(attr, _order(OrderSide.BUY, 100, 1, sid="funding_arb_btc")))
    asyncio.run(_record_and_flush(attr, _order(OrderSide.SELL, 102, 1, sid="funding_arb_btc")))
    assert any(r["source"] == FUNDING for r in db.records)
    assert not any(r["source"] == SPREAD for r in db.records)


def test_is_funding_strategy_keywords():
    assert PnLAttributor._is_funding_strategy("funding_arb") is True
    assert PnLAttributor._is_funding_strategy("cash_carry") is True
    assert PnLAttributor._is_funding_strategy("spread_arb") is False


def test_attribution_ignores_orders_without_strategy():
    db = _FakeStorage()
    attr = PnLAttributor(db)
    o = _order(OrderSide.BUY, 100, 1, fee=1.0, sid="")
    asyncio.run(_record_and_flush(attr, o))
    assert db.records == []


# ── PositionReconciler ───────────────────────────────────────────────────────

class _FakeRM:
    def __init__(self, notionals):
        from types import SimpleNamespace
        # position_notionals keyed by (exchange, symbol)
        self.state = SimpleNamespace(position_notionals=dict(notionals))
        self.corrections = []

    def record_position_notional(self, ex, sym, val):
        self.state.position_notionals[(ex, sym)] = Decimal(str(val))
        self.corrections.append((ex, sym, val))


class _FakeConnector:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


class _FakeEngine:
    def __init__(self, connector, rm, state="connected"):
        self.connectors = {Exchange.BINANCE: connector}
        self._connector_states = {"binance": state}
        self.risk_manager = rm


def _position(symbol, size, mark):
    return Position(
        exchange=Exchange.BINANCE, symbol=symbol, side=PositionSide.LONG,
        size=Decimal(str(size)), entry_price=Decimal(str(mark)),
        mark_price=Decimal(str(mark)), leverage=1,
        unrealized_pnl=Decimal("0"), margin=Decimal("0"),
    )


def test_reconcile_clean_within_tolerance():
    # actual 1000, local 1005 → diff 5 < tolerance max(1, 2% of 1000 = 20) → clean
    conn = _FakeConnector([_position("BTC-USDT", 1, 1000)])
    rm = _FakeRM({("binance", "BTC-USDT"): Decimal("1005")})
    rep = asyncio.run(PositionReconciler(_FakeEngine(conn, rm)).reconcile_now())
    assert rep["discrepancies"] == []
    assert rm.corrections == []


def test_reconcile_corrects_large_discrepancy():
    # actual 1000, local 500 → diff 500 > tolerance → corrected to actual
    conn = _FakeConnector([_position("BTC-USDT", 1, 1000)])
    rm = _FakeRM({("binance", "BTC-USDT"): Decimal("500")})
    rec = PositionReconciler(_FakeEngine(conn, rm))
    rep = asyncio.run(rec.reconcile_now())
    assert len(rep["discrepancies"]) == 1
    assert rep["corrections"][0]["corrected_to"] == 1000.0
    assert rm.state.position_notionals[("binance", "BTC-USDT")] == Decimal("1000.0")
    assert rec.status()["discrepancy_count"] == 1


def test_reconcile_detects_local_only_position():
    # exchange has no position but local thinks there's 800 → diff 800 > tol → corrected to 0
    conn = _FakeConnector([])
    rm = _FakeRM({("binance", "BTC-USDT"): Decimal("800")})
    rep = asyncio.run(PositionReconciler(_FakeEngine(conn, rm)).reconcile_now())
    assert len(rep["discrepancies"]) == 1
    assert rep["corrections"][0]["corrected_to"] == 0.0


def test_reconcile_skips_disconnected_exchange():
    conn = _FakeConnector([_position("BTC-USDT", 1, 1000)])
    rm = _FakeRM({("binance", "BTC-USDT"): Decimal("500")})
    rep = asyncio.run(PositionReconciler(_FakeEngine(conn, rm, state="disconnected")).reconcile_now())
    assert rep["exchanges_checked"] == []
    assert rm.corrections == []
