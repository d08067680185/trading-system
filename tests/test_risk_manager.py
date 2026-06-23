"""Unit tests for RiskManager — the most critical module."""
import pytest
from decimal import Decimal
from unittest.mock import MagicMock
from risk.manager import RiskManager, RiskConfig, RiskState
from core.types import (
    Exchange, OrderSide, OrderStatus, OrderType, Signal, Order,
)


def make_config(**kwargs) -> RiskConfig:
    defaults = {
        "max_position_usdt":  Decimal("1000"),
        "max_order_usdt":     Decimal("100"),
        "max_daily_loss_usdt": Decimal("50"),
        "max_open_orders":    5,
        "enabled":            True,
        "max_drawdown_pct":   Decimal("0"),
        "max_symbol_concentration_pct": Decimal("0"),
        "max_rolling_7d_loss_usdt": Decimal("0"),
        "max_rolling_30d_loss_usdt": Decimal("0"),
    }
    defaults.update(kwargs)
    return RiskConfig(**defaults)


def make_signal(qty=Decimal("1"), price=Decimal("100"), reduce_only=False) -> Signal:
    return Signal(
        exchange=Exchange.BINANCE,
        symbol="BTC-USDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=price,
        reduce_only=reduce_only,
        strategy_id="test_strategy",
    )


def make_fill(order_id="ord1", side=OrderSide.BUY, qty=Decimal("1"),
              price=Decimal("100"), fee=Decimal("0.1")) -> Order:
    return Order(
        exchange=Exchange.BINANCE,
        symbol="BTC-USDT",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=price,
        order_id=order_id,
        status=OrderStatus.FILLED,
        filled_qty=qty,
        avg_price=price,
        fee=fee,
    )


# ── check_signal ──────────────────────────────────────────────────────────────

def test_check_signal_passes_when_within_limits():
    rm = RiskManager(make_config())
    assert rm.check_signal(make_signal()) is True


def test_check_signal_blocked_when_halted():
    rm = RiskManager(make_config())
    rm.halt("test")
    assert rm.check_signal(make_signal()) is False


def test_check_signal_blocked_by_daily_loss():
    rm = RiskManager(make_config(max_daily_loss_usdt=Decimal("10")))
    rm.state.daily_pnl = Decimal("-10.01")
    assert rm.check_signal(make_signal()) is False


def test_check_signal_blocked_by_order_size():
    rm = RiskManager(make_config(max_order_usdt=Decimal("50")))
    signal = make_signal(qty=Decimal("1"), price=Decimal("100"))  # notional = 100 > 50
    assert rm.check_signal(signal) is False


def test_check_signal_blocked_by_open_orders():
    rm = RiskManager(make_config(max_open_orders=2))
    rm.state._open_order_ids = {"a", "b"}
    assert rm.check_signal(make_signal()) is False


def test_check_signal_blocked_by_position_limit():
    rm = RiskManager(make_config(
        max_position_usdt=Decimal("150"),
        max_order_usdt=Decimal("200"),
    ))
    rm.state.position_notionals[(Exchange.BINANCE.value, "BTC-USDT")] = Decimal("100")
    signal = make_signal(qty=Decimal("1"), price=Decimal("100"))  # would add 100, total 200 > 150
    assert rm.check_signal(signal) is False


def test_reduce_only_bypasses_position_limit():
    rm = RiskManager(make_config(max_position_usdt=Decimal("50")))
    rm.state.position_notionals[(Exchange.BINANCE.value, "BTC-USDT")] = Decimal("100")
    signal = make_signal(qty=Decimal("1"), price=Decimal("100"), reduce_only=True)
    assert rm.check_signal(signal) is True


# ── Daily PnL tracking ────────────────────────────────────────────────────────

def test_daily_pnl_buy_then_sell():
    rm = RiskManager(make_config())
    buy  = make_fill("b1", OrderSide.BUY,  Decimal("1"), Decimal("100"), fee=Decimal("0.04"))
    sell = make_fill("s1", OrderSide.SELL, Decimal("1"), Decimal("110"), fee=Decimal("0.044"))
    rm.record_order_update(buy)
    rm.record_order_update(sell)
    # profit = 110 - 100 = 10, minus fees = 10 - 0.04 - 0.044 = 9.916
    assert rm.state.daily_pnl == pytest.approx(Decimal("9.916"), rel=Decimal("0.01"))


