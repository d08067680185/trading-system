"""
Event-driven backtesting engine.
Replays OHLCV data through the same BaseStrategy interface used in live trading.
Simulates order fills at next-candle open (conservative; avoids look-ahead).
"""
from __future__ import annotations
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from core.types import (
    Exchange, OrderSide, OrderStatus, OrderType,
    Ticker, TickerEvent, Order, OrderUpdateEvent,
    PositionUpdateEvent, BalanceUpdateEvent, Position, Balance, PositionSide,
)
from data.storage import DataStorage, OHLCVRow
from backtest.metrics import BacktestMetrics, BacktestTrade, calculate
from strategies.base import BaseStrategy

logger = logging.getLogger("BacktestEngine")

TAKER_FEE = Decimal("0.0004")  # 0.04% Binance USDT-M futures taker
MAKER_FEE = Decimal("0.0002")  # 0.02% Binance USDT-M futures maker
FUNDING_INTERVAL_S = 8 * 3600   # funding settlements every 8h for perpetuals


@dataclass
class SimPosition:
    symbol: str
    side: str      # "long" | "short"
    size: Decimal
    entry_price: Decimal
    entry_time: float
    entry_fee: Decimal = Decimal("0")


@dataclass
class BacktestJob:
    job_id: str
    strategy_id: str
    exchange: str
    symbol: str
    interval: str
    start_ts: int
    end_ts: int
    initial_capital: float
    params: dict
    status: str = "pending"           # pending | running | done | error
    progress: float = 0.0
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    slippage_bps: int = 0             # taker slippage in basis points (0 = none)
    funding_rate_pct: float = 0.0     # funding rate per 8h period (e.g. 0.01 = 0.01%); 0 = disabled
    # Per-leg fees in bps; None = use the module TAKER_FEE/MAKER_FEE defaults.
    # A limit order that rests and fills at its price is charged maker; one that
    # fills at candle open (was marketable) and every market order is charged taker.
    taker_fee_bps: Optional[float] = None
    maker_fee_bps: Optional[float] = None


