"""Tests for the REST order-fill poller and FILLED-event dedup in the engine."""
import asyncio
import time
from decimal import Decimal

import pytest

from config.manager import AppConfig, EngineConfig, RiskConfig
from core.engine import TradingEngine
from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType, OrderUpdateEvent,
)
from strategies.base import BaseStrategy


def _engine():
    cfg = AppConfig(exchanges={}, risk=RiskConfig(), engine=EngineConfig())
    return TradingEngine(cfg)


def _filled_order(order_id="oid-1", strategy_id="strat"):
    return Order(
        exchange=Exchange.BINANCE, symbol="BTC-USDT",
        side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"), price=Decimal("60000"),
        order_id=order_id, status=OrderStatus.FILLED,
        filled_qty=Decimal("0.001"), avg_price=Decimal("60000"),
        strategy_id=strategy_id,
    )


class _CountingStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("strat", {})
        self.fills = 0

    def record_fill(self, order):
        self.fills += 1

    def get_status(self):
        return {}


class _FakeConnector:
    """get_order returns a scripted sequence of orders."""
    def __init__(self, orders):
        self._orders = list(orders)
        self.calls = 0

    async def get_order(self, symbol, order_id):
        self.calls += 1
        return self._orders.pop(0) if len(self._orders) > 1 else self._orders[0]


def _drain_one_event(engine):
    """Process a single queued event through the engine's event loop."""
    async def run():
        engine._running = True
        engine._active = True
        loop_task = asyncio.create_task(engine._event_loop())
        await asyncio.sleep(0.05)
        engine._running = False
        await asyncio.wait_for(loop_task, timeout=3)
    asyncio.run(run())


def test_duplicate_filled_event_records_fill_once():
    e = _engine()
    s = _CountingStrategy()
    e.add_strategy(s)
    # Same FILLED order arrives twice (WS push + REST poll race)
    e.event_queue.put_nowait(OrderUpdateEvent(_filled_order()))
    e.event_queue.put_nowait(OrderUpdateEvent(_filled_order()))
    _drain_one_event(e)
    assert s.fills == 1


def test_distinct_orders_both_recorded():
    e = _engine()
    s = _CountingStrategy()
    e.add_strategy(s)
    e.event_queue.put_nowait(OrderUpdateEvent(_filled_order(order_id="a")))
    e.event_queue.put_nowait(OrderUpdateEvent(_filled_order(order_id="b")))
    _drain_one_event(e)
    assert s.fills == 2


def test_poller_emits_event_on_state_change():
    e = _engine()
    open_order = Order(
        exchange=Exchange.BINANCE, symbol="BTC-USDT",
        side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"), price=Decimal("60000"),
        order_id="oid-2", status=OrderStatus.OPEN,
    )
    filled = _filled_order(order_id="oid-2", strategy_id="")
    conn = _FakeConnector([filled])
    e.connectors[Exchange.BINANCE] = conn
    e._open_orders["oid-2"] = {
        "placed_ts": time.time() - 10,
        "exchange": Exchange.BINANCE,
        "symbol": "BTC-USDT",
        "strategy_id": "strat",
        "last_state": (OrderStatus.OPEN.value, "0"),
    }

    async def run():
        e._running = True
        e._active = True
        e.config.engine.order_poll_interval_s = 0.01
        task = asyncio.create_task(e._order_poll_loop())
        await asyncio.sleep(0.1)
        e._running = False
        task.cancel()
    asyncio.run(run())

    assert conn.calls >= 1
    ev = e.event_queue.get_nowait()
    assert isinstance(ev, OrderUpdateEvent)
    assert ev.order.order_id == "oid-2"
    assert ev.order.status == OrderStatus.FILLED
    assert ev.order.strategy_id == "strat"   # backfilled from registry
    # No duplicate event for the unchanged state on subsequent polls
    assert e.event_queue.empty()


def test_poller_skips_unchanged_state():
    e = _engine()
    open_order = Order(
        exchange=Exchange.BINANCE, symbol="BTC-USDT",
        side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"), price=Decimal("60000"),
        order_id="oid-3", status=OrderStatus.OPEN,
    )
    conn = _FakeConnector([open_order])
    e.connectors[Exchange.BINANCE] = conn
    e._open_orders["oid-3"] = {
        "placed_ts": time.time() - 10,
        "exchange": Exchange.BINANCE,
        "symbol": "BTC-USDT",
        "strategy_id": "strat",
        "last_state": (OrderStatus.OPEN.value, "0"),
    }

    async def run():
        e._running = True
        e._active = True
        e.config.engine.order_poll_interval_s = 0.01
        task = asyncio.create_task(e._order_poll_loop())
        await asyncio.sleep(0.1)
        e._running = False
        task.cancel()
    asyncio.run(run())

    assert conn.calls >= 2
    assert e.event_queue.empty()
