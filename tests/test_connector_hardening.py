"""Tests for connector production hardening:
precision/lot-size quantization, idempotency keys, rate limiter."""
import asyncio
import time
from decimal import Decimal

import pytest

from connectors.base import (
    SymbolRule, snap_to_step, fmt_decimal, gen_client_order_id,
    AsyncRateLimiter, BaseConnector,
)
from core.types import Exchange, MarketType, OrderSide, OrderType, Order, Position, Balance


# ── #1 precision / lot-size ──────────────────────────────────────────────────

def test_snap_to_step_floors_quantity():
    assert snap_to_step(Decimal("1.23456"), Decimal("0.001")) == Decimal("1.234")
    assert snap_to_step(Decimal("1.2399"), Decimal("0.01")) == Decimal("1.23")
    assert snap_to_step(Decimal("7.9"), Decimal("1")) == Decimal("7")


def test_snap_to_step_zero_step_passthrough():
    assert snap_to_step(Decimal("1.2345"), Decimal("0")) == Decimal("1.2345")


def test_fmt_decimal_no_scientific_notation():
    assert fmt_decimal(Decimal("0.00100000")) == "0.001"
    assert fmt_decimal(Decimal("100")) == "100"
    assert fmt_decimal(Decimal("0.123")) == "0.123"


class _StubConnector(BaseConnector):
    """Minimal concrete connector to exercise _quantize_order."""
    @property
    def exchange(self): return Exchange.BINANCE
    async def connect(self): ...
    async def disconnect(self): ...
    async def subscribe_ticker(self, s): ...
    async def subscribe_orderbook(self, s, depth=20): ...
    async def place_order(self, *a, **k): ...
    async def cancel_order(self, s, o): ...
    async def get_order(self, s, o): ...
    async def get_open_orders(self, s=None): ...
    async def cancel_all_orders(self, s=None): ...
    async def get_positions(self): ...
    async def get_balances(self): ...
    async def set_leverage(self, s, l): ...


def _conn():
    return _StubConnector("k", "s", MarketType.FUTURES)


def test_quantize_snaps_qty_and_price():
    c = _conn()
    c._rules["BTC-USDT"] = SymbolRule(
        tick_size=Decimal("0.1"), step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"), min_notional=Decimal("5"),
    )
    qty, px = c._quantize_order("BTC-USDT", OrderSide.BUY, Decimal("0.0029999"), Decimal("60000.07"))
    assert qty == Decimal("0.002")
    assert px == Decimal("60000.0")   # buy → tick floored


def test_quantize_price_side_aware():
    c = _conn()
    c._rules["BTC-USDT"] = SymbolRule(tick_size=Decimal("0.1"), step_size=Decimal("0.001"),
                                      min_qty=Decimal("0"), min_notional=Decimal("0"))
    _, px_sell = c._quantize_order("BTC-USDT", OrderSide.SELL, Decimal("1"), Decimal("60000.07"))
    assert px_sell == Decimal("60000.1")  # sell → tick ceiled


def test_quantize_rejects_below_min_qty():
    c = _conn()
    c._rules["BTC-USDT"] = SymbolRule(step_size=Decimal("0.001"), min_qty=Decimal("0.01"))
    with pytest.raises(ValueError):
        c._quantize_order("BTC-USDT", OrderSide.BUY, Decimal("0.005"), None)


def test_quantize_rejects_below_min_notional():
    c = _conn()
    c._rules["BTC-USDT"] = SymbolRule(step_size=Decimal("0.001"), min_qty=Decimal("0.001"),
                                      min_notional=Decimal("100"))
    with pytest.raises(ValueError):
        c._quantize_order("BTC-USDT", OrderSide.BUY, Decimal("0.001"), Decimal("60000"))  # =60 < 100


def test_quantize_passthrough_when_no_rule():
    c = _conn()  # no rules loaded
    qty, px = c._quantize_order("ETH-USDT", OrderSide.BUY, Decimal("1.23456789"), Decimal("1234.5678"))
    assert qty == Decimal("1.23456789") and px == Decimal("1234.5678")


# ── #2 idempotency key ───────────────────────────────────────────────────────

def test_client_order_id_format_and_uniqueness():
    ids = {gen_client_order_id() for _ in range(1000)}
    assert len(ids) == 1000                       # unique
    for cid in list(ids)[:50]:
        assert cid.isalnum()                      # OKX: alphanumeric only
        assert len(cid) <= 32                      # OKX: max 32 chars
        assert len(cid) <= 36                      # Binance: max 36 chars


# ── #4 rate limiter ──────────────────────────────────────────────────────────

def test_rate_limiter_throttles_burst():
    async def run():
        # 10 tokens/sec, capacity 3 → 6 acquisitions must take >= ~0.3s
        rl = AsyncRateLimiter(rate_per_sec=10, capacity=3)
        t0 = time.monotonic()
        for _ in range(6):
            await rl.acquire()
        return time.monotonic() - t0
    elapsed = asyncio.run(run())
    assert elapsed >= 0.25   # 3 free, then 3 more at 10/s ≈ 0.3s


def test_rate_limiter_allows_within_capacity():
    async def run():
        rl = AsyncRateLimiter(rate_per_sec=5, capacity=10)
        t0 = time.monotonic()
        for _ in range(10):
            await rl.acquire()
        return time.monotonic() - t0
    elapsed = asyncio.run(run())
    assert elapsed < 0.1   # all within burst capacity, no sleeping