class BacktestEngine:
    def __init__(self, storage: DataStorage, parity: bool = False):
        self._db = storage
        self._jobs: dict[str, BacktestJob] = {}
        # When True, run jobs through the parity path (real TradingEngine +
        # RiskManager + SimulatedConnector) instead of this module's legacy
        # single-net-position simulator. Same result dict, so the API/frontend
        # contract is unchanged; legacy stays the default until proven out live.
        self._parity = parity

    def create_job(
        self,
        strategy_class,
        strategy_id: str,
        params: dict,
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        initial_capital: float = 10_000.0,
        slippage_bps: int = 0,
        funding_rate_pct: float = 0.0,
        taker_fee_bps: Optional[float] = None,
        maker_fee_bps: Optional[float] = None,
    ) -> BacktestJob:
        job = BacktestJob(
            job_id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
            exchange=exchange,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            initial_capital=initial_capital,
            params=params,
            slippage_bps=slippage_bps,
            funding_rate_pct=funding_rate_pct,
            taker_fee_bps=taker_fee_bps,
            maker_fee_bps=maker_fee_bps,
        )
        self._jobs[job.job_id] = job
        asyncio.create_task(self._run_job(job, strategy_class))
        return job

    def get_job(self, job_id: str) -> Optional[BacktestJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        return [
            {"job_id": j.job_id, "strategy_id": j.strategy_id,
             "symbol": j.symbol, "status": j.status,
             "progress": j.progress, "created_at": j.created_at}
            for j in sorted(self._jobs.values(), key=lambda x: -x.created_at)
        ]

    # ── Core simulation ───────────────────────────────────────────────────────

    async def _run_job(self, job: BacktestJob, strategy_class) -> None:
        job.status = "running"
        # Persist job start
        if self._db:
            try:
                asyncio.create_task(self._db.save_backtest_job(
                    job_id=job.job_id, strategy_id=job.strategy_id,
                    status="running", params=job.params
                ))
            except Exception:
                pass
        try:
            if self._parity:
                metrics = await self._simulate_parity(job, strategy_class)
            else:
                metrics = await self._simulate(job, strategy_class)
            job.result = metrics.to_dict()
            job.status = "done"
            job.progress = 1.0
            # Persist result
            if self._db:
                try:
                    asyncio.create_task(self._db.save_backtest_job(
                        job_id=job.job_id, strategy_id=job.strategy_id,
                        status="done", result=job.result
                    ))
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Backtest {job.job_id} failed: {e}", exc_info=True)
            job.error = str(e)
            job.status = "error"
            if self._db:
                try:
                    asyncio.create_task(self._db.save_backtest_job(
                        job_id=job.job_id, strategy_id=job.strategy_id,
                        status="error", error=str(e)
                    ))
                except Exception:
                    pass

    async def _simulate_parity(self, job: BacktestJob, strategy_class) -> BacktestMetrics:
        """Parity path: drive a real TradingEngine + RiskManager over a
        SimulatedConnector with the real strategy. Same OHLCV source and same
        BacktestMetrics output as `_simulate`, so callers can't tell which ran."""
        from backtest.runner import BacktestRunner

        candles = await self._db.get_ohlcv(
            exchange=job.exchange, symbol=job.symbol, interval=job.interval,
            start_ts=job.start_ts, end_ts=job.end_ts, limit=100_000,
        )
        strategy = strategy_class(job.strategy_id, dict(job.params))

        def _progress(p: float) -> None:
            job.progress = p

        return await BacktestRunner().run(
            strategy=strategy, candles=candles,
            exchange=Exchange(job.exchange), symbol=job.symbol,
            initial_capital=job.initial_capital,
            slippage_bps=job.slippage_bps, funding_rate_pct=job.funding_rate_pct,
            taker_fee_bps=job.taker_fee_bps, maker_fee_bps=job.maker_fee_bps,
            progress_cb=_progress,
        )

    async def _simulate(self, job: BacktestJob, strategy_class) -> BacktestMetrics:
        candles = await self._db.get_ohlcv(
            exchange=job.exchange,
            symbol=job.symbol,
            interval=job.interval,
            start_ts=job.start_ts,
            end_ts=job.end_ts,
            limit=100_000,
        )
        if len(candles) < 2:
            raise ValueError(
                f"Insufficient data: {len(candles)} candles for {job.symbol} "
                f"on {job.exchange} ({job.interval}). "
                f"Run /api/data/fetch first."
            )

        bad = [i for i, c in enumerate(candles)
               if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0 or c.high < c.low]
        if bad:
            raise ValueError(f"Invalid OHLCV data at candle indices {bad[:5]}: prices must be > 0 and high >= low")

        # Data gap check — warn but don't block (gap > 10% raises warning in result)
        from backtest.metrics import check_data_gaps
        data_quality = check_data_gaps(candles, job.interval, max_gap_pct=0.10)
        if not data_quality["ok"]:
            logger.warning(f"Backtest {job.job_id}: {data_quality['warning']}")

        strategy: BaseStrategy = strategy_class(job.strategy_id, dict(job.params))
        # The instance shares the live strategy's logger name ("strategy.<id>"),
        # so without this swap simulated fills flood the main log AND the live
        # DB log handler attached to that logger (49k rows per grid run).
        strategy.logger = logging.getLogger(f"backtest.{job.strategy_id}")
        strategy.logger.setLevel(logging.WARNING)

        capital   = Decimal(str(job.initial_capital))
        positions: dict[str, SimPosition] = {}
        equity_curve: list[tuple[float, float]] = []
        trades: list[BacktestTrade] = []
        pending_orders: list[tuple[Order, bool]] = []   # (order, reduce_only)

        slip_factor = Decimal(str(job.slippage_bps)) / Decimal("10000")
        funding_rate = Decimal(str(job.funding_rate_pct)) / Decimal("100")
        taker_fee = (Decimal(str(job.taker_fee_bps)) / Decimal("10000")
                     if job.taker_fee_bps is not None else TAKER_FEE)
        maker_fee = (Decimal(str(job.maker_fee_bps)) / Decimal("10000")
                     if job.maker_fee_bps is not None else MAKER_FEE)
        _last_funding_ts: Optional[int] = None  # track last funding settlement candle ts

        exchange_enum = Exchange(job.exchange)

        # Proxy updated each iteration so strategy can place orders and read equity
        _cur: list = [None]  # [current candle] — mutable container for closure

        class _RiskProxy:
            def check_signal(self, *a): return True
            def record_order_placed(self, *a): return None

        _risk = _RiskProxy()

        class _Proxy:
            async def place_order(_self, exchange, symbol, side, order_type, quantity, price=None, reduce_only=False, strategy_id=""):
                o = Order(
                    exchange=exchange_enum, symbol=symbol,
                    side=side, order_type=order_type, quantity=quantity, price=price,
                    order_id=uuid.uuid4().hex[:12], status=OrderStatus.OPEN,
                )
                # Fix 8: fills happen at the NEXT candle's open (conservative, no
                # look-ahead). reduce_only is carried so a closing order doesn't
                # flip into a fresh opposite position.
                pending_orders.append((o, reduce_only))
                return o
            async def cancel_order(_self, *a): return True
            async def get_positions(_self): return []
            async def get_balances(_self): return [Balance(exchange_enum, "USDT", capital, Decimal("0"))]
            @property
            def risk_manager(_self): return _risk

        strategy.engine = _Proxy()

        for i, candle in enumerate(candles):
            _cur[0] = candle
            job.progress = i / len(candles)

            # ── Funding fee settlement (every 8h for perpetuals) ──────────────
            if funding_rate != 0 and positions and _last_funding_ts is not None:
                if candle.ts - _last_funding_ts >= FUNDING_INTERVAL_S:
                    mark = Decimal(str(candle.open))
                    for pos in positions.values():
                        notional = pos.size * mark
                        # Longs pay funding (positive rate), shorts receive
                        payment = notional * funding_rate
                        if pos.side == "long":
                            capital -= payment
                        else:
                            capital += payment
                    _last_funding_ts = candle.ts
            elif funding_rate != 0 and _last_funding_ts is None:
                _last_funding_ts = candle.ts

            # ── Fill pending orders at candle open ────────────────────────────
            if pending_orders:
                # Snapshot: orders placed by strategy callbacks during this loop
                # (e.g. grid re-quotes on fill) must wait for the NEXT candle,
                # otherwise the loop consumes its own appends and never ends.
                batch = pending_orders[:]
                pending_orders.clear()
                still_pending: list[tuple[Order, bool]] = []
                for order, reduce_only in batch:
                    open_p = Decimal(str(candle.open))
                    is_maker = False
                    if order.order_type == OrderType.LIMIT and order.price is not None:
                        # Limit orders fill only if this candle crosses the price.
                        # Filling at open = order was marketable when placed (taker);
                        # filling at the limit intra-candle = it rested first (maker).
                        limit = Decimal(str(order.price))
                        if order.side == OrderSide.BUY:
                            if open_p <= limit:
                                raw_price = open_p
                            elif Decimal(str(candle.low)) <= limit:
                                raw_price = limit
                                is_maker = True
                            else:
                                still_pending.append((order, reduce_only))
                                continue
                        else:
                            if open_p >= limit:
                                raw_price = open_p
                            elif Decimal(str(candle.high)) >= limit:
                                raw_price = limit
                                is_maker = True
                            else:
                                still_pending.append((order, reduce_only))
                                continue
                    else:
                        raw_price = open_p
                    # Slippage applies only to taker fills (a resting maker order
                    # executes at its own price, no spread crossing)
                    if is_maker:
                        fill_price = raw_price
                    elif order.side == OrderSide.BUY:
                        fill_price = raw_price * (1 + slip_factor)
                    else:
                        fill_price = raw_price * (1 - slip_factor)
                    qty = order.quantity
                    fee = qty * fill_price * (maker_fee if is_maker else taker_fee)
                    filled = Order(
                        exchange=exchange_enum,
                        symbol=job.symbol,
                        side=order.side,
                        order_type=order.order_type,
                        quantity=qty,
                        order_id=order.order_id,
                        status=OrderStatus.FILLED,
                        filled_qty=qty,
                        avg_price=fill_price,
                        fee=fee,
                    )
                    # Fix 1: subtract fee once here; _close_position no longer
                    # subtracts exit fee from pnl to avoid double-counting
                    capital -= fee

                    ex_key = exchange_enum.value

                    if order.side == OrderSide.BUY:
                        pos = positions.get(ex_key)
                        if pos and pos.side == "short":
                            gross_pnl, trade = self._close_position(pos, fill_price, candle.ts, fee)
                            capital += gross_pnl
                            trades.append(trade)
                            del positions[ex_key]
                            pos = None
                        # reduce_only orders only close — they never open a new leg.
                        if ex_key not in positions and not reduce_only:
                            positions[ex_key] = SimPosition(
                                symbol=job.symbol, side="long",
                                size=qty, entry_price=fill_price,
                                entry_time=float(candle.ts), entry_fee=fee,
                            )
                    else:  # SELL
                        pos = positions.get(ex_key)
                        if pos and pos.side == "long":
                            gross_pnl, trade = self._close_position(pos, fill_price, candle.ts, fee)
                            capital += gross_pnl
                            trades.append(trade)
                            del positions[ex_key]
                            pos = None
                        if ex_key not in positions and not reduce_only:
                            positions[ex_key] = SimPosition(
                                symbol=job.symbol, side="short",
                                size=qty, entry_price=fill_price,
                                entry_time=float(candle.ts), entry_fee=fee,
                            )

                    await strategy.on_event(OrderUpdateEvent(filled))

                # Keep uncrossed limit orders resting; orders appended by fill
                # callbacks above also stay queued for the next candle.
                pending_orders.extend(still_pending)

            # ── Feed ticker event to strategy ─────────────────────────────────
            # Fix 8: use candle.open — the strategy decides based on prices known at
            # bar open, not close, so close-based ticker was look-ahead bias
            ticker = Ticker(
                exchange=exchange_enum,
                symbol=job.symbol,
                bid=Decimal(str(candle.open)) * Decimal("0.9999"),
                ask=Decimal(str(candle.open)) * Decimal("1.0001"),
                last=Decimal(str(candle.open)),
                volume_24h=Decimal(str(candle.volume)),
                timestamp=float(candle.ts),
            )

            try:
                signals = await strategy.on_event(TickerEvent(ticker))
            except Exception as exc:
                raise RuntimeError(
                    f"Strategy error at candle {i} (ts={candle.ts}, price={candle.open}): {exc}"
                ) from exc
            # (Signals are handled via place_order on the proxy → pending_orders)

            # ── Equity snapshot (mark-to-market uses close — known after bar ends) ─
            mark = Decimal(str(candle.close))
            unrealized = Decimal("0")
            for pos in positions.values():
                if pos.side == "long":
                    unrealized += (mark - pos.entry_price) * pos.size
                else:
                    unrealized += (pos.entry_price - mark) * pos.size

            equity = float(capital + unrealized)
            equity_curve.append((float(candle.ts), equity))

            # Yield control periodically
            if i % 500 == 0:
                await asyncio.sleep(0)

        # Close any open positions at last close
        if positions:
            last_close = Decimal(str(candles[-1].close))
            for pos in list(positions.values()):
                # Forced end-of-test liquidation is a market (taker) close
                exit_fee = pos.size * last_close * taker_fee
                capital -= exit_fee
                gross_pnl, trade = self._close_position(pos, last_close, candles[-1].ts, exit_fee)
                capital += gross_pnl
                trades.append(trade)
            positions.clear()
            if equity_curve:
                equity_curve[-1] = (equity_curve[-1][0], float(capital))

        # Determine candle interval in seconds for annualization
        if len(candles) > 1:
            avg_interval_s = (candles[-1].ts - candles[0].ts) / (len(candles) - 1)
            ppy = int(365 * 86400 / avg_interval_s) if avg_interval_s > 0 else 365 * 24
        else:
            ppy = 365 * 24

        metrics = calculate(equity_curve, trades, job.initial_capital, periods_per_year=ppy)
        metrics.data_quality = data_quality
        return metrics

    def _close_position(
        self, pos: SimPosition, close_price: Decimal, ts: int, exit_fee: Decimal
    ) -> tuple[Decimal, BacktestTrade]:
        # Fix 1: return gross pnl only — caller already subtracted exit_fee from capital
        # so we must NOT subtract it here again. Net pnl in the trade record uses both
        # the entry and exit fees for accurate reporting.
        if pos.side == "long":
            gross_pnl = (close_price - pos.entry_price) * pos.size
            pnl_pct = float((close_price - pos.entry_price) / pos.entry_price * 100)
        else:
            gross_pnl = (pos.entry_price - close_price) * pos.size
            pnl_pct = float((pos.entry_price - close_price) / pos.entry_price * 100)
        net_pnl = gross_pnl - pos.entry_fee - exit_fee
        trade = BacktestTrade(
            symbol=pos.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=float(ts),
            entry_price=float(pos.entry_price), exit_price=float(close_price),
            quantity=float(pos.size), pnl=float(net_pnl), pnl_pct=pnl_pct,
            fee=float(pos.entry_fee + exit_fee),
        )
        return gross_pnl, trade
