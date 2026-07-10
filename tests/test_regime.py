"""Tests for RegimeDetector hysteresis + debounce (anti-flapping)."""
import time
import pytest

from signals.regime import RegimeDetector, LOW, NORMAL, HIGH, EXTREME


def test_classify_raw_boundaries():
    d = RegimeDetector()
    assert d._classify_raw(10) == LOW
    assert d._classify_raw(50) == NORMAL
    assert d._classify_raw(80) == HIGH
    assert d._classify_raw(95) == EXTREME


def test_hysteresis_holds_regime_near_boundary():
    # margin 7 → NORMAL stays NORMAL until pct >= 82, won't drop until pct < 18
    d = RegimeDetector(hysteresis_pct=7.0)
    assert d._classify(NORMAL, 76) == NORMAL    # just over 75 boundary, within buffer
    assert d._classify(NORMAL, 74) == NORMAL
    assert d._classify(NORMAL, 82) == HIGH      # clears 75 + 7
    assert d._classify(HIGH, 74) == HIGH        # within buffer, doesn't drop back
    assert d._classify(HIGH, 67) == NORMAL      # clears 75 - 7


def test_escalation_can_skip_multiple_bands():
    d = RegimeDetector(hysteresis_pct=7.0)
    assert d._classify(LOW, 99) == EXTREME      # jumps all the way up


def _feed_alternating(d, symbol, n, lo_price, hi_price):
    """Feed prices that whipsaw to produce noisy vol, counting regime flips."""
    flips = 0
    last = None
    for i in range(n):
        p = hi_price if i % 2 == 0 else lo_price
        snap = d.update(symbol, p)
        if snap and last is not None and snap.regime != last:
            flips += 1
        if snap:
            last = snap.regime
    return flips


def test_debounce_blocks_rapid_de_escalation():
    # min_dwell large → once escalated, cannot relax within the window
    d = RegimeDetector(min_dwell_s=1000.0, min_data=3, sample_interval_s=0.0)
    sym = "BTC-USDT"
    # build a vol history, then force an escalation
    for p in (100, 101, 100, 103, 99, 105, 98):
        d.update(sym, p)
    before = d.get_regime(sym)
    # a calm tick that would normally de-escalate
    d.update(sym, 100.0001)
    d.update(sym, 100.0002)
    after = d.get_regime(sym)
    # de-escalation suppressed within dwell window (regime cannot drop)
    from signals.regime import _ORDER
    assert _ORDER[after] >= _ORDER[before] or after == before


def test_no_prev_uses_raw():
    d = RegimeDetector()
    assert d._classify(None, 95) == EXTREME
    assert d._classify(None, 10) == LOW


def test_de_escalation_steps_one_band_at_a_time():
    # A single calm sample can NOT drop extreme straight to low — one band max
    d = RegimeDetector(hysteresis_pct=7.0)
    assert d._classify(EXTREME, 5) == HIGH
    assert d._classify(HIGH, 5) == NORMAL
    assert d._classify(NORMAL, 5) == LOW


def test_de_escalation_hysteresis_buffer():
    # EXTREME lower bound is 90; with margin 7, drop only below 83
    d = RegimeDetector(hysteresis_pct=7.0)
    assert d._classify(EXTREME, 80) == HIGH      # below 83 → one band down
    assert d._classify(EXTREME, 85) == EXTREME   # within buffer → hold


def test_sampling_throttles_tick_flood():
    # 100 ticks in the same instant → only the first is sampled
    d = RegimeDetector(min_data=3, sample_interval_s=60.0)
    accepted = 0
    for i in range(100):
        d.update("BTC-USDT", 100 + (i % 2))
        accepted = len(d._prices.get("BTC-USDT", []))
    assert accepted == 1


def test_sampling_disabled_processes_every_tick():
    d = RegimeDetector(min_data=3, sample_interval_s=0.0)
    for i in range(10):
        d.update("BTC-USDT", 100 + (i % 3))
    assert len(d._prices["BTC-USDT"]) == 10
