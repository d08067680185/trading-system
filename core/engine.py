from __future__ import annotations
import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from pathlib import Path
from core.types import (
    Exchange, Order, OrderSide, OrderStatus, OrderType, Signal,
    ConnectorReadyEvent, ConnectorErrorEvent,
    OrderUpdateEvent, PositionUpdateEvent, BalanceUpdateEvent, TickerEvent,
    OrderBookEvent,
)
from connectors.base import BaseConnector, gen_client_order_id
from risk.manager import RiskManager
from strategies.base import BaseStrategy
from config.manager import AppConfig


class TradingEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.connectors: dict[Exchange, BaseConnector] = {}
        self.strategies: list[BaseStrategy] = []
        self.risk_manager = RiskManager(config.risk)
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._running = False
        self._active = True   # False = paused (connectors disconnected, loop idles)
        self.is_backtest = False  # True under BacktestRunner — strategies disable wall-clock timers
        # Backtest clock: when set, engine.now() returns this sim time instead of
        # wall-clock, so strategy cooldowns/timeouts advance with the replayed bars.
        self._sim_time: Optional[float] = None
        self._start_time: float = 0.0
        self._connector_states: dict[str, str] = {}  # exchange → "connected"|"disconnected"|"error"
        # Exchanges the operator disconnected on purpose — the health monitor's
        # auto-heal must NOT reconnect these (it would fight a deliberate action).
        self._manually_disconnected: set[Exchange] = set()
        self._event_listeners: list = []
        self._notifier = None  # TelegramAlerter, injected externally
        self._prev_halted: bool = False
        self._loss_warn_sent: bool = False
        # (exchange_value, symbol) → (bid, ask, last, recv_ts) — for staleness guard + fee FX
        self._last_ticker: dict[tuple[str, str], tuple] = {}
        # order_id → {placed_ts, exchange, symbol, strategy_id, last_state} — for the
        # stale-order reaper and the REST fill poller
        self._open_orders: dict[str, dict] = {}
        self._reaper_task: Optional[asyncio.Task] = None
        self._order_poll_task: Optional[asyncio.Task] = None
        # FILLED events already fanned out to strategy.record_fill — the same fill
        # can arrive via both private WS and the REST poller
        self._processed_fill_ids: set[str] = set()
        self.logger = logging.getLogger("TradingEngine")
        # Optional pluggable modules (set externally after construction)
        self.position_sizer  = None     # risk.position_sizer.PositionSizer
        self.portfolio_risk  = None     # risk.portfolio.PortfolioRisk
        self.regime_detector = None     # signals.regime.RegimeDetector
        self.microstructure  = None     # signals.microstructure.MicrostructureSignals
        self.attributor      = None     # risk.attribution.PnLAttributor
        self.tca             = None     # risk.tca.TransactionCostAnalyzer

    def now(self) -> float:
        """Current time — wall-clock live, or the replayed bar time under backtest.
        Strategies should use this (via `BaseStrategy._now()`) for cooldowns and
        timeouts so they behave correctly in faster-than-real-time replay."""
        return self._sim_time if self._sim_time is not None else time.time()

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def add_event_listener(self, fn) -> None:
        self._event_listeners.append(fn)

    # ── Registration ──────────────────────────────────────────────────────────

    def add_connector(self, exchange: Exchange, connector: BaseConnector) -> None:
        connector.set_event_queue(self.event_queue)
        self.connectors[exchange] = connector
        self.logger.info(f"Connector registered: {exchange.value}")

    def add_strategy(self, strategy: BaseStrategy) -> None:
        strategy.set_engine(self)
        self.strategies.append(strategy)
        self.logger.info(f"Strategy registered: {strategy.strategy_id}")

    def remove_strategy(self, strategy_id: str) -> bool:
        for i, s in enumerate(self.strategies):
            if s.strategy_id == strategy_id:
                s.disable()
                self.strategies.pop(i)
                self.logger.info(f"Strategy removed: {strategy_id}")
                return True
        return False

    def reload_custom_strategies(
        self, found: dict[str, tuple[type, str]]
    ) -> dict[str, str]:
        """
        Replace all custom strategies with freshly-loaded ones.
        Returns {strategy_id: "added" | "replaced"}.
        """
        # Remove existing custom strategies
        custom_ids = list(getattr(self, "_custom_strategy_ids", set()))
        for sid in custom_ids:
            self.remove_strategy(sid)
        self._custom_strategy_ids: set[str] = set()

        result: dict[str, str] = {}
        for sid, (cls, path) in found.items():
            action = "replaced" if sid in custom_ids else "added"
            instance = cls(strategy_id=sid, params={})
            instance._is_custom = True
            instance._source_file = Path(path).name
            self.add_strategy(instance)
            self._custom_strategy_ids.add(sid)
            result[sid] = action
        return result

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._active = True
        self._start_time = time.time()
        self.logger.info("Starting TradingEngine...")

        results = await asyncio.gather(
            *[c.connect() for c in self.connectors.values()],
            return_exceptions=True,
        )
        for ex, result in zip(self.connectors.keys(), results):
            if isinstance(result, Exception):
                self.logger.error(f"Connector {ex.value} failed to connect: {result}")
                self._connector_states[ex.value] = "error"
            else:
                self._connector_states[ex.value] = "connected"

        for connector in self.connectors.values():
            for symbol in self.config.engine.symbols:
                await connector.subscribe_ticker(symbol)
                await connector.subscribe_orderbook(
                    symbol, depth=self.config.engine.orderbook_depth
                )

        self.logger.info(
            f"Engine running: {len(self.connectors)} exchanges, "
            f"{len(self.strategies)} strategies, "
            f"symbols={self.config.engine.symbols}"
        )
        await self._reconcile_positions()
        await self._reconcile_orders()
        if self.config.engine.order_ttl_s > 0:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            self.logger.info(f"Stale-order reaper active (ttl={self.config.engine.order_ttl_s}s)")
        if self.config.engine.order_poll_interval_s > 0:
            self._order_poll_task = asyncio.create_task(self._order_poll_loop())
            self.logger.info(
                f"Order fill poller active (interval={self.config.engine.order_poll_interval_s}s)"
            )
        await self._event_loop()

    async def stop(self) -> None:
        self._running = False
        self._active = False
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        if self._order_poll_task:
            self._order_poll_task.cancel()
            self._order_poll_task = None
        if self.config.engine.cancel_orders_on_shutdown:
            try:
                await self.cancel_all_orders()
            except Exception as e:
                self.logger.error(f"cancel_all_orders on shutdown failed: {e}")
        await asyncio.gather(*[c.disconnect() for c in self.connectors.values()])
        for ex in self.connectors:
            self._connector_states[ex.value] = "disconnected"
        self.logger.info("TradingEngine stopped")

    async def pause(self) -> None:
        """Disconnect all connectors but keep the event loop alive."""
        if not self._active:
            return
        self._active = False
        await asyncio.gather(*[c.disconnect() for c in self.connectors.values()])
        for ex in self.connectors:
            self._connector_states[ex.value] = "disconnected"
        self.logger.info("TradingEngine paused")

    async def resume(self) -> None:
        """Reconnect all connectors and resume processing."""
        if self._active:
            return
        self._active = True
        results = await asyncio.gather(
            *[c.connect() for c in self.connectors.values()],
            return_exceptions=True,
        )
        for ex, result in zip(self.connectors.keys(), results):
            if isinstance(result, Exception):
                self.logger.error(f"Connector {ex.value} failed to reconnect: {result}")
                self._connector_states[ex.value] = "error"
            else:
                self._connector_states[ex.value] = "connected"

        for connector in self.connectors.values():
            for symbol in self.config.engine.symbols:
                await connector.subscribe_ticker(symbol)
                await connector.subscribe_orderbook(
                    symbol, depth=self.config.engine.orderbook_depth
                )
        self.logger.info("TradingEngine resumed")

    async def connect_exchange(self, exchange: Exchange) -> None:
        """Connect (or reconnect) a single exchange."""
        connector = self.connectors.get(exchange)
        if not connector:
            raise ValueError(f"No connector registered for {exchange.value}")
        await connector.connect()
        for symbol in self.config.engine.symbols:
            await connector.subscribe_ticker(symbol)
            await connector.subscribe_orderbook(symbol, depth=self.config.engine.orderbook_depth)
        self._connector_states[exchange.value] = "connected"
        self._manually_disconnected.discard(exchange)
        self.logger.info(f"Exchange reconnected: {exchange.value}")

    async def disconnect_exchange(self, exchange: Exchange) -> None:
        """Disconnect a single exchange."""
        connector = self.connectors.get(exchange)
        if not connector:
            raise ValueError(f"No connector registered for {exchange.value}")
        await connector.disconnect()
        self._connector_states[exchange.value] = "disconnected"
        self._manually_disconnected.add(exchange)
        self.logger.info(f"Exchange disconnected: {exchange.value}")

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time if self._start_time else 0.0

    @property
    def is_active(self) -> bool:
        return self._running and self._active

    def get_system_status(self) -> dict:
        return {
            "running": self._running,
            "active": self._active,
            "uptime_seconds": round(self.uptime_seconds),
            "connector_states": dict(self._connector_states),
            "strategy_count": len(self.strategies),
            "symbol_count": len(self.config.engine.symbols),
            "symbols": self.config.engine.symbols,
        }

    # ── Startup reconciliation ────────────────────────────────────────────────

    async def _reconcile_positions(self) -> None:
        """Fetch open positions from all connectors and seed risk manager state."""
        for ex, conn in self.connectors.items():
            try:
                positions = await conn.get_positions()
                for pos in positions:
                    self.risk_manager.record_position_notional(
                        pos.exchange.value, pos.symbol, pos.notional
                    )
                if positions:
                    self.logger.info(
                        f"Reconciled {len(positions)} positions for {ex.value}"
                    )
            except Exception as e:
                self.logger.warning(f"Position reconciliation failed [{ex.value}]: {e}")

    async def _reconcile_orders(self) -> None:
        """Fetch open orders from all connectors and seed risk manager state."""
        for ex, conn in self.connectors.items():
            try:
                orders = await conn.get_open_orders()
                for order in orders:
                    self.risk_manager.record_order_placed(order)
                if orders:
                    self.logger.info(
                        f"Reconciled {len(orders)} open orders for {ex.value}"
                    )
            except Exception as e:
                self.logger.warning(f"Order reconciliation failed [{ex.value}]: {e}")

    # ── Quote freshness / fee normalization helpers ───────────────────────────

    _STABLES = {"USDT", "USDC", "BUSD", "USD", ""}

    def _quote_age(self, exchange_value: str, symbol: str) -> Optional[float]:
        """Seconds since the last ticker for (exchange, symbol); None if never seen."""
        entry = self._last_ticker.get((exchange_value, symbol))
        return None if entry is None else time.time() - entry[3]

    def _ref_price(self, symbol: str) -> Optional[float]:
        """Latest 'last' price for a symbol from any exchange's cached ticker."""
        for (_ex, sym), v in self._last_ticker.items():
            if sym == symbol:
                return float(v[2])
        return None

    def _normalize_fee(self, order: Order) -> None:
        """Convert a non-USDT fee (e.g. BNB) to USDT in place using cached quotes."""
        ccy = (order.fee_ccy or "").upper()
        if order.fee <= 0 or ccy in self._STABLES:
            return
        price = self._ref_price(f"{ccy}-USDT")
        if price:
            order.fee = order.fee * Decimal(str(price))
            order.fee_ccy = "USDT"

    def _strategy_rests_orders(self, strategy_id: str) -> bool:
        """Grid / market-maker strategies legitimately keep orders resting — the
        reaper must not cancel those. Strategies opt in via `keeps_resting_orders`."""
        for s in self.strategies:
            if s.strategy_id == strategy_id:
                return getattr(s, "keeps_resting_orders", False)
        return False

    async def cancel_all_orders(self) -> int:
        """Cancel every open order across all connectors. Used on graceful shutdown."""
        total = 0
        for ex, conn in self.connectors.items():
            try:
                n = await conn.cancel_all_orders()
                total += n
                if n:
                    self.logger.info(f"Cancelled {n} open orders on {ex.value}")
            except Exception as e:
                self.logger.error(f"cancel_all_orders [{ex.value}] failed: {e}")
        self._open_orders.clear()
        return total

    async def _reaper_loop(self) -> None:
        """Periodically cancel resting orders older than engine.order_ttl_s."""
        ttl = self.config.engine.order_ttl_s
        interval = min(30.0, max(5.0, ttl / 4))
        while self._running:
            await asyncio.sleep(interval)
            if ttl <= 0 or not self._active:
                continue
            now = time.time()
            for oid, info in list(self._open_orders.items()):
                if now - info["placed_ts"] <= ttl:
                    continue
                if self._strategy_rests_orders(info.get("strategy_id", "")):
                    continue
                conn = self.connectors.get(info["exchange"])
                if not conn:
                    self._open_orders.pop(oid, None)
                    continue
                try:
                    if await conn.cancel_order(info["symbol"], oid):
                        self.logger.info(
                            f"Reaped stale order {oid} ({info['symbol']}, age>{ttl:.0f}s)"
                        )
                        self._open_orders.pop(oid, None)
                except Exception as e:
                    self.logger.warning(f"Reaper cancel {oid} failed: {e}")

    async def _order_poll_loop(self) -> None:
        """REST fallback for fill confirmations while private WS streams are down
        (OKX 60011 / Binance listenKey 410). Polls non-terminal orders and injects
        an OrderUpdateEvent whenever status or filled_qty changes; downstream
        consumers dedupe by order_id, so overlap with WS pushes is safe."""
        interval = self.config.engine.order_poll_interval_s
        while self._running:
            await asyncio.sleep(interval)
            if not self._active or not self._open_orders:
                continue
            now = time.time()
            # Snapshot, capped per cycle to stay friendly to REST rate limits
            for oid, info in list(self._open_orders.items())[:40]:
                if now - info["placed_ts"] < 1.0:
                    continue  # fresh order — give the WS push a chance first
                conn = self.connectors.get(info["exchange"])
                if conn is None:
                    continue
                try:
                    order = await conn.get_order(info["symbol"], oid)
                except Exception as e:
                    self.logger.debug(f"Order poll {oid} failed: {e}")
                    continue
                if order is None or not order.order_id:
                    continue
                state = (order.status.value, str(order.filled_qty))
                if state == info.get("last_state"):
                    continue
                if oid not in self._open_orders:
                    continue  # WS terminal update won the race while we polled
                info["last_state"] = state
                order.strategy_id = order.strategy_id or info.get("strategy_id", "")
                try:
                    self.event_queue.put_nowait(OrderUpdateEvent(order))
                except asyncio.QueueFull:
                    self.logger.warning("Event queue full — dropping polled order update")

    # ── Event loop ────────────────────────────────────────────────────────────

    async def _event_loop(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if not self._active:
                continue  # discard stale events while paused

            await self._process_event(event)

    async def drain_events(self) -> None:
        """Process every event currently on the queue, then return.

        Used by the backtest replay driver to pump the engine one bar at a time
        without the real-time `await get()` wait. The live path uses
        `_event_loop`; both share `_process_event`, so a strategy runs the
        identical code path in simulation and in production."""
        while True:
            try:
                event = self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not self._active:
                continue
            await self._process_event(event)

    async def _process_event(self, event) -> None:
        """Handle a single event: update risk/quant state, broadcast to external
        listeners, and fan out to strategies. Shared verbatim by the live event
        loop and the backtest replay driver."""
        # Risk state updates (no blocking check)
        if isinstance(event, OrderUpdateEvent):
            # Normalize fee to USDT (fees may be charged in BNB/other) before it
            # flows to PnL attribution / portfolio / TCA, all of which assume USDT.
            self._normalize_fee(event.order)
            # Drop terminal orders from the reaper registry
            if event.order.is_done and event.order.order_id:
                self._open_orders.pop(event.order.order_id, None)
            self.risk_manager.record_order_update(event.order)
            # Attribute fill to originating strategy for per-strategy PnL.
            # Dedupe by order_id: the same FILLED event can arrive via both
            # private WS and the REST fill poller; record_fill/TCA/attribution
            # are not idempotent.
            if (event.order.status == OrderStatus.FILLED
                    and event.order.order_id
                    and event.order.order_id in self._processed_fill_ids):
                pass
            elif (event.order.status == OrderStatus.FILLED
                    and event.order.strategy_id):
                if event.order.order_id:
                    self._processed_fill_ids.add(event.order.order_id)
                    if len(self._processed_fill_ids) > 20_000:
                        self._processed_fill_ids.clear()
                for strategy in self.strategies:
                    if strategy.strategy_id == event.order.strategy_id:
                        was_halted = strategy._halted
                        strategy.record_fill(event.order)
                        if strategy._halted and not was_halted and self._notifier:
                            asyncio.create_task(
                                self._notifier.alert_strategy_error(
                                    strategy.strategy_id,
                                    f"Circuit breaker: {strategy._halt_reason}",
                                )
                            )
                        break
                # Portfolio risk: record fill PnL
                if self.portfolio_risk and event.order.status == OrderStatus.FILLED:
                    fill_p = float(event.order.avg_price or event.order.price or 0)
                    fill_q = float(event.order.filled_qty)
                    # Approximate PnL delta from fee only (open legs tracked in attributor)
                    self.portfolio_risk.record_fill_pnl(
                        event.order.strategy_id or "",
                        -float(event.order.fee),
                    )
                # PnL attribution
                if self.attributor and event.order.status == OrderStatus.FILLED:
                    asyncio.create_task(self.attributor.record_fill(event.order))
                # TCA
                if self.tca and event.order.status == OrderStatus.FILLED:
                    self.tca.record_fill(event.order)
                if self._notifier:
                    asyncio.create_task(self._notifier.alert_fill(
                        event.order.strategy_id,
                        event.order.symbol,
                        event.order.side.value,
                        float(event.order.filled_qty),
                        float(event.order.avg_price or event.order.price or 0),
                    ))
        elif isinstance(event, PositionUpdateEvent):
            notional = event.position.notional
            self.risk_manager.record_position_notional(
                event.position.exchange.value, event.position.symbol, notional
            )
        elif isinstance(event, OrderBookEvent):
            # Feed microstructure signals with full order book depth
            if self.microstructure:
                ob = event.orderbook
                self.microstructure.update(
                    exchange=ob.exchange.value,
                    symbol=ob.symbol,
                    bids=ob.bids,
                    asks=ob.asks,
                )
        elif isinstance(event, TickerEvent):
            # Feed position sizer, portfolio risk ref prices, regime, attributor
            t = event.ticker
            sym  = t.symbol
            # cache latest quote per (exchange, symbol) for the staleness guard + fee FX
            self._last_ticker[(t.exchange.value, sym)] = (t.bid, t.ask, t.last, time.time())
            price = float(t.last)
            if self.position_sizer:
                self.position_sizer.update_price(sym, price)
            if self.portfolio_risk:
                self.portfolio_risk.update_ref_price(sym, price)
            if self.regime_detector:
                self.regime_detector.update(sym, price)
            if self.attributor:
                self.attributor.update_mid(t.exchange.value, sym, float(t.bid), float(t.ask))
            if self.tca:
                self.tca.update_mid(t.exchange.value, sym, float(t.bid), float(t.ask))
        elif isinstance(event, ConnectorReadyEvent):
            self.logger.info(f"Connector ready: {event.exchange.value}")
            return
        elif isinstance(event, ConnectorErrorEvent):
            self.logger.error(f"Connector error [{event.exchange.value}]: {event.error}")
            return

        # Broadcast to external listeners (e.g. WebSocket)
        for listener in self._event_listeners:
            try:
                await listener(event)
            except Exception as e:
                self.logger.error(f"Event listener error: {e}", exc_info=True)

        # Fan out to strategies
        for strategy in self.strategies:
            try:
                signals = await strategy.on_event(event)
                for signal in signals:
                    await self._execute_signal(signal)
            except Exception as e:
                self.logger.error(f"Strategy {strategy.strategy_id} error: {e}", exc_info=True)
                if self._notifier:
                    asyncio.create_task(
                        self._notifier.alert_strategy_error(strategy.strategy_id, str(e))
                    )

        # Telegram notifications: halt state change + 80% loss warning
        if self._notifier:
            now_halted = self.risk_manager.is_halted
            if now_halted and not self._prev_halted:
                asyncio.create_task(
                    self._notifier.alert_halt(getattr(self.risk_manager, "_halt_reason", ""))
                )
                self._loss_warn_sent = True  # suppress redundant loss warn after halt
            elif not now_halted and self._prev_halted:
                asyncio.create_task(self._notifier.alert_resume())
                self._loss_warn_sent = False
            self._prev_halted = now_halted

            if not now_halted and not self._loss_warn_sent:
                pnl = float(self.risk_manager.state.daily_pnl)
                limit = float(self.risk_manager.config.max_daily_loss_usdt)
                warn_pct = getattr(self.config, "telegram", None)
                warn_threshold = (warn_pct.loss_warn_pct / 100.0) if warn_pct else 0.8
                if pnl < 0 and limit > 0 and abs(pnl) / limit >= warn_threshold:
                    self._loss_warn_sent = True
                    asyncio.create_task(self._notifier.alert_loss_warning(pnl, limit))

    # ── Order execution ───────────────────────────────────────────────────────

    async def _execute_signal(self, signal: Signal) -> Optional[Order]:
        if not self.risk_manager.check_signal(signal):
            self.logger.warning(f"Signal blocked by risk: {signal}")
            return None

        connector = self.connectors.get(signal.exchange)
        if not connector:
            self.logger.error(f"No connector for exchange: {signal.exchange}")
            return None

        # Staleness guard: never open/increase on a frozen quote (e.g. WS stalled).
        # Risk-reducing orders (reduce_only) are allowed through so we can always exit.
        if not signal.reduce_only:
            age = self._quote_age(signal.exchange.value, signal.symbol)
            max_age = self.config.engine.max_quote_age_s
            if max_age > 0 and (age is None or age > max_age):
                self.logger.warning(
                    f"Stale quote for {signal.exchange.value}:{signal.symbol} "
                    f"(age={age if age is None else round(age,1)}s > {max_age}s) — blocking entry"
                )
                return None

        _RETRY_ERRORS = ("timeout", "connection", "network", "reset", "eof", "service unavailable", "503", "502", "429")
        # One idempotency key per signal, reused across every retry below so a
        # request that times out after the exchange accepted it can't double-fill.
        cid = gen_client_order_id()
        last_exc = None
        for attempt in range(3):
            try:
                order = await connector.place_order(
                    symbol=signal.symbol,
                    side=signal.side,
                    order_type=signal.order_type,
                    quantity=signal.quantity,
                    price=signal.price,
                    reduce_only=signal.reduce_only,
                    post_only=getattr(signal, "post_only", False),
                    client_order_id=cid,
                )
                order.strategy_id = signal.strategy_id
                self.risk_manager.record_order_placed(order)
                # Track non-terminal orders so the reaper can cancel stale ones
                if order.order_id and not order.is_done:
                    self._open_orders[order.order_id] = {
                        "placed_ts": time.time(),
                        "exchange": signal.exchange,
                        "symbol": signal.symbol,
                        "strategy_id": signal.strategy_id,
                        "last_state": (order.status.value, str(order.filled_qty)),
                    }
                if attempt > 0:
                    self.logger.info(f"Order placed on retry {attempt}")
                self.logger.info(
                    f"Order placed [{signal.exchange.value}] {signal.symbol} "
                    f"{signal.side.value} {signal.quantity} @ "
                    f"{signal.price or 'MARKET'} | id={order.order_id}"
                )
                return order
            except Exception as e:
                last_exc = e
                err_lower = str(e).lower()
                is_retryable = any(kw in err_lower for kw in _RETRY_ERRORS)
                if not is_retryable or attempt == 2:
                    break
                wait = 0.5 * (2 ** attempt)
                self.logger.warning(f"Order placement transient error (attempt {attempt+1}/3), retrying in {wait}s: {e}")
                await asyncio.sleep(wait)
        self.logger.error(f"Order placement failed: {last_exc}", exc_info=True)
        return None

    # ── Public helpers (called by strategies) ─────────────────────────────────

    async def place_order(
        self,
        exchange: Exchange,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        strategy_id: str = "",
        post_only: bool = False,
    ) -> Optional[Order]:
        signal = Signal(
            exchange=exchange,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            reduce_only=reduce_only,
            strategy_id=strategy_id,
            post_only=post_only,
        )
        return await self._execute_signal(signal)

    async def cancel_order(self, exchange: Exchange, symbol: str, order_id: str) -> bool:
        connector = self.connectors.get(exchange)
        if not connector:
            return False
        return await connector.cancel_order(symbol, order_id)

    async def ensure_symbol_feed(self, exchange: Exchange, symbol: str) -> bool:
        """Subscribe the ticker feed for a symbol at runtime.

        Lets strategies trade dynamically discovered symbols (e.g. funding scan_all
        alts): without a live feed the staleness guard in _execute_signal rightly
        blocks every entry. Idempotent at the connector level."""
        connector = self.connectors.get(exchange)
        if not connector:
            return False
        try:
            await connector.subscribe_ticker(symbol)
            return True
        except Exception as e:
            self.logger.warning(f"ensure_symbol_feed [{exchange.value}:{symbol}]: {e}")
            return False

    async def get_positions(self, exchange: Optional[Exchange] = None) -> list:
        if exchange:
            conn = self.connectors.get(exchange)
            if not conn:
                return []
            try:
                return await conn.get_positions()
            except Exception as e:
                self.logger.error(f"get_positions [{exchange.value}]: {e}")
                return []
        results = []
        for ex, conn in self.connectors.items():
            try:
                results.extend(await conn.get_positions())
            except Exception as e:
                self.logger.error(f"get_positions [{ex.value}]: {e}")
        return results

    async def get_balances(self, exchange: Optional[Exchange] = None) -> list:
        if exchange:
            conn = self.connectors.get(exchange)
            if not conn:
                return []
            try:
                return await conn.get_balances()
            except Exception as e:
                self.logger.error(f"get_balances [{exchange.value}]: {e}")
                return []
        results = []
        for ex, conn in self.connectors.items():
            try:
                results.extend(await conn.get_balances())
            except Exception as e:
                self.logger.error(f"get_balances [{ex.value}]: {e}")
        return results

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "running": self._running,
            "exchanges": list(self.connectors.keys()),
            "strategies": [s.strategy_id for s in self.strategies],
            "risk": self.risk_manager.status(),
        }
