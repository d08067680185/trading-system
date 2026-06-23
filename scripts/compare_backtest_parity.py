"""
Shadow-compare the legacy backtest engine vs the parity runner on real data.

Runs the SAME BacktestJob through `BacktestEngine._simulate` (legacy single-net-
position simulator) and `_simulate_parity` (real TradingEngine + RiskManager +
SimulatedConnector) and diffs the headline metrics. Expectation:
  - buy-and-hold (single position, no layering)  → results match closely (parity)
  - grid (many concurrent resting levels)         → parity reports more trades,
    because the legacy engine collapses layered inventory into one net position.

Usage:  venv/bin/python scripts/compare_backtest_parity.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decimal import Decimal

from core.types import Exchange, OrderSide, OrderType
from data.storage import DataStorage
from backtest.engine import BacktestEngine, BacktestJob
from strategies.base import BaseStrategy
from strategies.grid import SpotGridStrategy

DB_PATH = "data/trading_data.db"
EXCHANGE = "binance"
SYMBOL = "BTC-USDT"
INTERVAL = "1h"
LAST_N = 1500          # most recent N candles
INITIAL = 10_000.0


class BuyHoldOnce(BaseStrategy):
    """Buy a fixed quantity once on the first ticker, then hold. A single net
    position with no layering — both engines must agree on its PnL."""

    def __init__(self, strategy_id, params):
        super().__init__(strategy_id, {"exchange": EXCHANGE, "symbol": SYMBOL,
                                       "qty": 0.05, **params})
        self._done = False

    async def on_ticker(self, event):
        if self._done:
            return []
        self._done = True
        await self.engine.place_order(
            exchange=Exchange(self.params["exchange"]), symbol=self.params["symbol"],
            side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal(str(self.params["qty"])), strategy_id=self.strategy_id,
        )
        return []

    def get_status(self):
        return {}


def _fmt(d: dict) -> str:
    return (f"ret={d['total_return_pct']:+.3f}%  trades={d['total_trades']:<4} "
            f"sharpe={d['sharpe_ratio']:+.2f}  maxDD={d['max_drawdown_pct']:.2f}%  "
            f"fees={d['total_fees']:.2f}")


async def _run_both(storage, strategy_class, params, start_ts, end_ts, label):
    legacy = BacktestEngine(storage, parity=False)
    parity = BacktestEngine(storage, parity=True)

    def mk_job():
        return BacktestJob(
            job_id="cmp", strategy_id=label, exchange=EXCHANGE, symbol=SYMBOL,
            interval=INTERVAL, start_ts=start_ts, end_ts=end_ts,
            initial_capital=INITIAL, params=dict(params),
            taker_fee_bps=4.0, maker_fee_bps=2.0,
        )

    m_legacy = await legacy._simulate(mk_job(), strategy_class)
    m_parity = await parity._simulate_parity(mk_job(), strategy_class)
    print(f"\n=== {label} ===")
    print(f"  legacy:  {_fmt(m_legacy.to_dict())}")
    print(f"  parity:  {_fmt(m_parity.to_dict())}")
    return m_legacy.to_dict(), m_parity.to_dict()


async def main():
    storage = DataStorage(DB_PATH)
    await storage.connect()

    candles = await storage.get_ohlcv(
        exchange=EXCHANGE, symbol=SYMBOL, interval=INTERVAL,
        start_ts=0, end_ts=10**12, limit=100_000,
    )
    candles = candles[-LAST_N:]
    start_ts, end_ts = candles[0].ts, candles[-1].ts
    lows = [c.low for c in candles]
    highs = [c.high for c in candles]
    lo, hi = min(lows), max(highs)
    mid = (lo + hi) / 2
    print(f"Data: {EXCHANGE} {SYMBOL} {INTERVAL}  {len(candles)} candles  "
          f"price [{lo:.0f}..{hi:.0f}]")

    # 1) Buy-and-hold — parity check (should match)
    await _run_both(storage, BuyHoldOnce, {}, start_ts, end_ts, "buy_hold_once")

    # 2) Grid — divergence demo (parity should report more trades).
    #    Grid bounds bracket the middle of the observed range so levels actually cross.
    grid_low = mid - (mid - lo) * 0.5
    grid_high = mid + (hi - mid) * 0.5
    grid_params = {
        "exchange": EXCHANGE, "symbol": SYMBOL,
        "grid_low": float(grid_low), "grid_high": float(grid_high),
        "grid_levels": 20, "order_usdt": 200.0,
        "price_precision": 1, "qty_precision": 6,
    }
    print(f"\nGrid bounds: [{grid_low:.0f}..{grid_high:.0f}] x20 levels")
    await _run_both(storage, SpotGridStrategy, grid_params, start_ts, end_ts, "grid")

    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
