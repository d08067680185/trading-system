"""
Basis-aware funding-harvest backtest over the top screener candidates.

Picks the most persistent harvest candidates from the funding_rates screener,
then for each fetches its real Binance perp + spot OHLCV and replays the
delta-neutral harvest, separating funding carry from basis PnL and fees.
Perp-only alts (no spot to hedge against) are reported as skipped.

Usage:  venv/bin/python scripts/harvest_backtest.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.storage import DataStorage
from backtest.funding_harvest import FundingHarvestAnalyzer
from backtest.funding_harvest_sim import HarvestBacktestRunner

DB_PATH = "data/trading_data.db"
DAYS = 60


async def main():
    st = DataStorage(DB_PATH)
    await st.connect()

    top = await FundingHarvestAnalyzer(st).scan(
        exchange="binance", days=DAYS, min_periods=15,
        min_span_days=7, min_favorable_pct=80, top_n=15,
    )
    runner = HarvestBacktestRunner(st)

    print(f"{'symbol':16}{'side':11}{'net%':>7}{'apr%':>8}"
          f"{'funding$':>10}{'basis$':>9}{'fees$':>7}{'fav%':>6}{'sharpe':>7}{'days':>6}")
    print("-" * 92)
    for c in top:
        sym = c["symbol"]
        try:
            d = (await runner.run(sym, days=DAYS, interval="1h",
                                  initial_capital=10_000, fee_bps_per_leg=2.0)).to_dict()
            print(f"{sym:16}{d['side']:11}{d['net_return_pct']:>7.2f}{d['apr_pct']:>8.0f}"
                  f"{d['funding_collected_usdt']:>10.0f}{d['basis_pnl_usdt']:>9.0f}"
                  f"{d['fees_usdt']:>7.0f}{d['favorable_pct']:>6.0f}"
                  f"{d['sharpe_ratio']:>7.1f}{d['span_days']:>6.1f}")
        except Exception as e:
            print(f"{sym:16}SKIP  {str(e)[:60]}")

    await st.close()


if __name__ == "__main__":
    asyncio.run(main())
