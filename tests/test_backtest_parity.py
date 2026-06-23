"""
Parity backtest tests: a strategy runs through the *real* TradingEngine +
RiskManager over a SimulatedConnector. These cover the things the legacy
single-net-position backtest engine could not: layered grid inventory, exact
reduce_only semantics, and real risk-gate enforcement during a backtest.
"""
import asyncio
from decimal import Decimal

from core.types import Exchange, OrderSide, OrderType, Signal, TickerEvent
from data.storage import OHLCVRow
from risk.manager import RiskConfig
from backtest.runner import BacktestRunner
from strategies.base import BaseStrategy
from strategies.grid import SpotGridStrategy


def _bars(prices, exchange="binance", symbol="BTC-USDT", interval="1h", start=1_700_000_000):
    """Build OHLCV bars from a price path. Each bar's high/low bracket the move
    from its open to the next price so limit levels in between get crossed."""
    bars = []
    step = 3600
    for i in range(len(prices) - 1):
        o = float(prices[i])
        c = float(prices[i + 1])
        bars.append(OHLCVRow(
            exchange=exchange, symbol=symbol, interval=interval,
            ts=start + i * step, open=o, high=max(o, c), low=min(o, c), close=c, volume=1.0,
        ))
    # final bar holds flat so the close-out has a clean mark
    o = float(prices[-1])
    bars.append(OHLCVRow(
        exchange=exchange, symbol=symbol, interval=interval,
        ts=start + (len(prices) - 1) * step, open=o, high=o, low=o, close=o, volume=1.0,
    ))
    return bars


class _ScriptStrategy(BaseStrategy):
    """Places one entry at the `buy_at`-th ticker and a close at the `sell_at`-th.
    Orders fill at the *next* bar open (market) or when crossed (limit)."""

    def __init__(self, strategy_id, params):
        defaults = {
            "buy_at": 1, "entry_type": "market", "entry_price": None, "entry_qty": 1.0,
            "sell_at": 3, "sell_qty": 1.0, "sell_reduce_only": True,
            "exchange": "binance", "symbol": "BTC-USDT",
        }
        defaults.update(params)
        super().__init__(strategy_id, defaults)
        self._n = 0

    async def on_ticker(self, event: TickerEvent):
        self._n += 1
        t = event.ticker
        out = []
        if self._n == self.params["buy_at"]:
            otype = (OrderType.LIMIT if self.params["entry_type"] == "limit"
                     else OrderType.MARKET)
            price = (Decimal(str(self.params["entry_price"]))
                     if self.params["entry_price"] is not None else None)
            out.append(Signal(
                exchange=t.exchange, symbol=t.symbol, side=OrderSide.BUY,
                order_type=otype, quantity=Decimal(str(self.params["entry_qty"])),
                price=price, strategy_id=self.strategy_id,
            ))
        if self._n == self.params["sell_at"]:
            out.append(Signal(
                exchange=t.exchange, symbol=t.symbol, side=OrderSide.SELL,
                order_type=OrderType.MARKET, quantity=Decimal(str(self.params["sell_qty"])),
                reduce_only=bool(self.params["sell_reduce_only"]), strategy_id=self.strategy_id,
            ))
        return out

    def get_status(self):
        return {}


def test_market_entry_exit_pnl_is_exact():
    """A market buy @100 then a reduce_only market sell @120, qty 1, 10bps taker.
    Net PnL = 20 - 0.1 - 0.12 = 19.78; final equity = 10019.78; exactly one trade."""
    bars = _bars([100, 100, 105, 120, 120])
    strat = _ScriptStrategy("script", {"buy_at": 1, "sell_at": 3, "entry_qty": 1, "sell_qty": 1})
    runner = BacktestRunner()
    metrics = asyncio.run(runner.run(
        strategy=strat, candles=bars, exchange=Exchange.BINANCE, symbol="BTC-USDT",
        initial_capital=10_000.0, taker_fee_bps=10.0, maker_fee_bps=10.0,
    ))
    assert len(metrics.trades) == 1
    assert abs(metrics.trades[0].pnl - 19.78) < 1e-6
    final_equity = metrics.equity_curve[-1][1]
    assert abs(final_equity - 10019.78) < 1e-6


