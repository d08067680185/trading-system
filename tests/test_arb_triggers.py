"""Tests for spread-arb trigger-quality logging."""
import asyncio
from decimal import Decimal

from core.types import Exchange, OrderSide
from data.storage import DataStorage
from strategies.spread_arb import SpreadArbStrategy, _ArbLeg, _OpenArb


def test_arb_trigger_storage_roundtrip():
    async def run():
        st = DataStorage(":memory:")
        await st.connect()
        tid = await st.record_arb_trigger(
            "arb_spread", "BTC-USDT", 6.2, 5.0, "maker2", "okx", "binance"
        )
        await st.update_arb_trigger(tid, "completed", legs_filled=2,
                                    realized_bps=4.8, duration_s=3.2)
        rows = await st.get_arb_triggers()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "completed"
        assert rows[0]["legs_filled"] == 2
        assert rows[0]["realized_bps"] == 4.8

        stats = await st.get_arb_trigger_stats(hours=1.0)
        assert stats["total_triggers"] == 1
        assert stats["completed"] == 1
        assert stats["completion_rate"] == 1.0
        await st.close()
    asyncio.run(run())


def _leg(side, fill_price):
    leg = _ArbLeg("oid", Exchange.BINANCE, "BTC-USDT", side, Decimal("0.001"), 0.0)
    leg.filled = True
    leg.fill_price = Decimal(str(fill_price))
    return leg


def test_realized_bps_maker_mode():
    s = SpreadArbStrategy("arb_spread", {"maker_both_legs": True, "maker_fee_bps": 0.0})
    arb = _OpenArb([_leg(OrderSide.BUY, 60000), _leg(OrderSide.SELL, 60060)], "BTC-USDT")
    # (60060-60000)/60000 * 1e4 = 10 bps gross, maker cost 0 → 10 net
    assert abs(s._realized_bps(arb) - 10.0) < 0.01


def test_realized_bps_taker_mode_subtracts_fees():
    s = SpreadArbStrategy("arb_spread", {"maker_both_legs": False, "fee_bps": 4.0})
    arb = _OpenArb([_leg(OrderSide.BUY, 60000), _leg(OrderSide.SELL, 60060)], "BTC-USDT")
    # 10 bps gross - 8 bps fees = 2 net
    assert abs(s._realized_bps(arb) - 2.0) < 0.01


def test_realized_bps_none_when_leg_missing_price():
    s = SpreadArbStrategy("arb_spread", {})
    incomplete = _ArbLeg("x", Exchange.OKX, "BTC-USDT", OrderSide.SELL, Decimal("0.001"), 0.0)
    arb = _OpenArb([_leg(OrderSide.BUY, 60000), incomplete], "BTC-USDT")
    assert s._realized_bps(arb) is None
