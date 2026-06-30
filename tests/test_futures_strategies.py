"""Tests for FuturesTrendStrategy, FuturesGridStrategy, FuturesSignalStrategy.

All tests are offline (no network, no real engine). A lightweight _FakeEngine
stub records placed/cancelled orders. cooldown_s=0 is used throughout so
back-to-back signals don't hit the wall-clock guard.
"""
import asyncio
from decimal import Decimal

from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType,
    Ticker, TickerEvent, OrderUpdateEvent,
)
from strategies.futures_trend import FuturesTrendStrategy
from strategies.futures_grid import FuturesGridStrategy
from strategies.futures_signal import FuturesSignalStrategy


# ── Shared stub ──────────────────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self):
        self.regime_detector = None
        self.position_sizer = None
        self.microstructure = None
        self.connectors = {}
        self.placed = []
        self.cancelled = []
        self._n = 0
        self.is_backtest = True  # disable wall-clock guards
        self._sim_time = 0.0

    def now(self):
        return self._sim_time

    async def place_order(self, exchange, symbol, side, order_type, quantity,
                          price=None, reduce_only=False, strategy_id="", post_only=False):
        self._n += 1
        oid = f"o{self._n}"
        status = OrderStatus.OPEN
        self.placed.append(dict(
            exchange=exchange, symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price, reduce_only=reduce_only, order_id=oid,
        ))
        return Order(exchange=exchange, symbol=symbol, side=side, order_type=order_type,
                     quantity=quantity, price=price, order_id=oid, status=status,
                     avg_price=(price or Decimal("0")))

    async def cancel_order(self, exchange, symbol, order_id):
        self.cancelled.append(order_id)
        return True


def _ticker(price, exchange=Exchange.BINANCE, symbol="BTC-USDT"):
    p = Decimal(str(price))
    return Ticker(exchange, symbol, p, p, p, Decimal("0"))


def _tick_event(price, **kwargs):
    return TickerEvent(ticker=_ticker(price, **kwargs))


# ── FuturesTrendStrategy ─────────────────────────────────────────────────────

def _trend(**params):
    defaults = {
        "exchange": "binance", "symbol": "BTC-USDT",
        "fast_period": 3, "slow_period": 5,
        "position_usdt": 10.0, "stop_loss_pct": 2.0, "take_profit_pct": 4.0,
        "direction": "both", "cooldown_s": 0.0,
    }
    defaults.update(params)
    eng = _FakeEngine()
    s = FuturesTrendStrategy("ft_test", defaults)
    s.set_engine(eng)
    return s, eng


def _feed_trend(s, prices):
    async def run():
        for p in prices:
            await s.on_ticker(_tick_event(p))
    asyncio.run(run())


def test_golden_cross_opens_long():
    """fast MA crossing above slow MA triggers a BUY market order."""
    s, eng = _trend()
    # 5 flat prices seed prev_fast/prev_slow as equal; 6th spike makes fast > slow
    _feed_trend(s, [100, 100, 100, 100, 100, 120])
    assert len(eng.placed) == 1
    o = eng.placed[0]
    assert o["side"] == OrderSide.BUY
    assert o["order_type"] == OrderType.MARKET
    assert o["reduce_only"] is False
    assert s._position_side == "long"


def test_death_cross_opens_short():
    """fast MA crossing below slow MA triggers a SELL market order."""
    s, eng = _trend()
    _feed_trend(s, [100, 100, 100, 100, 100, 80])
    assert len(eng.placed) == 1
    o = eng.placed[0]
    assert o["side"] == OrderSide.SELL
    assert o["order_type"] == OrderType.MARKET
    assert s._position_side == "short"


