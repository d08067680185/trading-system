"""Unit tests for PositionSizer."""
import pytest
import math
from risk.position_sizer import PositionSizer


def test_size_zero_without_data():
    ps = PositionSizer(min_size_usdt=5.0)
    # No price data → fallback to conservative sizing
    size = ps.get_size_usdt("BTC-USDT", 10000)
    assert 5.0 <= size <= 500.0


def test_size_scales_with_capital():
    ps = PositionSizer(target_vol_pct=0.01, lookback=5)
    for p in [100, 101, 99, 102, 98]:
        ps.update_price("BTC-USDT", p)
    size_small = ps.get_size_usdt("BTC-USDT", 1000)
    size_large  = ps.get_size_usdt("BTC-USDT", 10000)
    assert size_large > size_small, "Size should scale with capital"


def test_size_respects_min_max():
    ps = PositionSizer(min_size_usdt=10.0, max_size_usdt=100.0)
    for p in [100, 101, 99, 102, 98]:
        ps.update_price("BTC-USDT", p)
    size = ps.get_size_usdt("BTC-USDT", 1_000_000)
    assert size >= 10.0
    assert size <= 100.0


def test_vol_increases_with_wider_swings():
    ps = PositionSizer(lookback=5)
    for p in [100, 100, 100, 100, 100]:
        ps.update_price("BTC-USDT", p)
    low_vol = ps.get_vol("BTC-USDT")

    ps2 = PositionSizer(lookback=5)
    for p in [100, 110, 90, 115, 85]:
        ps2.update_price("BTC-USDT", p)
    high_vol = ps2.get_vol("BTC-USDT")

    assert high_vol > low_vol, "Higher swings should produce higher vol estimate"


def test_regime_multiplier_reduces_size_at_high_vol():
    ps = PositionSizer(lookback=5)
    # Simulate very high vol
    for p in [100, 130, 70, 140, 60]:
        ps.update_price("BTC-USDT", p)
    mult = ps.get_vol_regime_multiplier("BTC-USDT")
    assert mult < 1.0, "High vol should give multiplier < 1 (reduce size)"


def test_kelly_positive_edge():
    ps = PositionSizer()
    size = ps.get_kelly_size_usdt(
        capital_usdt=10000,
        win_rate=0.6,
        avg_win_usdt=100,
        avg_loss_usdt=50,
        kelly_fraction=0.25,
    )
    assert size > 0


def test_kelly_zero_edge():
    ps = PositionSizer(min_size_usdt=5.0)
    size = ps.get_kelly_size_usdt(
        capital_usdt=10000,
        win_rate=0.3,   # clearly negative edge
        avg_win_usdt=10,
        avg_loss_usdt=50,
        kelly_fraction=0.25,
    )
    assert size == 5.0  # floored at min_size


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
