"""Tests for the basis-aware funding-harvest backtest (backtest/funding_harvest_sim.py)."""
import pytest

from data.storage import OHLCVRow
from backtest.funding_harvest_sim import simulate

H8 = 8 * 3600
START = 1_700_000_000


def _bars(prices):
    return [OHLCVRow(exchange="x", symbol="AAA-USDT", interval="8h",
                     ts=START + i * H8, open=float(p), high=float(p),
                     low=float(p), close=float(p), volume=1.0)
            for i, p in enumerate(prices)]


def _funding(rates):
    return [{"ts": START + i * H8, "rate": r, "next_funding_time": START + i * H8}
            for i, r in enumerate(rates)]


def test_pure_funding_no_basis_no_fees():
    # Flat perp & spot, +10bps funding for 4 settlements, no fees → net = funding only.
    perp = _bars([100, 100, 100, 100])
    spot = _bars([100, 100, 100, 100])
    r = simulate("AAA-USDT", perp, spot, _funding([0.001] * 4),
                 initial_capital=10_000, notional_usdt=10_000, fee_bps_per_leg=0.0)
    assert r.side == "short_perp"                       # positive funding → short perp collects
    # qty = 100; income/settlement = 0.001 * 100 * 100 = 10; 4 settlements = 40
    assert r.funding_collected_usdt == pytest.approx(40.0)
    assert r.basis_pnl_usdt == pytest.approx(0.0)
    assert r.fees_usdt == pytest.approx(0.0)
    assert r.net_pnl_usdt == pytest.approx(40.0)
    assert r.favorable_pct == 100.0


def test_basis_drift_is_isolated_from_funding():
    # Perp rises vs flat spot, zero funding → all PnL is (negative) basis, no carry.
    perp = _bars([100, 102])
    spot = _bars([100, 100])
    r = simulate("AAA-USDT", perp, spot, _funding([0.0, 0.0]),
                 initial_capital=10_000, notional_usdt=10_000, fee_bps_per_leg=0.0)
    # short perp (qty 100): perp +2 costs 200; spot flat → basis pnl = -200
    assert r.basis_pnl_usdt == pytest.approx(-200.0)
    assert r.funding_collected_usdt == pytest.approx(0.0)
    assert r.net_pnl_usdt == pytest.approx(-200.0)


def test_fees_charged_on_all_four_fills():
    perp = _bars([100, 100])
    spot = _bars([100, 100])
    r = simulate("AAA-USDT", perp, spot, _funding([0.0, 0.0]),
                 initial_capital=10_000, notional_usdt=10_000, fee_bps_per_leg=10.0)
    # entry: (100*100 + 100*100)*0.001 = 20; exit: 20 → 40 total
    assert r.fees_usdt == pytest.approx(40.0)
    assert r.net_pnl_usdt == pytest.approx(-40.0)


def test_flipping_funding_shows_adverse_settlements():
    perp = _bars([100, 100, 100, 100])
    spot = _bars([100, 100, 100, 100])
    r = simulate("AAA-USDT", perp, spot, _funding([0.001, -0.001, 0.001, -0.001]),
                 initial_capital=10_000, notional_usdt=10_000, fee_bps_per_leg=0.0)
    assert r.favorable_pct == pytest.approx(50.0)        # half were costs
    assert r.funding_collected_usdt == pytest.approx(0.0)


def test_negative_funding_picks_long_perp_and_still_collects():
    perp = _bars([100, 100, 100])
    spot = _bars([100, 100, 100])
    r = simulate("AAA-USDT", perp, spot, _funding([-0.001, -0.001, -0.001]),
                 initial_capital=10_000, notional_usdt=10_000, fee_bps_per_leg=0.0)
    assert r.side == "long_perp"                          # negative funding → long perp collects
    assert r.funding_collected_usdt > 0
    assert r.favorable_pct == 100.0


def test_requires_overlapping_bars():
    perp = _bars([100, 100])
    spot = [OHLCVRow(exchange="x", symbol="AAA-USDT", interval="8h",
                     ts=START + 999, open=100, high=100, low=100, close=100, volume=1.0)]
    with pytest.raises(ValueError):
        simulate("AAA-USDT", perp, spot, _funding([0.001]))
