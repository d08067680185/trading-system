"""Entry point — starts engine + API server concurrently."""
from __future__ import annotations
import asyncio
import logging
import os
import signal

import uvicorn

from config.manager import load_config
from core.engine import TradingEngine
from core.types import Exchange
from connectors.binance import BinanceConnector
from connectors.okx import OKXConnector
from api.main import app, set_engine, set_data_services, set_dex_services, set_api_key
from notifications.telegram import TelegramAlerter
from strategies.cash_carry import CashCarryStrategy
from strategies.grid import SpotGridStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.trading_comp import TradingCompStrategy
from strategies.futures_trend import FuturesTrendStrategy
from strategies.futures_grid import FuturesGridStrategy
from strategies.futures_signal import FuturesSignalStrategy


def validate_config(config) -> list[str]:
    """Return list of warning messages for potential config issues."""
    warnings = []
    if not config.engine.symbols:
        warnings.append("No symbols configured — market data will not stream")
    for ex, cfg in config.exchanges.items():
        if not cfg.testnet and not cfg.api_key:
            warnings.append(f"Exchange {ex.value}: API key not set — live trading will fail")
        if not cfg.testnet and not cfg.secret:
            warnings.append(f"Exchange {ex.value}: Secret not set — live trading will fail")
    if float(config.risk.max_daily_loss_usdt) <= 0:
        warnings.append("max_daily_loss_usdt is 0 — daily loss protection is DISABLED")
    if float(config.risk.max_position_usdt) <= 0:
        warnings.append("max_position_usdt is 0 — position size protection is DISABLED")
    return warnings


