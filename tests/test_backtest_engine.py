"""Tests for the event-driven backtest engine:
  - _close_position pure PnL math (long/short, gross vs net-with-fees)
  - _simulate end-to-end replay over synthetic OHLCV, validating fill timing,
    slippage, fee accounting (no double-counting) and trade records
"""
import asyncio
from decimal import Decimal

import pytest

from core.types import Exchange, OrderSide, OrderType, TickerEvent
from data.storage import OHLCVRow
from strategies.base import BaseStrategy
from backtest.engine import BacktestEngine, SimPosition, BacktestJob, TAKER_FEE


# ── _close_position pure math ────────────────────────────────────────────────

def _bt():
    return BacktestEngine(storage=None)


def test_close_long_gross_and_net():
    bt = _bt()
    pos = SimPosition(symbol="BTC-USDT", side="long", size=Decimal("2"),
                      entry_price=Decimal("100"), entry_time=0.0, entry_fee=Decimal("0.1"))
    gross, trade = bt._close_position(pos, close_price=Decimal("110"), ts=3600, exit_fee=Decimal("0.2"))
    assert gross == Decimal("20")                       # (110-100)*2
    assert trade.pnl == pytest.approx(20 - 0.1 - 0.2)   # net of both fees
    assert trade.pnl_pct == pytest.approx(10.0)
    assert trade.fee == pytest.approx(0.3)


def test_close_short_gross_and_net():
    bt = _bt()
    pos = SimPosition(symbol="BTC-USDT", side="short", size=Decimal("1"),
                      entry_price=Decimal("100"), entry_time=0.0, entry_fee=Decimal("0"))
    gross, trade = bt._close_position(pos, close_price=Decimal("90"), ts=3600, exit_fee=Decimal("0"))
    assert gross == Decimal("10")                       # (100-90)*1
    assert trade.pnl_pct == pytest.approx(10.0)
    assert trade.side == "short"


# ── End-to-end _simulate ─────────────────────────────────────────────────────

class _FakeDB:
    """Minimal DataStorage stand-in for the backtest engine."""
    def __init__(self, candles):
        self._candles = candles

    async def get_ohlcv(self, exchange, symbol, interval, start_ts, end_ts, limit=100000):
        return self._candles

    async def save_backtest_job(self, **kw):
        return None


def _candles(prices, start_ts=0, step=3600):
    return [
        OHLCVRow(exchange="binance", symbol="BTC-USDT", interval="1h",
                 ts=start_ts + i * step, open=p, high=p * 1.01, low=p * 0.99,
                 close=p, volume=100.0)
        for i, p in enumerate(prices)
    ]


class _BuyThenSellStrategy(BaseStrategy):
    """Buys 1 unit on the first tick, sells it on the third — one clean round trip."""
    def __init__(self, sid, params):
        super().__init__(sid, params)
        self._n = 0

    async def on_ticker(self, event):
        self._n += 1
        if self._n == 1:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("1"), strategy_id=self.strategy_id)
        elif self._n == 3:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.SELL,
                order_type=OrderType.MARKET, quantity=Decimal("1"), reduce_only=True,
                strategy_id=self.strategy_id)
        return []

    def get_status(self):
        return {}


def _run_sim(candles, **job_kw):
    bt = BacktestEngine(_FakeDB(candles))
    job = BacktestJob(
        job_id="t1", strategy_id="bt", exchange="binance", symbol="BTC-USDT",
        interval="1h", start_ts=0, end_ts=10**12, initial_capital=10000.0,
        params={}, **job_kw,
    )
    return asyncio.run(bt._simulate(job, _BuyThenSellStrategy))


def test_simulate_insufficient_data_raises():
    bt = BacktestEngine(_FakeDB(_candles([100])))
    job = BacktestJob(job_id="t", strategy_id="s", exchange="binance", symbol="BTC-USDT",
                      interval="1h", start_ts=0, end_ts=1, initial_capital=10000.0, params={})
    with pytest.raises(ValueError, match="Insufficient data"):
        asyncio.run(bt._simulate(job, _BuyThenSellStrategy))


def test_simulate_invalid_ohlcv_raises():
    bad = _candles([100, 100])
    bad[1].high = -5    # invalid
    bt = BacktestEngine(_FakeDB(bad))
    job = BacktestJob(job_id="t", strategy_id="s", exchange="binance", symbol="BTC-USDT",
                      interval="1h", start_ts=0, end_ts=1, initial_capital=10000.0, params={})
    with pytest.raises(ValueError, match="Invalid OHLCV"):
        asyncio.run(bt._simulate(job, _BuyThenSellStrategy))


def test_simulate_round_trip_profit_and_fees():
    # buy fills at candle[1].open=100, sell fills at candle[3].open=120 → +20 gross
    metrics = _run_sim(_candles([100, 100, 110, 120, 120]))
    assert metrics.total_trades == 1
    assert metrics.winning_trades == 1
    tr = metrics.trades[0]
    assert tr.entry_price == pytest.approx(100.0)
    assert tr.exit_price == pytest.approx(120.0)
    # net pnl = 20 gross - entry_fee(100*0.0004) - exit_fee(120*0.0004) = 20 - 0.04 - 0.048
    assert tr.pnl == pytest.approx(20 - 0.04 - 0.048, abs=1e-6)
    # final equity reflects the same net (no fee double counting)
    assert metrics.total_return_pct == pytest.approx((tr.pnl / 10000) * 100, abs=1e-6)