def test_stop_loss_closes_long():
    """Price falling below entry*(1-stop_loss_pct/100) closes the long position."""
    s, eng = _trend(stop_loss_pct=2.0)
    _feed_trend(s, [100, 100, 100, 100, 100, 120])  # open long ~120
    assert s._position_side == "long"
    assert s._entry_price == 120.0

    # Feed price below 120 * 0.98 = 117.6
    _feed_trend(s, [117.0])
    close_orders = [o for o in eng.placed if o["reduce_only"]]
    assert len(close_orders) == 1
    assert close_orders[0]["side"] == OrderSide.SELL
    assert s._position_side is None


def test_take_profit_closes_long():
    """Price rising above entry*(1+take_profit_pct/100) closes the long position."""
    s, eng = _trend(take_profit_pct=4.0)
    _feed_trend(s, [100, 100, 100, 100, 100, 120])  # open long ~120
    assert s._position_side == "long"

    # Feed price above 120 * 1.04 = 124.8
    _feed_trend(s, [125.0])
    close_orders = [o for o in eng.placed if o["reduce_only"]]
    assert len(close_orders) == 1
    assert close_orders[0]["side"] == OrderSide.SELL
    assert s._position_side is None


def test_direction_long_only_blocks_short():
    """direction='long_only' suppresses death-cross short entries."""
    s, eng = _trend(direction="long_only")
    _feed_trend(s, [100, 100, 100, 100, 100, 80])  # death cross
    assert len(eng.placed) == 0
    assert s._position_side is None


def test_price_samples_needed_in_status():
    """get_status reports price_samples_needed = slow_period + 2."""
    s, _ = _trend(slow_period=5)
    status = s.get_status()
    assert status["price_samples_needed"] == 7  # slow(5) + 2


# ── FuturesGridStrategy ───────────────────────────────────────────────────────

def _grid_strat(**params):
    defaults = {
        "exchange": "binance", "symbol": "BTC-USDT",
        "grid_low": 90.0, "grid_high": 110.0, "grid_count": 4,
        "grid_usdt": 10.0, "mode": "neutral",
    }
    defaults.update(params)
    eng = _FakeEngine()
    s = FuturesGridStrategy("fg_test", defaults)
    s.set_engine(eng)
    return s, eng


def test_futures_grid_build_correct_levels():
    """Build grid returns grid_count+1 price boundaries spanning [low, high]."""
    s, _ = _grid_strat(grid_low=100.0, grid_high=200.0, grid_count=5)
    prices = s._build_grid()
    assert len(prices) == 6   # count+1 boundaries
    assert prices[0] == Decimal("100.00")
    assert prices[-1] == Decimal("200.00")


def test_futures_grid_inactive_when_bounds_zero():
    """grid_low=0 → _build_grid returns [] → strategy stays uninitialized."""
    s, eng = _grid_strat(grid_low=0.0, grid_high=0.0)
    s.enable()

    async def run():
        await s.on_ticker(_tick_event(100))
    asyncio.run(run())

    assert not s._initialized
    assert len(eng.placed) == 0


def test_futures_grid_enable_resets_state():
    """Re-enabling after disable resets _initialized so the grid will re-setup."""
    s, _ = _grid_strat()
    # Manually mark as initialized with a stale order
    s._initialized = True
    s._open_orders[Decimal("95")] = "stale_oid"
    s._order_map["stale_oid"] = (OrderSide.BUY, Decimal("95"), False)

    s.disable()
    s.enable()

    assert not s._initialized
    assert len(s._open_orders) == 0
    assert len(s._order_map) == 0


def test_futures_grid_runaway_not_tripped_initially():
    s, _ = _grid_strat()
    assert not s._runaway_tripped
    assert s.get_status()["runaway_tripped"] is False


def test_futures_grid_mode_in_status():
    s, _ = _grid_strat(mode="long")
    assert s.get_status()["mode"] == "long"


# ── FuturesSignalStrategy ─────────────────────────────────────────────────────

def _signal_strat(**params):
    defaults = {
        "exchange": "binance", "symbol": "BTC-USDT",
        "position_usdt": 10.0, "signal_type": "rsi",
        "rsi_period": 3, "rsi_oversold": 30.0, "rsi_overbought": 70.0,
        "stop_loss_pct": 2.0, "take_profit_pct": 6.0,
        "direction": "both", "cooldown_s": 0.0,
    }
    defaults.update(params)
    eng = _FakeEngine()
    s = FuturesSignalStrategy("fs_test", defaults)
    s.set_engine(eng)
    return s, eng