def setup_logging(level: str) -> None:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(
        log_dir / "trading.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv optional; use env vars directly if not installed

    config = load_config("config.yaml")
    setup_logging(config.engine.log_level)
    logger = logging.getLogger("main")

    # Validate config and warn about potential issues
    cfg_warnings = validate_config(config)
    for w in cfg_warnings:
        logger.warning(f"[Config] {w}")

    engine = TradingEngine(config)

    # ── CEX connectors ────────────────────────────────────────────────────────
    if Exchange.BINANCE in config.exchanges:
        cfg = config.exchanges[Exchange.BINANCE]
        engine.add_connector(
            Exchange.BINANCE,
            BinanceConnector(
                api_key=cfg.api_key, secret=cfg.secret,
                market_type=cfg.market_type, testnet=cfg.testnet,
                portfolio_margin=os.environ.get("BINANCE_PORTFOLIO_MARGIN", "").lower() == "true",
            ),
        )

    if Exchange.BINANCE_SPOT in config.exchanges:
        cfg = config.exchanges[Exchange.BINANCE_SPOT]
        engine.add_connector(
            Exchange.BINANCE_SPOT,
            BinanceConnector(
                api_key=cfg.api_key, secret=cfg.secret,
                market_type=cfg.market_type, testnet=cfg.testnet,
            ),
        )

    if Exchange.OKX in config.exchanges:
        cfg = config.exchanges[Exchange.OKX]
        engine.add_connector(
            Exchange.OKX,
            OKXConnector(
                api_key=cfg.api_key, secret=cfg.secret,
                passphrase=cfg.passphrase,
                market_type=cfg.market_type, testnet=cfg.testnet,
            ),
        )

    if Exchange.OKX_SPOT in config.exchanges:
        cfg = config.exchanges[Exchange.OKX_SPOT]
        engine.add_connector(
            Exchange.OKX_SPOT,
            OKXConnector(
                api_key=cfg.api_key, secret=cfg.secret,
                passphrase=cfg.passphrase,
                market_type=cfg.market_type, testnet=cfg.testnet,
            ),
        )

    # ── Register strategies ───────────────────────────────────────────────────
    from strategies.spread_arb import SpreadArbStrategy
    from strategies.funding_rate import FundingRateArbStrategy
    from api.main import register_strategy

    # ── Capital sizing (OKX ~27U available) ──────────────────────────────────
    # SpreadArb: spot-to-spot (binance_spot vs okx_spot) so $10 order clears
    # min contract sizes; threshold lowered to 3bps to catch real opportunities
    spread_arb = SpreadArbStrategy("arb_spread", {
        "min_profit_bps": 3.0,
        "order_size_usdt": 10.0,
        "cooldown_s": 30.0,
        "max_position_usdt": 20.0,
    })
    # FundingRateArb: 25U per side (long + short = 50U total)
    funding_arb = FundingRateArbStrategy("funding_arb", {
        "symbols": config.engine.symbols,
        "min_rate_diff": 50.0,
        "position_usdt": 25.0,
        "check_interval_s": 300,
        "exit_rate_diff": 10.0,
        "max_hold_hours": 24.0,
        # Scan all Binance USDT perps for high-funding alts — BTC/ETH diffs
        # (~0.3bps/8h) can never clear the 4-leg fee gate
        "scan_all": True,
        "scan_top_n": 8,
        "min_volume_24h_usdt": 50_000_000.0,
    })
    # CashCarry: 25U spot + 25U futures = 50U total deployment
    cash_carry = CashCarryStrategy("cash_carry", {
        "symbols": config.engine.symbols,
        "spot_exchange": "binance_spot",
        "futures_exchange": "binance",
        # Soft pre-filter; the fee-viability gate (amortized over the hold) is the
        # real economic constraint. BTC/ETH at calm funding won't clear it — by
        # design, not a bug. See cash_carry.py fee-gate comment.
        "min_rate_8h": 0.0001,
        "exit_rate_8h": 0.0001,
        "position_usdt": 25.0,
        "check_interval_s": 300,
        "max_hold_hours": 72.0,
        "min_hold_hours": 8.0,
    })
    # SpotGrid: 10 levels × 10U = 100U max (set grid_low/grid_high via API to activate)
    spot_grid_btc = SpotGridStrategy("spot_grid_btc", {
        "exchange": "binance_spot",
        "symbol": "BTC-USDT",
        "grid_low": 0.0,
        "grid_high": 0.0,
        "grid_levels": 10,
        "order_usdt": 10.0,
        "qty_precision": 5,
        "price_precision": 0,
    })
    # MarketMaker: 50U bid + 50U ask, max 200U inventory (disabled by default)
    market_maker = MarketMakerStrategy("market_maker", {
        "exchange": "binance_spot",
        "symbol": "BTC-USDT",
        "spread_bps": 10.0,
        "order_usdt": 50.0,
        "max_inventory_usdt": 200.0,
        "inventory_skew_bps": 10.0,
        "requote_interval_s": 5.0,
        "vol_window": 30,
        "vol_spread_mult": 2.0,
        "min_spread_bps": 5.0,
        "max_spread_bps": 50.0,
        "qty_precision": 5,
        "price_precision": 0,
    })
    market_maker.disable()  # start disabled; user enables via UI

    # TradingComp: 50U per cycle, 60s interval (disabled by default; user sets symbol+amount via UI)
    trading_comp = TradingCompStrategy("trading_comp", {
        "exchange": "binance_spot",
        "symbol": "BTC-USDT",
        "order_usdt": 50.0,
        "cycle_interval_s": 60.0,
        "max_cycles": 0,
        "qty_precision": 5,
    })
    trading_comp.disable()  # start disabled
    funding_arb.disable()   # disabled: scan_all bought unhedged alt positions (2026-07-01)
    cash_carry.disable()    # disabled: Binance futures has no USDT margin

    engine.add_strategy(spread_arb)
    engine.add_strategy(funding_arb)
    engine.add_strategy(cash_carry)
    engine.add_strategy(spot_grid_btc)
    engine.add_strategy(market_maker)
    engine.add_strategy(trading_comp)

    # ── Futures auto-trading strategies ──────────────────────────────────────
    futures_trend = FuturesTrendStrategy("futures_trend", {
        "exchange": "binance", "symbol": "BTC-USDT",
        "fast_period": 10, "slow_period": 30,
        "position_usdt": 10.0, "stop_loss_pct": 2.0, "take_profit_pct": 4.0,
        "direction": "both", "cooldown_s": 60.0,
    })
    futures_trend.disable()

    futures_grid = FuturesGridStrategy("futures_grid", {
        "exchange": "binance", "symbol": "BTC-USDT",
        "grid_low": 0.0, "grid_high": 0.0, "grid_count": 10,
        "grid_usdt": 10.0, "mode": "neutral",
    })
    futures_grid.disable()

    futures_signal = FuturesSignalStrategy("futures_signal", {
        "exchange": "okx", "symbol": "BTC-USDT",
        "position_usdt": 10.0, "signal_type": "rsi",
        "rsi_period": 14, "rsi_oversold": 30.0, "rsi_overbought": 70.0,
        "stop_loss_pct": 2.0, "take_profit_pct": 6.0, "direction": "both",
        "cooldown_s": 120.0,
    })
    # starts disabled; user enables via UI when ready

    engine.add_strategy(futures_trend)
    engine.add_strategy(futures_grid)
    engine.add_strategy(futures_signal)

    # Register with API so backtest can resolve strategies by ID
    register_strategy("arb_spread", SpreadArbStrategy)
    register_strategy("funding_arb", FundingRateArbStrategy)
    register_strategy("cash_carry", CashCarryStrategy)
    register_strategy("spot_grid_btc", SpotGridStrategy)
    register_strategy("market_maker", MarketMakerStrategy)
    register_strategy("trading_comp", TradingCompStrategy)
    register_strategy("futures_trend", FuturesTrendStrategy)
    register_strategy("futures_grid", FuturesGridStrategy)
    register_strategy("futures_signal", FuturesSignalStrategy)

    set_engine(engine)

    # ── Notifications + API auth ──────────────────────────────────────────────
    notifier = TelegramAlerter(
        token=config.telegram.token,
        chat_id=config.telegram.chat_id,
    )
    engine.set_notifier(notifier)

    if config.api.key:
        set_api_key(config.api.key)
        logger.info("API authentication enabled")
    else:
        logger.warning("API authentication DISABLED — set api.key or TRADING_API_KEY to enable")

    # ── Phase 2: Chain / DEX initialization ──────────────────────────────────
    _chain_connectors: dict = {}
    _chain_wallets: dict = {}
    _gas_managers: dict = {}

    for chain_name, chain_cfg in config.chains.items():
        if not chain_cfg.enabled or not chain_cfg.rpc_url:
            continue
        try:
            from connectors.dex.uniswap_v3 import UniswapV3Connector
            from connectors.onchain.wallet import EVMWallet
            from connectors.onchain.gas import GasManager

            conn = UniswapV3Connector(chain=chain_name, rpc_url=chain_cfg.rpc_url)
            await conn.connect()
            _chain_connectors[chain_name] = conn
            logger.info(f"DEX connector ready: {chain_name}")

            if chain_cfg.private_key:
                wallet = EVMWallet(
                    private_key=chain_cfg.private_key,
                    rpc_url=chain_cfg.rpc_url,
                    chain_id=chain_cfg.chain_id,
                )
                await wallet.connect()
                _chain_wallets[chain_name] = wallet

                gas_mgr = GasManager(
                    conn._w3,
                    priority_multiplier=chain_cfg.gas_priority_multiplier,
                    base_multiplier=chain_cfg.gas_base_multiplier,
                )
                await gas_mgr.start()
                _gas_managers[chain_name] = gas_mgr
                wallet.set_gas_manager(gas_mgr)  # apply live EIP-1559 fees to all sends
                logger.info(f"Wallet + GasManager ready: {chain_name} ({wallet.address})")
        except Exception as e:
            logger.error(f"Chain {chain_name} init failed: {e}", exc_info=True)

    set_dex_services(_chain_connectors, _chain_wallets)

    # ── DEX-CEX arb strategies (one per enabled chain + symbol) ──────────────
    if config.dex.enabled and _chain_connectors:
        from strategies.dex_arb import DexCexArbStrategy
        register_strategy("dex_cex_arb", DexCexArbStrategy)

        # Instantiate strategies for each chain that has a wallet.
        # token_in/token_out must be configured here — set real ERC20 addresses
        # for each chain. Below are Arbitrum One defaults; update as needed.
        _dex_token_map: dict[str, dict[str, str]] = {
            # chain → {symbol → (token_in, token_out)}
            # token_in  = USDC (the quote currency you spend)
            # token_out = the asset token (WETH, WBTC, …)
            "arbitrum": {
                "ETH-USDT": {
                    "token_in":  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC.e
                    "token_out": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
                },
            },
            "ethereum": {
                "ETH-USDT": {
                    "token_in":  "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
                    "token_out": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
                },
            },
        }

        for chain_name, conn in _chain_connectors.items():
            wallet = _chain_wallets.get(chain_name)
            if not wallet:
                logger.warning(f"DEX-CEX arb skipped for {chain_name}: no wallet")
                continue
            token_map = _dex_token_map.get(chain_name, {})
            for sym, tokens in token_map.items():
                if sym not in config.engine.symbols:
                    continue
                strat_id = f"dex_cex_{chain_name}_{sym.replace('-','').lower()}"
                dex_arb = DexCexArbStrategy(strat_id, {
                    "symbol": sym,
                    "chain": chain_name,
                    "token_in":  tokens["token_in"],
                    "token_out": tokens["token_out"],
                    "amount_usdt": 300.0,
                    "min_profit_bps": config.dex.min_profit_bps,
                    "cooldown_s": 60.0,
                    "fee_tier": 500,
                    "gas_usdt_budget": 5.0,
                })
                dex_arb.set_dex(conn, wallet)
                # Fix 9: inject live gas manager so strategy uses real gas prices
                gas_mgr = _gas_managers.get(chain_name)
                if gas_mgr:
                    dex_arb.set_gas_manager(gas_mgr)
                engine.add_strategy(dex_arb)
                logger.info(f"DEX-CEX arb strategy registered: {strat_id}")

    # ── Phase 3: Data + backtest services ─────────────────────────────────────
    from data.storage import DataStorage
    from data.fetcher import HistoricalFetcher
    from data.collector import LiveCollector
    from backtest.engine import BacktestEngine
    from backtest.capacity import CapacityAnalyzer
    from optimizer.engine import ParameterOptimizer

    storage = DataStorage(config.data.db_path)
    await storage.connect()

    # DB-stored Telegram settings override config/env (set via System → Telegram UI)
    tg_token   = await storage.get_setting("telegram_token")   or config.telegram.token
    tg_chat_id = await storage.get_setting("telegram_chat_id") or config.telegram.chat_id
    if tg_token and tg_chat_id:
        engine.set_notifier(TelegramAlerter(token=tg_token, chat_id=tg_chat_id))

    fetcher    = HistoricalFetcher(storage)
    bt_engine  = BacktestEngine(storage, parity=config.backtest.parity)
    optimizer  = ParameterOptimizer(bt_engine)
    capacity_analyzer = CapacityAnalyzer(bt_engine)

    set_data_services(storage, fetcher, bt_engine, optimizer)

    # Trigger-quality logging for spread arb (arb_triggers table)
    spread_arb.storage = storage

    # Attach DB log handlers to all registered strategies
    from data.log_handler import StrategyDBHandler
    _db_log_fmt = logging.Formatter("%(message)s")
    for _strat in engine.strategies:
        _h = StrategyDBHandler(storage, _strat.strategy_id)
        _h.setFormatter(_db_log_fmt)
        _strat.logger.addHandler(_h)
    logger.info("Strategy DB log handlers attached")

    # Live data collection
    collector = LiveCollector(storage, config.data.equity_snapshot_interval)
    collector.attach(engine)
    await collector.start()

    # Restore cumulative PnL from DB (survive restarts)
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    latest_pnl = await storage.get_all_strategy_pnl_latest()
    pnl_map = {r["strategy_id"]: r for r in latest_pnl}
    for strat in engine.strategies:
        rec = pnl_map.get(strat.strategy_id)
        if rec and rec["date"] == today:
            strat._realized_pnl = rec["daily_pnl"]
            strat._trade_count  = rec["trade_count"]
            logger.info(f"Restored PnL [{strat.strategy_id}]: {rec['daily_pnl']:.4f} USDT, {rec['trade_count']} trades")
        elif rec:
            strat._cumulative_pnl = rec["cumulative_pnl"]
    logger.info("Strategy PnL restored from DB")

    logger.info("Data services initialized")

    # ── Phase 4: Quant modules ────────────────────────────────────────────────
    from risk.position_sizer import PositionSizer
    from risk.portfolio import PortfolioRisk
    from risk.attribution import PnLAttributor
    from risk.margin_monitor import MarginMonitor
    from risk.factor_exposure import FactorExposureMonitor
    from risk.tca import TransactionCostAnalyzer
    from risk.stress_test import StressTester
    from monitoring.latency import init_monitor as init_latency
    from monitoring.health import HealthMonitor
    from core.reconciler import PositionReconciler
    from signals.regime import RegimeDetector
    from signals.microstructure import MicrostructureSignals
    from execution.algorithms import ExecutionAlgorithms

    position_sizer   = PositionSizer(target_vol_pct=0.01, lookback=20)
    portfolio_risk   = PortfolioRisk(lookback_days=60)
    attributor       = PnLAttributor(storage)
    tca_analyzer     = TransactionCostAnalyzer(slippage_budget_bps=5.0)
    stress_tester    = StressTester(portfolio_risk)
    latency_monitor  = init_latency(rest_alert_p99_ms=500.0, ws_alert_p99_ms=200.0)
    regime_detector  = RegimeDetector(short_window=20, long_window=200)
    microstructure   = MicrostructureSignals(levels=5)
    exec_algos       = ExecutionAlgorithms(engine)
    reconciler       = PositionReconciler(engine, interval_s=300)
    margin_monitor   = MarginMonitor(engine, warn_safety_pct=15.0, critical_safety_pct=8.0)
    factor_monitor   = FactorExposureMonitor(engine, max_net_exposure_pct=50.0)
    # Feed-staleness thresholds tied to the engine's own order-staleness guard so
    # health flags a stale feed before (and after) it starts silently blocking orders.
    health_monitor   = HealthMonitor(
        engine,
        stale_feed_warn_s=float(config.engine.max_quote_age_s),
        stale_feed_crit_s=float(config.engine.max_quote_age_s) * 3.0,
        # Reconnect a connector whose feed silently freezes (config flag optional,
        # defaults on — reconnect is non-destructive, never touches orders).
        auto_heal=getattr(config.engine, "auto_heal_feeds", True),
    )

    # Attach to engine
    engine.position_sizer  = position_sizer
    engine.portfolio_risk  = portfolio_risk
    engine.regime_detector = regime_detector
    engine.microstructure  = microstructure
    engine.attributor      = attributor
    engine.tca             = tca_analyzer

    # Start background monitors
    await reconciler.start()
    await margin_monitor.start()
    await factor_monitor.start()
    await health_monitor.start()

    # Register quant services with API
    from api.main import set_quant_services
    set_quant_services(
        position_sizer=position_sizer,
        portfolio_risk=portfolio_risk,
        attributor=attributor,
        regime_detector=regime_detector,
        microstructure=microstructure,
        exec_algos=exec_algos,
        reconciler=reconciler,
        margin_monitor=margin_monitor,
        factor_monitor=factor_monitor,
        capacity_analyzer=capacity_analyzer,
        tca=tca_analyzer,
        stress_tester=stress_tester,
        health_monitor=health_monitor,
    )
    # Push health status transitions to connected UIs over the existing WS channel.
    from api.main import ws_manager
    health_monitor.set_broadcast(ws_manager.broadcast)
    logger.info("Quant modules initialized")

    # ── Periodic DB backup (every 6 hours) ────────────────────────────────────
    async def _backup_loop():
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await storage.backup()
            except Exception as e:
                logger.warning(f"DB backup failed: {e}")

    # ── Daily DB purge (old ticks / logs) ─────────────────────────────────────
    async def _purge_loop():
        await asyncio.sleep(3600)   # first run after 1h, then every 24h
        while True:
            try:
                result = await storage.purge_old_data(ticks_days=7, logs_days=30, attribution_days=90)
                logger.info(f"Daily DB purge complete: {result}")
            except Exception as e:
                logger.warning(f"DB purge failed: {e}")
            await asyncio.sleep(24 * 3600)

    async def _daily_report_loop():
        """Send daily Telegram report at 08:00 UTC."""
        import datetime as _dt
        while True:
            now = _dt.datetime.now(_dt.timezone.utc)
            # Next 08:00 UTC
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if target <= now:
                target += _dt.timedelta(days=1)
            wait_s = (target - now).total_seconds()
            await asyncio.sleep(wait_s)
            try:
                if not (notifier and notifier.enabled):
                    continue
                st = engine.risk_manager.state
                positions = await engine.get_positions()
                strat_stats = await storage.get_all_strategy_pnl_latest()
                await notifier.daily_report(
                    date_str=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
                    daily_pnl=float(st.daily_pnl),
                    total_equity=sum(float(b.free + b.locked) for conn in engine.connectors.values()
                                     for b in await conn.get_balances() if b.asset == "USDT"),
                    strategy_stats=strat_stats,
                    total_fees=0.0,
                    open_positions=len(positions),
                )
            except Exception as e:
                logger.warning(f"Daily report failed: {e}")

    asyncio.create_task(_backup_loop())
    asyncio.create_task(_purge_loop())
    asyncio.create_task(_daily_report_loop())

    # Auto-warmup OHLCV for configured symbols (best-effort, non-blocking)
    async def _warmup_ohlcv():
        """Download recent OHLCV on startup so KlineChart has data immediately."""
        await asyncio.sleep(10)  # wait for connectors to settle
        for symbol in config.engine.symbols:
            for exchange_name in ["binance", "okx"]:
                for interval in ["1h", "4h"]:
                    try:
                        rows = await fetcher.fetch(
                            exchange=exchange_name, symbol=symbol,
                            interval=interval, days=200
                        )
                        if rows:
                            logger.info(f"OHLCV warmup: {exchange_name}/{symbol}/{interval} → {rows} rows")
                    except Exception as e:
                        logger.debug(f"OHLCV warmup skip {exchange_name}/{symbol}/{interval}: {e}")

    asyncio.create_task(_warmup_ohlcv())

    # ── PID file (used by emergency_halt.py) ─────────────────────────────────
    import pathlib
    _pid_file = pathlib.Path("/tmp/trading_system.pid")
    _pid_file.write_text(str(os.getpid()))

    # ── DB-based halt flag check (set by emergency_halt.py when process was down) ─
    async def _check_db_halt_flag():
        try:
            async with storage._db.execute(
                "SELECT reason FROM halt_flags ORDER BY ts DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row:
                engine.risk_manager.halt(f"[DB flag] {row[0]}")
                logger.warning(f"Startup: DB halt flag active — {row[0]}")
        except Exception:
            pass  # table may not exist

    await _check_db_halt_flag()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, shutting down...")
        stop_event.set()

    def _emergency_halt() -> None:
        """SIGUSR1: halt trading immediately without stopping the process."""
        engine.risk_manager.halt("SIGUSR1 emergency halt signal received")
        logger.error("⚠ EMERGENCY HALT via SIGUSR1 — trading stopped, server still running")
        if engine._notifier:
            asyncio.create_task(engine._notifier.alert_halt("SIGUSR1 emergency halt"))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)
    loop.add_signal_handler(signal.SIGUSR1, _emergency_halt)

    _host = os.environ.get("SERVER_HOST", "0.0.0.0")
    _port = int(os.environ.get("SERVER_PORT", "8080"))
    server_config = uvicorn.Config(app, host=_host, port=_port, log_level="warning")
    server = uvicorn.Server(server_config)

    async def run_engine():
        try:
            await engine.start()
        except Exception as e:
            logger.error(f"Engine crashed: {e}", exc_info=True)
            stop_event.set()

    _gas_managers_ref = _gas_managers  # capture for closure

    async def run_until_stop():
        await stop_event.wait()
        await collector.stop()
        await fetcher.close()
        await reconciler.stop()
        await margin_monitor.stop()
        await factor_monitor.stop()
        await storage.close()
        for gm in _gas_managers_ref.values():
            await gm.stop()
        await engine.stop()
        _pid_file.unlink(missing_ok=True)
        server.should_exit = True

    logger.info("Trading system starting — API on http://0.0.0.0:8080")
    await asyncio.gather(run_engine(), server.serve(), run_until_stop())


if __name__ == "__main__":
    asyncio.run(main())