def test_simulate_slippage_reduces_pnl():
    base = _run_sim(_candles([100, 100, 110, 120, 120]))
    slipped = _run_sim(_candles([100, 100, 110, 120, 120]), slippage_bps=50)
    # buy fills higher, sell fills lower with slippage → strictly less profit
    assert slipped.trades[0].pnl < base.trades[0].pnl


class _RestingLimitStrategy(BaseStrategy):
    """Buy limit BELOW market (rests, fills as maker when price dips), then a
    sell limit ABOVE market (rests, fills as maker when price rises)."""
    def __init__(self, sid, params):
        super().__init__(sid, params)
        self._n = 0

    async def on_ticker(self, event):
        self._n += 1
        if self._n == 1:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                order_type=OrderType.LIMIT, quantity=Decimal("1"), price=Decimal("95"),
                strategy_id=self.strategy_id)
        elif self._n == 3:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.SELL,
                order_type=OrderType.LIMIT, quantity=Decimal("1"), price=Decimal("125"),
                reduce_only=True, strategy_id=self.strategy_id)
        return []

    def get_status(self):
        return {}


def _run_resting(candles, **job_kw):
    bt = BacktestEngine(_FakeDB(candles))
    job = BacktestJob(
        job_id="m1", strategy_id="bt", exchange="binance", symbol="BTC-USDT",
        interval="1h", start_ts=0, end_ts=10**12, initial_capital=10000.0,
        params={}, **job_kw,
    )
    return asyncio.run(bt._simulate(job, _RestingLimitStrategy))


def test_resting_limit_fills_pay_maker_fee():
    # Prices chosen so each limit rests (open does NOT cross) then is hit
    # intra-candle → maker fill, not taker:
    #   buy@95 placed on candle[0]; candle[1] open=95.5 (>95, not marketable),
    #     low=95.5*0.99=94.5 (≤95) → maker fill at 95.
    #   sell@125 placed on candle[2]; candle[3] open=124 (<125, not marketable),
    #     high=124*1.01=125.24 (≥125) → maker fill at 125.
    candles = _candles([100, 95.5, 110, 124, 124])
    m = _run_resting(candles)
    assert m.total_trades == 1
    tr = m.trades[0]
    assert tr.entry_price == pytest.approx(95.0)
    assert tr.exit_price == pytest.approx(125.0)
    # maker fee = 0.0002 per leg (vs 0.0004 taker); no slippage on maker fills
    expected_fee = 95 * 0.0002 + 125 * 0.0002
    gross = (125 - 95) * 1
    assert tr.pnl == pytest.approx(gross - expected_fee, abs=1e-6)


def test_maker_fee_override_changes_cost():
    candles = _candles([100, 95.5, 110, 124, 124])
    default = _run_resting(candles)
    cheap = _run_resting(candles, maker_fee_bps=0.0)  # zero-fee maker
    assert cheap.trades[0].pnl > default.trades[0].pnl
    # zero maker fee → pnl is exactly the gross spread
    assert cheap.trades[0].pnl == pytest.approx(30.0, abs=1e-6)


class _BuyThenPlainSell(BaseStrategy):
    """Buy on tick 1, then a NON-reduce_only sell on tick 3 → flips to short."""
    def __init__(self, sid, params):
        super().__init__(sid, params)
        self._n = 0

    async def on_ticker(self, event):
        self._n += 1
        if self._n == 1:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("1"), strategy_id=self.strategy_id)
        elif self._n == 3:
            await self.engine.place_order(
                exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.SELL,
                order_type=OrderType.MARKET, quantity=Decimal("1"), strategy_id=self.strategy_id)
        return []

    def get_status(self):
        return {}


def test_plain_sell_flips_to_short_then_closes_at_end():
    # Without reduce_only, the sell closes the long AND opens a short, which is
    # then force-closed at the last candle → two trades (legacy reversal semantics).
    bt = BacktestEngine(_FakeDB(_candles([100, 100, 110, 120, 130])))
    job = BacktestJob(job_id="t", strategy_id="s", exchange="binance", symbol="BTC-USDT",
                      interval="1h", start_ts=0, end_ts=1, initial_capital=10000.0, params={})
    metrics = asyncio.run(bt._simulate(job, _BuyThenPlainSell))
    assert metrics.total_trades == 2          # long round-trip + the flipped short


def test_simulate_no_trades_flat_equity():
    class _Idle(BaseStrategy):
        async def on_ticker(self, event): return []
        def get_status(self): return {}
    bt = BacktestEngine(_FakeDB(_candles([100, 101, 102])))
    job = BacktestJob(job_id="t", strategy_id="s", exchange="binance", symbol="BTC-USDT",
                      interval="1h", start_ts=0, end_ts=1, initial_capital=10000.0, params={})
    metrics = asyncio.run(bt._simulate(job, _Idle))
    assert metrics.total_trades == 0
    assert metrics.total_return_pct == pytest.approx(0.0)