def test_reduce_only_never_flips_into_a_short():
    """An oversized reduce_only sell (5) on a long of 1 closes only 1 and opens no
    short — so there is exactly one trade and no end-of-test liquidation trade."""
    bars = _bars([100, 100, 105, 120, 120])
    strat = _ScriptStrategy("script", {"buy_at": 1, "sell_at": 3, "entry_qty": 1, "sell_qty": 5})
    runner = BacktestRunner()
    metrics = asyncio.run(runner.run(
        strategy=strat, candles=bars, exchange=Exchange.BINANCE, symbol="BTC-USDT",
        initial_capital=10_000.0, taker_fee_bps=10.0, maker_fee_bps=10.0,
    ))
    assert len(metrics.trades) == 1
    assert metrics.trades[0].side == "long"


def test_risk_gate_blocks_oversized_order_in_backtest():
    """The real RiskManager runs in the backtest: a marketable limit buy fills
    under permissive risk but is rejected when max_order_usdt is below its
    notional — proving the gate is exercised, not stubbed."""
    bars = _bars([100, 100, 105, 120, 120])

    def run_with(risk):
        strat = _ScriptStrategy("script", {
            "buy_at": 1, "sell_at": 3, "entry_type": "limit", "entry_price": 200,
            "entry_qty": 1, "sell_qty": 1,
        })
        return asyncio.run(BacktestRunner().run(
            strategy=strat, candles=bars, exchange=Exchange.BINANCE, symbol="BTC-USDT",
            initial_capital=10_000.0, taker_fee_bps=10.0, maker_fee_bps=10.0,
            risk_config=risk,
        ))

    permissive = run_with(None)
    assert len(permissive.trades) == 1

    strict = RiskConfig(
        max_position_usdt=Decimal("1e15"), max_order_usdt=Decimal("10"),
        max_daily_loss_usdt=Decimal("1e15"), max_open_orders=10**9, enabled=True,
    )
    blocked = run_with(strict)
    assert len(blocked.trades) == 0


def test_parity_path_through_backtest_engine():
    """BacktestEngine(parity=True) drives the parity runner and returns the same
    BacktestMetrics.to_dict() shape as the legacy path — the opt-in API wiring."""
    from backtest.engine import BacktestEngine, BacktestJob

    bars = _bars([100, 100, 105, 120, 120])

    class _FakeDB:
        async def get_ohlcv(self, exchange, symbol, interval, start_ts, end_ts, limit=100000):
            return bars

        async def save_backtest_job(self, **kw):
            return None

    bt = BacktestEngine(storage=_FakeDB(), parity=True)
    job = BacktestJob(
        job_id="t", strategy_id="script", exchange="binance", symbol="BTC-USDT",
        interval="1h", start_ts=0, end_ts=10**12, initial_capital=10_000.0,
        params={"buy_at": 1, "sell_at": 3, "entry_qty": 1, "sell_qty": 1},
        taker_fee_bps=10.0, maker_fee_bps=10.0,
    )
    metrics = asyncio.run(bt._simulate_parity(job, _ScriptStrategy))
    d = metrics.to_dict()
    assert d["total_trades"] == 1
    assert abs(d["trades"][0]["pnl"] - 19.78) < 1e-4


def test_grid_layered_inventory_produces_many_round_trips():
    """A grid over an oscillating price fills many resting levels independently and
    realizes multiple round trips — the layered inventory the legacy single-net-
    position engine could not model."""
    # Zig-zag between 92 and 108 a few times so many grid levels cross repeatedly.
    path = [100]
    for tgt in (92, 108, 92, 108, 100):
        step = 2 if tgt > path[-1] else -2
        while path[-1] != tgt:
            path.append(path[-1] + step)
    bars = _bars(path, exchange="binance_spot", symbol="BTC-USDT")

    strat = SpotGridStrategy("grid", {
        "exchange": "binance_spot", "symbol": "BTC-USDT",
        "grid_low": 90.0, "grid_high": 110.0, "grid_levels": 10,
        "order_usdt": 100.0, "price_precision": 2, "qty_precision": 6,
    })
    metrics = asyncio.run(BacktestRunner().run(
        strategy=strat, candles=bars, exchange=Exchange.BINANCE_SPOT, symbol="BTC-USDT",
        initial_capital=10_000.0, taker_fee_bps=4.0, maker_fee_bps=2.0,
    ))
    # Many independent level fills → several realized round trips.
    assert len(metrics.trades) >= 4
    # Equity recorded once per bar.
    assert len(metrics.equity_curve) == len(bars)
