"""Tests for cash_carry's amortized fee-viability gate."""
from strategies.cash_carry import CashCarryStrategy


def _strat(**params):
    base = {"taker_fee_bps": 4.0, "fee_multiple": 1.5, "max_hold_hours": 72.0,
            "min_hold_hours": 8.0}
    base.update(params)
    return CashCarryStrategy("cash_carry", base)


def test_old_single_period_projection_was_unreachable():
    """At 1-period amortization the hurdle is 24bps/8h ≈ 262% annualized — the
    bug this fix removes. Confirm a realistic alt rate clears once amortized."""
    s = _strat()
    rate = 0.0006  # 6bps/8h ≈ 65% annualized — plausible for a hot alt
    # max_hold 72h → amortize over 36h = 4.5 periods → 27bps > 24bps hurdle
    ok, detail = s._fee_gate(rate, 8.0, 72.0)
    assert ok, detail


def test_calm_btc_funding_is_correctly_rejected():
    """BTC at ~3.3% annualized (≈0.3bps/8h) must NOT clear the taker hurdle —
    rejecting it is correct, not a regression."""
    s = _strat()
    rate = 0.0000907  # 0.907bps/8h ≈ 9.9% (a high-ish BTC day); still far below
    ok, detail = s._fee_gate(rate, 8.0, 72.0)
    assert not ok, detail


def test_amortization_floored_at_min_hold():
    """fee_amortize_hours below min_hold is floored to min_hold (no free lunch)."""
    s = _strat(fee_amortize_hours=1.0)  # 1h < 8h min → floored to 8h = 1 period
    # 1 period needs rate*1 > 24bps hurdle
    assert s._fee_gate(0.0026, 8.0, 72.0)[0]
    assert not s._fee_gate(0.0020, 8.0, 72.0)[0]


def test_explicit_amortize_hours_override():
    s = _strat(fee_amortize_hours=24.0)  # 3 periods → hurdle 24/3 = 8bps/8h
    assert s._fee_gate(0.0008, 8.0, 72.0)[0]       # 8bps clears
    assert not s._fee_gate(0.0007, 8.0, 72.0)[0]   # 7bps fails