def _feed_signal(s, prices):
    async def run():
        for p in prices:
            await s.on_ticker(_tick_event(p))
    asyncio.run(run())


def test_rsi_oversold_opens_long():
    """RSI < oversold threshold (all prices falling → RSI≈0) triggers a BUY."""
    s, eng = _signal_strat(rsi_period=3, rsi_oversold=30.0)
    # 4 prices going down: period+1 prices → RSI seeded with avg_gain=0 → RSI=0
    _feed_signal(s, [100, 99, 98, 97])
    assert len(eng.placed) == 1
    assert eng.placed[0]["side"] == OrderSide.BUY
    assert s._position_side == "long"


def test_rsi_overbought_opens_short():
    """RSI > overbought threshold (all prices rising → RSI≈100) triggers a SELL."""
    s, eng = _signal_strat(rsi_period=3, rsi_overbought=70.0)
    _feed_signal(s, [100, 101, 102, 103])
    assert len(eng.placed) == 1
    assert eng.placed[0]["side"] == OrderSide.SELL
    assert s._position_side == "short"


def test_breakout_up_opens_long():
    """Price breaking above N-bar high fires a BUY."""
    s, eng = _signal_strat(signal_type="breakout", breakout_period=3)
    # Feed 4 prices: lookback high = max(100,101,102)=102; current 110 > 102 → long
    _feed_signal(s, [100, 101, 102, 110])
    assert len(eng.placed) == 1
    assert eng.placed[0]["side"] == OrderSide.BUY


def test_breakout_down_opens_short():
    """Price breaking below N-bar low fires a SELL."""
    s, eng = _signal_strat(signal_type="breakout", breakout_period=3)
    # Lookback low = min(100,99,98)=98; current 85 < 98 → short
    _feed_signal(s, [100, 99, 98, 85])
    assert len(eng.placed) == 1
    assert eng.placed[0]["side"] == OrderSide.SELL


def test_ma_cross_signal_opens_long():
    """ma_cross signal: fast MA crossing above slow MA opens long."""
    s, eng = _signal_strat(signal_type="ma_cross", fast_period=3, slow_period=5, cooldown_s=0.0)
    # Same price sequence used for FuturesTrend golden-cross test
    _feed_signal(s, [100, 100, 100, 100, 100, 120])
    assert len(eng.placed) == 1
    assert eng.placed[0]["side"] == OrderSide.BUY


def test_signal_stop_loss_closes_long():
    """Stop-loss fires when price drops below entry*(1-sl%)."""
    s, eng = _signal_strat(rsi_period=3, rsi_oversold=30.0, stop_loss_pct=2.0)
    _feed_signal(s, [100, 99, 98, 97])  # opens long ~97
    assert s._position_side == "long"
    entry = s._entry_price
    sl_price = entry * (1 - 0.02) - 0.01
    _feed_signal(s, [sl_price])
    close_orders = [o for o in eng.placed if o["reduce_only"]]
    assert len(close_orders) == 1
    assert close_orders[0]["side"] == OrderSide.SELL
    assert s._position_side is None


def test_price_samples_needed_rsi():
    s, _ = _signal_strat(signal_type="rsi", rsi_period=14)
    assert s.get_status()["price_samples_needed"] == 15  # rsi_period + 1


def test_price_samples_needed_breakout():
    s, _ = _signal_strat(signal_type="breakout", breakout_period=20)
    assert s.get_status()["price_samples_needed"] == 21  # breakout_period + 1


def test_price_samples_needed_ma_cross():
    s, _ = _signal_strat(signal_type="ma_cross", slow_period=30)
    assert s.get_status()["price_samples_needed"] == 32  # slow_period + 2