def test_daily_pnl_short_then_cover():
    rm = RiskManager(make_config())
    sell  = make_fill("s1", OrderSide.SELL, Decimal("1"), Decimal("100"), fee=Decimal("0.04"))
    cover = make_fill("b1", OrderSide.BUY,  Decimal("1"), Decimal("90"),  fee=Decimal("0.036"))
    rm.record_order_update(sell)
    rm.record_order_update(cover)
    # profit = 100 - 90 = 10, minus fees
    assert rm.state.daily_pnl > Decimal("9.5")


def test_fee_deducted_on_open():
    rm = RiskManager(make_config())
    buy = make_fill("b1", OrderSide.BUY, Decimal("1"), Decimal("100"), fee=Decimal("0.04"))
    rm.record_order_update(buy)
    # Opening long: only fee deducted, no realized PnL yet
    assert rm.state.daily_pnl == Decimal("-0.04")


# ── Rolling PnL ───────────────────────────────────────────────────────────────

def test_rolling_pnl_sums_history():
    rm = RiskManager(make_config())
    import time
    # Simulate 3 days of history
    for i in range(3):
        rm.state._daily_pnl_history.append((time.time() - (3 - i) * 86400, -10.0))
    rm.state.daily_pnl = Decimal("-5")
    rolling = rm.state.rolling_pnl(7)
    assert rolling == pytest.approx(Decimal("-35"), rel=Decimal("0.01"))


def test_rolling_7d_limit_blocks_signal():
    rm = RiskManager(make_config(max_rolling_7d_loss_usdt=Decimal("25")))
    import time
    rm.state._daily_pnl_history.append((time.time() - 86400, -20.0))
    rm.state.daily_pnl = Decimal("-10")  # rolling total = -30 > limit
    assert rm.check_signal(make_signal(price=Decimal("10"))) is False


# ── Drawdown control ──────────────────────────────────────────────────────────

def test_drawdown_halts_at_threshold():
    rm = RiskManager(make_config(max_drawdown_pct=Decimal("20")))
    rm.state.peak_equity_usdt = 1000.0
    rm.update_equity(790.0)   # 21% drawdown
    assert rm.is_halted is True


def test_drawdown_does_not_halt_below_threshold():
    rm = RiskManager(make_config(max_drawdown_pct=Decimal("20")))
    rm.state.peak_equity_usdt = 1000.0
    rm.update_equity(850.0)   # 15% drawdown
    assert rm.is_halted is False


# ── Deduplication ─────────────────────────────────────────────────────────────

def test_duplicate_fill_events_not_double_counted():
    rm = RiskManager(make_config())
    sell = make_fill("s1", OrderSide.SELL, Decimal("1"), Decimal("100"), fee=Decimal("0.04"))
    cover = make_fill("b1", OrderSide.BUY, Decimal("1"), Decimal("90"), fee=Decimal("0.036"))
    rm.record_order_update(sell)
    rm.record_order_update(cover)
    first_pnl = rm.state.daily_pnl
    # Sending same events again should not change PnL (orders are done, removed from set)
    rm.record_order_update(sell)
    rm.record_order_update(cover)
    # PnL should be the same (duplicate terminal events ignored once order_id removed from set)
    assert rm.state.daily_pnl == first_pnl


# ── Resume preserves daily_pnl ────────────────────────────────────────────────

def test_resume_preserves_daily_pnl():
    rm = RiskManager(make_config(max_daily_loss_usdt=Decimal("10")))
    rm.state.daily_pnl = Decimal("-8")
    rm.halt("test")
    rm.resume()
    assert rm.state.daily_pnl == Decimal("-8")
    assert rm.is_halted is False


# ── Status dict ───────────────────────────────────────────────────────────────

def test_status_contains_expected_keys():
    rm = RiskManager(make_config())
    s = rm.status()
    for key in ("halted", "daily_pnl_usdt", "rolling_7d_pnl_usdt",
                "rolling_30d_pnl_usdt", "limits"):
        assert key in s, f"Missing key: {key}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
