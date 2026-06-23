"""Tests for the funding-harvest analyzer (backtest/funding_harvest.py)."""
import asyncio

from backtest.funding_harvest import analyze, rank, FundingHarvestAnalyzer

H8 = 8 * 3600


def _rows(rates, start=1_700_000_000):
    return [{"ts": start + i * H8, "rate": r} for i, r in enumerate(rates)]


def test_analyze_positive_persistent_funding():
    # Steady +10 bps/8h (0.001) for 30 periods → short the perp, collect every period.
    res = analyze("binance", "AAA-USDT", _rows([0.001] * 30), fee_bps_per_leg=4.0)
    assert res.dominant_sign == 1            # positive funding → short perp collects
    assert res.favorable_pct == 100.0
    assert abs(res.mean_abs_rate_bps - 10.0) < 1e-6
    # gross annualized = 0.001 * 3 * 365 * 100 = 109.5%
    assert abs(res.gross_annual_pct - 109.5) < 1e-3
    assert res.net_annual_pct < res.gross_annual_pct   # fee drag subtracted


def test_analyze_negative_funding_picks_long_side():
    res = analyze("binance", "BBB-USDT", _rows([-0.0005] * 20))
    assert res.dominant_sign == -1           # negative funding → long perp collects
    assert res.favorable_pct == 100.0
    assert res.gross_annual_pct > 0          # collected on the committed (long) side


def test_analyze_flipping_funding_low_favorable():
    # Alternating sign, net ~0 → harvest mostly cancels, favorable ~50%.
    res = analyze("binance", "CCC-USDT", _rows([0.001, -0.001] * 15))
    assert 40.0 <= res.favorable_pct <= 60.0
    assert abs(res.gross_annual_pct) < 20.0  # near-zero net collection


def test_analyze_too_few_rows_returns_none():
    assert analyze("binance", "DDD-USDT", _rows([0.001])) is None


def test_rank_orders_by_net_and_filters_min_periods():
    rows_by_symbol = {
        "HIGH": _rows([0.002] * 30),
        "LOW":  _rows([0.0002] * 30),
        "SHORT": _rows([0.01] * 5),     # huge but too few periods → filtered
    }
    out = rank(rows_by_symbol, "binance", min_periods=10, top_n=10)
    syms = [r.symbol for r in out]
    assert syms == ["HIGH", "LOW"]       # SHORT filtered, HIGH ranks above LOW


def test_analyzer_scan_groups_by_symbol():
    class _FakeDB:
        async def get_funding_rates(self, exchange, start_ts, limit):
            return (
                [{"symbol": "AAA-USDT", "ts": 1_700_000_000 + i * H8, "rate": 0.001} for i in range(20)]
                + [{"symbol": "BBB-USDT", "ts": 1_700_000_000 + i * H8, "rate": 0.0001} for i in range(20)]
            )

    out = asyncio.run(FundingHarvestAnalyzer(_FakeDB()).scan(
        min_periods=10, min_span_days=0, top_n=10))
    assert [r["symbol"] for r in out] == ["AAA-USDT", "BBB-USDT"]
    assert out[0]["net_annual_pct"] > out[1]["net_annual_pct"]


def _settle_rows(rates, nft_start=1_700_000_000, step=H8):
    """Rows carrying next_funding_time so the analyzer dedupes to real settlements."""
    return [{"ts": nft_start + i * step - 60, "rate": r, "next_funding_time": nft_start + i * step}
            for i, r in enumerate(rates)]


def test_polled_samples_dedupe_to_real_settlements():
    # Same settlement polled 5×: 5 rows, but only ONE real settlement counts.
    nft = 1_700_000_000 + H8
    rows = [{"ts": nft - 300 + k, "rate": 0.001, "next_funding_time": nft} for k in range(5)]
    rows += [{"ts": nft + H8 - 100, "rate": 0.002, "next_funding_time": nft + H8}]
    res = analyze("binance", "AAA-USDT", rows)
    assert res.n_periods == 2                # two settlements, not six polls
    assert abs(res.mean_abs_rate_bps - 15.0) < 1e-6   # mean(10, 20) bps, not poll-weighted


def test_cadence_derived_from_settlement_gap():
    # 4h funding (step = 4h) → ~2190 settlements/year, so annualization doubles vs 8h.
    res4 = analyze("binance", "FAST-USDT", _settle_rows([0.001] * 30, step=4 * 3600))
    res8 = analyze("binance", "SLOW-USDT", _settle_rows([0.001] * 30, step=H8))
    assert 2000 <= res4.settlements_per_year <= 2400
    assert 900 <= res8.settlements_per_year <= 1200
    assert res4.gross_annual_pct > 1.9 * res8.gross_annual_pct


def test_short_span_filtered_as_noise():
    # A high rate over only ~1.3 days should be dropped by min_span_days.
    rows_by_symbol = {
        "SHORTWIN": _settle_rows([0.02] * 10, step=H8),    # 10 settlements, ~3 days
        "LONGWIN":  _settle_rows([0.001] * 60, step=H8),   # 60 settlements, ~19.7 days
    }
    out = rank(rows_by_symbol, "binance", min_periods=5, min_span_days=7)
    assert [r.symbol for r in out] == ["LONGWIN"]          # SHORTWIN dropped despite huge rate


def test_min_favorable_pct_drops_flippy_funding():
    rows_by_symbol = {
        "STABLE": _settle_rows([0.001] * 40, step=H8),          # 100% favorable
        "FLIPPY": _settle_rows([0.001, -0.001] * 20, step=H8),  # ~50% favorable
    }
    out = rank(rows_by_symbol, "binance", min_periods=5, min_span_days=0, min_favorable_pct=80)
    assert [r.symbol for r in out] == ["STABLE"]
