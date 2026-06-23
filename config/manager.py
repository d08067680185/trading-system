from __future__ import annotations
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml

from core.types import Exchange, MarketType
from risk.manager import RiskConfig


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""
    loss_warn_pct: int = 80  # alert when daily loss > this % of limit


@dataclass
class ApiConfig:
    key: str = ""  # if empty, authentication is disabled


@dataclass
class ExchangeConfig:
    api_key: str
    secret: str
    market_type: MarketType
    testnet: bool = False
    passphrase: str = ""  # OKX only


@dataclass
class EngineConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC-USDT", "ETH-USDT"])
    orderbook_depth: int = 20
    log_level: str = "INFO"
    max_quote_age_s: float = 10.0           # block new entries on quotes older than this (staleness guard)
    order_ttl_s: float = 0.0                # auto-cancel resting orders older than this; 0 = disabled
    cancel_orders_on_shutdown: bool = True  # cancel all open orders on graceful shutdown
    order_poll_interval_s: float = 3.0      # REST poll of in-flight orders (fill fallback while private WS is down); 0 = disabled
    auto_heal_feeds: bool = True            # auto-reconnect a connector whose market-data feed silently freezes


@dataclass
class ChainConfig:
    enabled: bool = False
    rpc_url: str = ""
    private_key: str = ""
    chain_id: int = 1
    gas_priority_multiplier: float = 1.2
    gas_base_multiplier: float = 1.1


@dataclass
class DexConfig:
    enabled: bool = False
    min_profit_bps: int = 10
    max_slippage_bps: int = 50


@dataclass
class DataConfig:
    db_path: str = "data/trading_data.db"
    tick_sample_rate: int = 10
    equity_snapshot_interval: int = 60


@dataclass
class BacktestConfig:
    default_initial_capital: float = 10_000.0
    taker_fee_bps: int = 4
    default_interval: str = "1h"
    parity: bool = False   # run backtests through the parity engine (real TradingEngine + RiskManager)


@dataclass
class AppConfig:
    exchanges: dict[Exchange, ExchangeConfig]
    risk: RiskConfig
    engine: EngineConfig
    chains: dict[str, ChainConfig] = field(default_factory=dict)
    dex: DexConfig = field(default_factory=DexConfig)
    data: DataConfig = field(default_factory=DataConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    api: ApiConfig = field(default_factory=ApiConfig)


def _expand_env(value):
    """Expand ${VAR} or $VAR placeholders in string config values."""
    if not isinstance(value, str):
        return value
    import re
    return re.sub(r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                  lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)),
                  value)


def load_config(path: str = "config.yaml") -> AppConfig:
    # Auto-load .env file if present (no dependency on python-dotenv)
    env_file = Path(path).parent / ".env"
    if not env_file.exists():
        env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    raw = yaml.safe_load(Path(path).read_text())

    exchanges: dict[Exchange, ExchangeConfig] = {}
    for name, cfg in raw.get("exchanges", {}).items():
        ex = Exchange(name)
        env_prefix = name.upper()
        passphrase = (
            os.environ.get(f"{env_prefix}_PASSPHRASE")
            or os.environ.get("OKX_PASSPHRASE")
            or cfg.get("passphrase", "")
        )
        exchanges[ex] = ExchangeConfig(
            api_key=os.environ.get(f"{env_prefix}_API_KEY", cfg.get("api_key", "")),
            secret=os.environ.get(f"{env_prefix}_SECRET", cfg.get("secret", "")),
            passphrase=passphrase,
            market_type=MarketType(cfg.get("market_type", "futures")),
            testnet=cfg.get("testnet", False),
        )

    risk_raw = raw.get("risk", {})
    risk = RiskConfig(
        max_position_usdt=Decimal(str(risk_raw.get("max_position_usdt", 1000))),
        max_order_usdt=Decimal(str(risk_raw.get("max_order_usdt", 500))),
        max_daily_loss_usdt=Decimal(str(risk_raw.get("max_daily_loss_usdt", 200))),
        max_open_orders=risk_raw.get("max_open_orders", 10),
        enabled=risk_raw.get("enabled", True),
        max_drawdown_pct=Decimal(str(risk_raw.get("max_drawdown_pct", 0))),
        max_symbol_concentration_pct=Decimal(str(risk_raw.get("max_symbol_concentration_pct", 0))),
        max_rolling_7d_loss_usdt=Decimal(str(risk_raw.get("max_rolling_7d_loss_usdt", 0))),
        max_rolling_30d_loss_usdt=Decimal(str(risk_raw.get("max_rolling_30d_loss_usdt", 0))),
    )

    engine_raw = raw.get("engine", {})
    engine = EngineConfig(
        symbols=engine_raw.get("symbols", ["BTC-USDT", "ETH-USDT"]),
        orderbook_depth=engine_raw.get("orderbook_depth", 20),
        log_level=engine_raw.get("log_level", "INFO"),
        max_quote_age_s=engine_raw.get("max_quote_age_s", 10.0),
        order_ttl_s=engine_raw.get("order_ttl_s", 0.0),
        cancel_orders_on_shutdown=engine_raw.get("cancel_orders_on_shutdown", True),
        order_poll_interval_s=engine_raw.get("order_poll_interval_s", 3.0),
        auto_heal_feeds=engine_raw.get("auto_heal_feeds", True),
    )

    chains: dict[str, ChainConfig] = {}
    for name, cfg in raw.get("chains", {}).items():
        chains[name] = ChainConfig(
            enabled=cfg.get("enabled", False),
            rpc_url=os.environ.get(f"{name.upper()}_RPC_URL", cfg.get("rpc_url", "")),
            private_key=os.environ.get(f"{name.upper()}_PRIVATE_KEY", cfg.get("private_key", "")),
            chain_id=cfg.get("chain_id", 1),
            gas_priority_multiplier=cfg.get("gas_priority_multiplier", 1.2),
            gas_base_multiplier=cfg.get("gas_base_multiplier", 1.1),
        )

    dex_raw = raw.get("dex", {})
    dex = DexConfig(
        enabled=dex_raw.get("enabled", False),
        min_profit_bps=dex_raw.get("min_profit_bps", 10),
        max_slippage_bps=dex_raw.get("max_slippage_bps", 50),
    )

    data_raw = raw.get("data", {})
    data = DataConfig(
        db_path=data_raw.get("db_path", "data/trading_data.db"),
        tick_sample_rate=data_raw.get("tick_sample_rate", 10),
        equity_snapshot_interval=data_raw.get("equity_snapshot_interval", 60),
    )

    bt_raw = raw.get("backtest", {})
    backtest = BacktestConfig(
        default_initial_capital=bt_raw.get("default_initial_capital", 10_000.0),
        taker_fee_bps=bt_raw.get("taker_fee_bps", 4),
        default_interval=bt_raw.get("default_interval", "1h"),
        parity=bt_raw.get("parity", False),
    )

    tg_raw = raw.get("telegram", {})
    telegram = TelegramConfig(
        token=os.environ.get("TELEGRAM_BOT_TOKEN", tg_raw.get("token", "")),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", tg_raw.get("chat_id", "")),
        loss_warn_pct=tg_raw.get("loss_warn_pct", 80),
    )

    api_raw = raw.get("api", {})
    api = ApiConfig(
        key=os.environ.get("TRADING_API_KEY", api_raw.get("key", "")),
    )

    return AppConfig(exchanges=exchanges, risk=risk, engine=engine,
                     chains=chains, dex=dex, data=data, backtest=backtest,
                     telegram=telegram, api=api)
