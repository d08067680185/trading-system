"""Unit tests for margin monitoring and stress testing logic."""
import asyncio
from decimal import Decimal

import pytest

from core.types import Exchange, PositionSide, Position
from risk.margin_monitor import MarginMonitor, MarginStatus
from risk.stress_test import StressTester, SCENARIOS
from risk.factor_exposure import FactorExposureMonitor


# ── MarginMonitor ────────────────────────────────────────────────────────────

class _StubEngine:
    def __init__(self, positions):
        self._positions = positions
        self._notifier = None
        self.reduced = []

    async def get_positions(self):
        return self._positions

    async def place_order(self, **kw):
        self.reduced.append(kw)
        from types import SimpleNamespace
        return SimpleNamespace(order_id="r1")


def _status(safety):
    return MarginStatus(exchange="binance", symbol="BTC-USDT", side="long",
                        size=1.0, entry_price=100, mark_price=100, liq_price=90,
                        unrealized_pnl=0, leverage=10, safety_pct=safety, margin_ratio=0)


def test_alert_level_thresholds():
    mm = MarginMonitor(_StubEngine([]), warn_safety_pct=15, critical_safety_pct=8)
    assert mm._alert_level(_status(20)) == "ok"
    assert mm._alert_level(_status(12)) == "warning"
    assert mm._alert_level(_status(5)) == "critical"


def test_should_alert_throttles():
    mm = MarginMonitor(_StubEngine([]))
    assert mm._should_alert("k", 1000.0) is True
    assert mm._should_alert("k", 1100.0) is False        # within 300s cooldown
    assert mm._should_alert("k", 1000.0 + 301) is True   # cooldown elapsed


def test_check_computes_safety_pct_and_autoreduces():
    # mark 100, liq 95 → 5% safety < 8% critical → auto-reduce
    pos = Position(
        exchange=Exchange.BINANCE, symbol="BTC-USDT", side=PositionSide.LONG,
        size=Decimal("2"), entry_price=Decimal("110"), mark_price=Decimal("100"),
        leverage=10, unrealized_pnl=Decimal("-20"), margin=Decimal("20"),
        liquidation_price=Decimal("95"),
    )
    eng = _StubEngine([pos])
    mm = MarginMonitor(eng, warn_safety_pct=15, critical_safety_pct=8)
    asyncio.run(mm._check())

    statuses = mm.get_statuses()
    assert len(statuses) == 1
    assert statuses[0]["safety_pct"] == pytest.approx(5.0, abs=0.01)
    assert statuses[0]["alert_level"] == "critical"
    # auto-reduce placed a reduce_only SELL for 50% of size
    assert len(eng.reduced) == 1
    assert eng.reduced[0]["reduce_only"] is True
    assert eng.reduced[0]["quantity"] == Decimal("1.0")


def test_check_skips_positions_without_liq_price():
    pos = Position(
        exchange=Exchange.BINANCE, symbol="BTC-USDT", side=PositionSide.LONG,
        size=Decimal("1"), entry_price=Decimal("100"), mark_price=Decimal("100"),
        leverage=10, unrealized_pnl=Decimal("0"), margin=Decimal("10"),
        liquidation_price=Decimal("0"),    # unknown → skipped
    )
    eng = _StubEngine([pos])
    mm = MarginMonitor(eng)
    asyncio.run(mm._check())
    assert mm.get_statuses() == []


# ── StressTester scenarios ───────────────────────────────────────────────────

def _positions():
    return [
        {"exchange": "binance", "symbol": "BTC-USDT", "side": "long",
         "notional": 1000.0, "mark_price": 50000.0},
        {"exchange": "binance", "symbol": "ETH-USDT", "side": "short",
         "notional": 500.0, "mark_price": 3000.0},
    ]


def test_scenario_long_loses_on_crash():
    st = StressTester(portfolio_risk=None)
    results = st.run_scenarios(_positions(), equity_usdt=10000)
    crash = next(r for r in results if r["scenario"] == "btc_crash_20pct")
    # BTC long 1000 * -0.20 = -200 ; ETH short 500 * -0.15 * -1 = +75 → -125
    assert crash["total_pnl_usdt"] == pytest.approx(-125.0, abs=0.01)
    assert crash["pct_of_equity"] == pytest.approx(-1.25, abs=0.01)


