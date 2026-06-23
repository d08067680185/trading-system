"""Unit tests for pure-logic analysis modules previously uncovered:
  - execution.algorithms._u_shape_weights (VWAP volume smile)
  - risk.tca slippage / execution-score math
  - backtest.metrics Sharpe / drawdown / deflated-Sharpe / data-gap checks
"""
import time
from decimal import Decimal

import pytest

from execution.algorithms import ExecutionAlgorithms
from risk.tca import TransactionCostAnalyzer
from core.types import Exchange, OrderSide, OrderType, OrderStatus, Order
import backtest.metrics as M


# ── VWAP U-shape weights ─────────────────────────────────────────────────────

def test_u_shape_weights_sum_to_one():
    for n in (1, 2, 5, 10, 23):
        w = ExecutionAlgorithms._u_shape_weights(n)
        assert len(w) == n
        assert sum(w) == pytest.approx(1.0)


def test_u_shape_is_actually_u_shaped():
    # ends must be the heaviest, the middle the lightest — the whole point of VWAP.
    w = ExecutionAlgorithms._u_shape_weights(9)
    mid = len(w) // 2
    assert w[0] == pytest.approx(w[-1])      # symmetric
    assert w[0] > w[mid]                      # heavier at the open than the middle
    assert w[-1] > w[mid]                     # heavier at the close than the middle
    assert w[mid] == min(w)                   # trough in the middle


def test_u_shape_single_slice():
    assert ExecutionAlgorithms._u_shape_weights(1) == [1.0]


# ── TCA slippage / scoring ───────────────────────────────────────────────────

def _filled_order(side, fill_price, mid, fee=0.0, qty=1.0, sid="s1", oid="o1"):
    o = Order(
        exchange=Exchange.BINANCE, symbol="BTC-USDT", side=side,
        order_type=OrderType.MARKET, quantity=Decimal(str(qty)),
        order_id=oid, status=OrderStatus.FILLED, strategy_id=sid,
        filled_qty=Decimal(str(qty)), avg_price=Decimal(str(fill_price)),
        fee=Decimal(str(fee)),
    )
    return o, mid


def _tca_with(order_mid_pairs, budget=5.0):
    tca = TransactionCostAnalyzer(slippage_budget_bps=budget)
    for o, mid in order_mid_pairs:
        tca.update_mid("binance", "BTC-USDT", mid - 0.0, mid + 0.0)  # mid exact
        tca.record_fill(o)
    return tca


def test_tca_buy_above_mid_is_negative_slippage():
    o, mid = _filled_order(OrderSide.BUY, fill_price=100.5, mid=100.0)
    tca = _tca_with([(o, mid)])
    rec = tca.get_recent_fills("s1")[0]
    # paid 0.5 above mid on a 100 mid → -50 bps
    assert rec["slippage_bps"] == pytest.approx(-50.0, abs=0.1)
    assert rec["over_budget"] is True


def test_tca_sell_above_mid_is_positive_slippage_and_maker():
    o, mid = _filled_order(OrderSide.SELL, fill_price=100.5, mid=100.0)
    tca = _tca_with([(o, mid)])
    rec = tca.get_recent_fills("s1")[0]
    assert rec["slippage_bps"] == pytest.approx(50.0, abs=0.1)  # sold above mid = good
    assert rec["is_maker"] is True
    assert rec["over_budget"] is False                          # maker never over budget


def test_tca_stats_score_perfect_for_clean_maker_fills():
    pairs = [_filled_order(OrderSide.SELL, 100.2, 100.0, oid=f"o{i}") for i in range(5)]
    tca = _tca_with(pairs)
    stats = tca.get_stats("s1")["s1"]
    assert stats.n_fills == 5
    assert stats.maker_rate_pct == 100.0
    assert stats.execution_score >= 99.0


def test_tca_worst_fill_tracked():
    pairs = [
        _filled_order(OrderSide.BUY, 100.1, 100.0, oid="ok"),
        _filled_order(OrderSide.BUY, 105.0, 100.0, oid="bad"),  # -500 bps
    ]
    tca = _tca_with(pairs)
    stats = tca.get_stats("s1")["s1"]
    assert stats.worst_fill.order_id == "bad"
    assert stats.worst_fill.slippage_bps == pytest.approx(-500.0, abs=1.0)


def test_tca_skips_fill_without_mid():
    o, _ = _filled_order(OrderSide.BUY, 100.0, 0.0)
    tca = TransactionCostAnalyzer()
    tca.record_fill(o)                       # no mid recorded → ignored
    assert tca.get_stats("s1") == {}


# ── Backtest metrics ─────────────────────────────────────────────────────────

def test_metrics_empty_curve_returns_zeros():
    m = M.calculate([], [], initial_capital=1000)
    assert m.sharpe_ratio == 0 and m.total_trades == 0


def test_metrics_total_return_and_drawdown():
    # equity: 1000 → 1100 → 990 → 1200
    curve = [(0, 1000.0), (3600, 1100.0), (7200, 990.0), (10800, 1200.0)]
    m = M.calculate(curve, [], initial_capital=1000)
    assert m.total_return_pct == pytest.approx(20.0)
    # max drawdown: peak 1100 → trough 990 = 10%
    assert m.max_drawdown_pct == pytest.approx(10.0, abs=0.01)


def test_metrics_sharpe_positive_for_uptrend():
    curve = [(i * 3600, 1000.0 * (1.001 ** i)) for i in range(50)]
    m = M.calculate(curve, [], initial_capital=1000)
    assert m.sharpe_ratio > 0


def test_metrics_trade_stats():
    trades = [
        M.BacktestTrade("BTC-USDT", "buy", 0, 3600, 100, 110, 1, pnl=10, pnl_pct=10),
        M.BacktestTrade("BTC-USDT", "buy", 0, 3600, 100, 95, 1, pnl=-5, pnl_pct=-5),
    ]
    curve = [(0, 1000.0), (3600, 1005.0)]
    m = M.calculate(curve, trades, initial_capital=1000)
    assert m.total_trades == 2
    assert m.winning_trades == 1 and m.losing_trades == 1
    assert m.win_rate == pytest.approx(0.5)
    assert m.profit_factor == pytest.approx(2.0)   # 10 win / 5 loss


def test_deflated_sharpe_bounds():
    # non-positive sharpe or tiny sample → 0
    assert M.deflated_sharpe_ratio(-1.0, 100) == 0.0
    assert M.deflated_sharpe_ratio(2.0, 3) == 0.0
    # positive sharpe with decent sample → between 0 and observed
    dsr = M.deflated_sharpe_ratio(2.0, 500, n_trials=1)
    assert 0.0 < dsr <= 2.0


def test_deflated_sharpe_penalizes_many_trials():
    single = M.deflated_sharpe_ratio(2.0, 500, n_trials=1)
    many = M.deflated_sharpe_ratio(2.0, 500, n_trials=100)
    assert many < single   # more trials → more deflation


class _Row:
    def __init__(self, ts):
        self.ts = ts


def test_data_gaps_clean_series_ok():
    rows = [_Row(i * 3600) for i in range(100)]
    res = M.check_data_gaps(rows, "1h")
    assert res["ok"] is True
    assert res["gap_pct"] == 0.0


def test_data_gaps_detects_missing_bars():
    # 100 hours span but only 60 bars present → 40% gap
    rows = [_Row(i * 3600) for i in range(60)] + [_Row(100 * 3600)]
    res = M.check_data_gaps(rows, "1h")
    assert res["ok"] is False
    assert res["gap_pct"] > 5.0
    assert res["warning"] is not None
