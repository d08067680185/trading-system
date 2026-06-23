"""
Live data collector — subscribes as engine event listener, persists to storage.
Also takes periodic equity snapshots.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.types import (
    TickerEvent, OrderUpdateEvent, OrderStatus,
    PositionUpdateEvent, BalanceUpdateEvent,
)
from data.storage import DataStorage

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("LiveCollector")


class LiveCollector:
    def __init__(self, storage: DataStorage, snapshot_interval: int = 60,
                 funding_rate_interval: int = 3600):
        self._db = storage
        self._snapshot_interval = snapshot_interval
        self._funding_interval = funding_rate_interval
        self._engine = None
        self._snapshot_task = None
        self._funding_task = None
        self._tick_count = 0
        self._tick_sample = 10   # store every Nth tick (avoid DB bloat)

    def attach(self, engine: "TradingEngine") -> None:
        self._engine = engine
        engine.add_event_listener(self._on_event)
        logger.info("LiveCollector attached to engine")

    async def start(self) -> None:
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        self._funding_task  = asyncio.create_task(self._funding_rate_loop())

    async def stop(self) -> None:
        for task in (self._snapshot_task, self._funding_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _on_event(self, event: object) -> None:
        try:
            if isinstance(event, TickerEvent):
                self._tick_count += 1
                if self._tick_count % self._tick_sample == 0:
                    t = event.ticker
                    await self._db.store_tick(
                        exchange=t.exchange.value,
                        symbol=t.symbol,
                        ts=t.timestamp,
                        bid=float(t.bid),
                        ask=float(t.ask),
                        last=float(t.last),
                        volume_24h=float(t.volume_24h),
                    )

            elif isinstance(event, OrderUpdateEvent):
                o = event.order
                if o.status == OrderStatus.FILLED:
                    fill_price = o.avg_price or o.price or 0
                    await self._db.store_trade(
                        strategy_id=o.strategy_id or "",
                        exchange=o.exchange.value,
                        symbol=o.symbol,
                        side=o.side.value,
                        order_type=o.order_type.value,
                        quantity=float(o.filled_qty),
                        price=float(fill_price),
                        fee=float(o.fee),
                        order_id=o.order_id or "",
                        status="filled",
                    )

        except Exception as e:
            logger.warning(f"Collector event error: {e}")

    async def _funding_rate_loop(self) -> None:
        """Hourly: collect funding rates from all futures/swap connectors."""
        await asyncio.sleep(60)  # initial delay
        while True:
            try:
                if self._engine:
                    from core.types import Exchange as Ex
                    import time
                    ts = time.time()
                    for ex in (Ex.BINANCE, Ex.OKX):
                        conn = self._engine.connectors.get(ex)
                        if not conn:
                            continue
                        try:
                            import inspect
                            sig = inspect.signature(conn.get_funding_rates)
                            if "symbols" in sig.parameters:
                                rates = await conn.get_funding_rates(
                                    symbols=self._engine.config.engine.symbols
                                )
                            else:
                                rates = await conn.get_funding_rates()
                            for r in rates:
                                ann = float(r.get("funding_rate", 0)) * 3 * 365 * 100
                                await self._db.store_funding_rate(
                                    exchange=ex.value,
                                    symbol=r.get("symbol", ""),
                                    ts=ts,
                                    rate=float(r.get("funding_rate", 0)),
                                    next_funding_time=r.get("next_funding_time"),
                                    annualized_pct=round(ann, 2),
                                )
                        except Exception as e:
                            logger.debug(f"Funding rate fetch [{ex.value}]: {e}")
            except Exception as e:
                logger.warning(f"Funding rate collection error: {e}")
            await asyncio.sleep(self._funding_interval)

    # Counted 1:1 toward equity. Earn-parked stables (LDUSDT/LDUSDC) are
    # deliberately excluded — they aren't tradeable until redeemed, and a large
    # constant offset would mask drawdowns in the risk manager's percentages.
    _STABLE_ASSETS = {"USDT", "USDC"}

    async def _snapshot_loop(self) -> None:
        import datetime
        from core.types import Exchange as Ex
        while True:
            await asyncio.sleep(self._snapshot_interval)
            try:
                if not self._engine:
                    continue
                # Partial connectivity produces nonsense equity points (one
                # exchange's balance present, another's missing) — the historical
                # 65→196→0→10 jumps all came from snapshots during connect/disconnect.
                states = self._engine._connector_states
                if any(states.get(ex.value) != "connected" for ex in self._engine.connectors):
                    continue
                balances = await self._engine.get_balances()
                # OKX unified account: the okx and okx_spot connectors report the
                # SAME account — drop the spot copy or USDT is counted twice
                if Ex.OKX in self._engine.connectors and Ex.OKX_SPOT in self._engine.connectors:
                    balances = [b for b in balances if b.exchange != Ex.OKX_SPOT]
                total = sum(float(b.total) for b in balances if b.asset in self._STABLE_ASSETS)
                try:
                    positions = await self._engine.get_positions()
                    total += sum(float(p.unrealized_pnl) for p in positions)
                except Exception:
                    pass
                pnl = self._engine.risk_manager.state.daily_pnl
                await self._db.store_equity(total_usdt=total, daily_pnl=float(pnl))
                self._engine.risk_manager.update_equity(total)

                # Persist per-strategy PnL to DB
                today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                for strat in self._engine.strategies:
                    try:
                        await self._db.upsert_strategy_pnl(
                            strategy_id=strat.strategy_id,
                            date=today,
                            daily_pnl=getattr(strat, "_realized_pnl", 0.0),
                            trade_count=getattr(strat, "_trade_count", 0),
                        )
                    except Exception as e:
                        logger.debug(f"Strategy PnL persist error [{strat.strategy_id}]: {e}")
            except Exception as e:
                logger.warning(f"Equity snapshot error: {e}")