def test_scenarios_sorted_worst_first():
    st = StressTester(portfolio_risk=None)
    results = st.run_scenarios(_positions(), equity_usdt=10000)
    pnls = [r["total_pnl_usdt"] for r in results]
    assert pnls == sorted(pnls)               # ascending = worst first
    assert results[0]["scenario"] == "black_swan_50pct"


def test_scenario_skips_zero_notional():
    st = StressTester(portfolio_risk=None)
    results = st.run_scenarios(
        [{"symbol": "BTC-USDT", "side": "long", "notional": 0, "mark_price": 0}],
        equity_usdt=1000)
    crash = next(r for r in results if r["scenario"] == "btc_crash_10pct")
    assert crash["position_impacts"] == []
    assert crash["total_pnl_usdt"] == 0.0


def test_scenario_rally_helps_long():
    st = StressTester(portfolio_risk=None)
    results = st.run_scenarios(_positions(), equity_usdt=10000)
    rally = next(r for r in results if r["scenario"] == "market_rally_15pct")
    # BTC long 1000*0.15=+150 ; ETH short 500*0.12*-1=-60 → +90
    assert rally["total_pnl_usdt"] == pytest.approx(90.0, abs=0.01)


# ── Monte Carlo VaR ──────────────────────────────────────────────────────────

class _StubPR:
    def __init__(self, daily):
        self._daily = daily

    def _combined_daily_pnl(self):
        return self._daily


def test_monte_carlo_insufficient_data():
    st = StressTester(_StubPR([1.0, 2.0]))   # < 5 points
    mc = st.monte_carlo_var()
    assert mc.n_simulations == 0
    assert mc.var_usdt == 0.0


def test_monte_carlo_var_deterministic_with_seed():
    daily = [-50.0, -20.0, 10.0, 30.0, 5.0, -10.0, 25.0, -35.0, 15.0, -5.0]
    st = StressTester(_StubPR(daily))
    a = st.monte_carlo_var(n_simulations=2000, confidence=0.95, seed=42)
    b = st.monte_carlo_var(n_simulations=2000, confidence=0.95, seed=42)
    assert a.var_usdt == b.var_usdt          # reproducible
    assert a.cvar_usdt >= a.var_usdt         # CVaR (tail mean loss) >= VaR
    assert a.min_pnl <= a.mean_pnl <= a.max_pnl


# ── FactorExposureMonitor ────────────────────────────────────────────────────

def _pos(symbol, side, size, mark):
    return Position(
        exchange=Exchange.BINANCE, symbol=symbol, side=side,
        size=Decimal(str(size)), entry_price=Decimal(str(mark)),
        mark_price=Decimal(str(mark)), leverage=1,
        unrealized_pnl=Decimal("0"), margin=Decimal("0"),
    )


def test_factor_exposure_net_directional():
    # long 1 BTC @ 50k = +50k BTC ; short 10 ETH @ 3k = -30k ETH
    eng = _StubEngine([
        _pos("BTC-USDT", PositionSide.LONG, 1, 50000),
        _pos("ETH-USDT", PositionSide.SHORT, 10, 3000),
    ])
    mon = FactorExposureMonitor(eng)
    snap = asyncio.run(mon.compute_now())
    assert snap.net_btc_usdt == pytest.approx(50000.0)
    assert snap.net_eth_usdt == pytest.approx(-30000.0)
    assert snap.total_notional == pytest.approx(80000.0)


def test_factor_exposure_delta_neutral_hedge_nets_to_zero():
    # funding-arb style: long perp + short spot, same coin → ~0 net BTC delta
    eng = _StubEngine([
        _pos("BTC-USDT", PositionSide.LONG, 1, 50000),
        _pos("BTC-USDT", PositionSide.SHORT, 1, 50000),
    ])
    mon = FactorExposureMonitor(eng)
    snap = asyncio.run(mon.compute_now())
    mon._snapshot = snap
    assert snap.net_btc_usdt == pytest.approx(0.0)
    assert snap.total_notional == pytest.approx(100000.0)   # gross still 100k
    assert mon.get_snapshot()["alert"] == "ok"              # net flat → no alert


def test_factor_exposure_alert_levels():
    eng = _StubEngine([_pos("BTC-USDT", PositionSide.LONG, 1, 50000)])
    mon = FactorExposureMonitor(eng, max_net_exposure_pct=50.0)
    asyncio.run(mon.compute_now())
    mon._snapshot = asyncio.run(mon.compute_now())
    # 100% net long BTC → over 50% threshold → warning
    assert mon.get_snapshot()["alert"] == "warning"
