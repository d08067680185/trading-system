"""
Parity backtest runner.

Unlike the legacy `backtest/engine.py` (which re-implements order handling with a
stubbed risk manager and a single net position), this runner drives a *real*
`TradingEngine` with the *real* strategy and the *real* `RiskManager` over one or
more `SimulatedConnector`s. The strategy therefore runs the identical
`_process_event → on_ticker/on_order_update → place_order → _execute_signal`
code path it runs live.

`run()` backtests a single-exchange strategy (grid, directional). `run_multi()`
replays several aligned OHLCV streams into one `SimulatedConnector` per exchange,
so cross-exchange strategies (spread_arb) can be backtested — the engine clock is
advanced to each bar's timestamp (`engine._sim_time`) so the strategy's
cooldowns/leg-timeouts pace with sim time, not wall-clock.

Per-timestamp loop (no look-ahead):
  1. sim.settle_bar(bar)   — fill orders resting from earlier bars; emit fills
  2. sim.emit_ticker(bar)  — push the bar's ticker (bar-open prices)
  3. engine.drain_events() — engine processes fills then ticker; strategy reacts
  4. drain create_task'd work (e.g. grid's async setup)
  5. sim.promote_pending() — orders placed this bar become resting for the next
  6. record equity (mark-to-market at bar close, summed across exchanges)
At the end every open position is liquidated at the last close (taker).

Fidelity note: OHLCV carries no real order book, so bid/ask is synthesized as a
fixed half-spread around the bar open (`half_spread_bps`) and depth-based checks
(microstructure) are off unless an instance is injected. A spread-arb backtest is
therefore only as realistic as that synthetic book — treat it as a logic/cadence
test and a coarse opportunity scan, not a precise PnL forecast.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from core.types import Exchange, MarketType
from core.engine import TradingEngine
from connectors.sim import SimulatedConnector
from config.manager import AppConfig, EngineConfig
from risk.manager import RiskConfig
from backtest.metrics import BacktestMetrics, BacktestTrade, calculate, check_data_gaps
from strategies.base import BaseStrategy

logger = logging.getLogger("BacktestRunner")

_MARKET_TYPE = {
    Exchange.BINANCE: MarketType.FUTURES,
    Exchange.BINANCE_SPOT: MarketType.SPOT,
    Exchange.OKX: MarketType.SWAP,
    Exchange.OKX_SPOT: MarketType.SPOT,
}


def permissive_risk() -> RiskConfig:
    """A RiskConfig that gates nothing — backtests measure raw strategy edge by
    default. Pass your live RiskConfig to gate instead (the point of running real
    risk here)."""
    big = Decimal("1e15")
    return RiskConfig(
        max_position_usdt=big, max_order_usdt=big, max_daily_loss_usdt=big,
        max_open_orders=10**9, enabled=True,
        max_drawdown_pct=Decimal("0"), max_symbol_concentration_pct=Decimal("0"),
        max_rolling_7d_loss_usdt=Decimal("0"), max_rolling_30d_loss_usdt=Decimal("0"),
    )


def _validate(candles: list) -> dict:
    if len(candles) < 2:
        raise ValueError(f"Insufficient data: {len(candles)} candles")
    bad = [i for i, c in enumerate(candles)
           if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0 or c.high < c.low]
    if bad:
        raise ValueError(f"Invalid OHLCV at indices {bad[:5]}: prices > 0 and high >= low required")
    interval = getattr(candles[0], "interval", "1h")
    dq = check_data_gaps(candles, interval, max_gap_pct=0.10)
    if not dq["ok"]:
        logger.warning(f"Backtest data gap: {dq['warning']}")
    return dq


def _make_sim(exchange, initial, taker, maker, slip, funding, half_spread) -> SimulatedConnector:
    return SimulatedConnector(
        exchange=exchange, market_type=_MARKET_TYPE.get(exchange, MarketType.FUTURES),
        initial_capital=initial, taker_fee=taker, maker_fee=maker,
        slippage_bps=slip, funding_rate=funding, half_spread_bps=half_spread,
    )


class BacktestRunner:
    async def run(
        self, *, strategy: BaseStrategy, candles: list, exchange: Exchange, symbol: str,
        initial_capital: float = 10_000.0, slippage_bps: float = 0.0,
        funding_rate_pct: float = 0.0, taker_fee_bps: Optional[float] = None,
        maker_fee_bps: Optional[float] = None, half_spread_bps: float = 1.0,
        risk_config: Optional[RiskConfig] = None, progress_cb=None,
    ) -> BacktestMetrics:
        """Single-exchange backtest (delegates to run_multi with one stream)."""
        return await self.run_multi(
            strategy=strategy, streams={exchange: candles}, symbol=symbol,
            initial_capital=initial_capital, slippage_bps=slippage_bps,
            funding_rate_pct=funding_rate_pct, taker_fee_bps=taker_fee_bps,
            maker_fee_bps=maker_fee_bps, half_spread_bps=half_spread_bps,
            risk_config=risk_config, progress_cb=progress_cb,
        )

    async def run_multi(
        self, *, strategy: BaseStrategy, streams: dict, symbol: str,
        initial_capital: float = 10_000.0, slippage_bps: float = 0.0,
        funding_rate_pct: float = 0.0, taker_fee_bps: Optional[float] = None,
        maker_fee_bps: Optional[float] = None, half_spread_bps: float = 1.0,
        risk_config: Optional[RiskConfig] = None, microstructure=None, progress_cb=None,
    ) -> BacktestMetrics:
        """Multi-exchange backtest. `streams` maps each Exchange to its candle list
        (same symbol). Capital is split evenly across exchanges; the equity curve
        sums every connector's mark-to-market so cross-exchange legs net out."""
        if not streams:
            raise ValueError("run_multi needs at least one stream")
        dqs = {ex: _validate(c) for ex, c in streams.items()}

        taker = (Decimal(str(taker_fee_bps)) / Decimal("10000")
                 if taker_fee_bps is not None else Decimal("0.0004"))
        maker = (Decimal(str(maker_fee_bps)) / Decimal("10000")
                 if maker_fee_bps is not None else Decimal("0.0002"))
        per_cap = Decimal(str(initial_capital)) / Decimal(len(streams))

        sims: dict[Exchange, SimulatedConnector] = {
            ex: _make_sim(ex, per_cap, taker, maker, Decimal(str(slippage_bps)),
                          Decimal(str(funding_rate_pct)) / Decimal("100"),
                          Decimal(str(half_spread_bps)))
            for ex in streams
        }

        config = AppConfig(
            exchanges={}, risk=risk_config or permissive_risk(),
            engine=EngineConfig(
                symbols=[symbol], max_quote_age_s=0.0, order_ttl_s=0.0,
                order_poll_interval_s=0.0, cancel_orders_on_shutdown=False,
                auto_heal_feeds=False,
            ),
        )
        engine = TradingEngine(config)
        engine.is_backtest = True
        engine.microstructure = microstructure   # None → depth checks are skipped
        for ex, sim in sims.items():
            engine.add_connector(ex, sim)
        engine.add_strategy(strategy)
        strategy.logger = logging.getLogger(f"backtest.{strategy.strategy_id}")
        strategy.logger.setLevel(logging.WARNING)

        for sim in sims.values():
            await sim.connect()
        engine._running = True
        engine._active = True

        # Per-exchange ts → bar maps, and the merged, sorted timeline.
        by_ts = {ex: {c.ts: c for c in c_list} for ex, c_list in streams.items()}
        timeline = sorted({ts for m in by_ts.values() for ts in m})
        order = list(streams.keys())   # deterministic exchange iteration order

        equity_curve: list[tuple[float, float]] = []
        ntl = len(timeline)
        try:
            for i, ts in enumerate(timeline):
                engine._sim_time = float(ts)
                present = [ex for ex in order if ts in by_ts[ex]]
                for ex in present:
                    await sims[ex].settle_bar(by_ts[ex][ts])
                for ex in present:
                    await sims[ex].emit_ticker(by_ts[ex][ts])
                await self._drain_fully(engine)
                for ex in present:
                    sims[ex].promote_pending()

                total = Decimal("0")
                for ex in order:
                    bar = by_ts[ex].get(ts)
                    if bar is not None:
                        total += sims[ex].equity({symbol: Decimal(str(bar.close))})
                    else:
                        total += sims[ex].equity()
                equity_curve.append((float(ts), float(total)))

                if progress_cb and i % 200 == 0:
                    progress_cb(i / ntl)
                if i % 500 == 0:
                    await asyncio.sleep(0)

            # End-of-test liquidation at each exchange's last close.
            last_ts = timeline[-1]
            engine._sim_time = float(last_ts)
            total = Decimal("0")
            for ex in order:
                last_bar = streams[ex][-1]
                mark = {symbol: Decimal(str(last_bar.close))}
                sims[ex].liquidate_all(mark, float(last_bar.ts))
                total += sims[ex].equity(mark)
            if equity_curve:
                equity_curve[-1] = (equity_curve[-1][0], float(total))
        finally:
            engine._running = False
            engine._active = False

        # Annualization from the merged timeline spacing.
        if ntl > 1:
            avg_s = (timeline[-1] - timeline[0]) / (ntl - 1)
            ppy = int(365 * 86400 / avg_s) if avg_s > 0 else 365 * 24
        else:
            ppy = 365 * 24

        trades = []
        for sim in sims.values():
            trades.extend(BacktestTrade(
                symbol=t.symbol, side=t.side, entry_time=t.entry_time, exit_time=t.exit_time,
                entry_price=t.entry_price, exit_price=t.exit_price, quantity=t.quantity,
                pnl=t.pnl, pnl_pct=t.pnl_pct, fee=t.fee,
            ) for t in sim.trades)
        trades.sort(key=lambda t: t.exit_time)

        metrics = calculate(equity_curve, trades, initial_capital, periods_per_year=ppy)
        # Attach the worst data-quality result across streams (most conservative).
        metrics.data_quality = min(dqs.values(), key=lambda d: 1 if d["ok"] else 0)
        return metrics

    async def _drain_fully(self, engine: TradingEngine) -> None:
        """Process all queued events, then let any create_task'd coroutines (e.g.
        grid's async initial setup, which only yields with asyncio.sleep(0)) run
        to completion. Bounded so a misbehaving strategy can't spin forever."""
        for _ in range(10_000):
            await engine.drain_events()
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()]
            if not pending and engine.event_queue.empty():
                return
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            else:
                await asyncio.sleep(0)
        logger.warning("drain_fully hit iteration cap — strategy may be enqueuing unboundedly")
