"""Tests for the second hardening round:
OKX contract-value (ctVal) conversion, quote staleness, fee normalization, reaper exclusion."""
import time
from decimal import Decimal

import pytest

from connectors.okx import OKXConnector
from connectors.base import SymbolRule
from core.types import Exchange, MarketType, OrderSide
from config.manager import AppConfig, EngineConfig, RiskConfig
from core.engine import TradingEngine


# ── A: OKX contract-value conversion ─────────────────────────────────────────

def _okx_with_rule():
    c = OKXConnector("", "", "", MarketType.SWAP)
    c._rules["BTC-USDT"] = SymbolRule(
        tick_size=Decimal("0.1"), step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"), contract_val=Decimal("0.01"),
    )
    return c


def test_ctval_lookup():
    c = _okx_with_rule()
    assert c._ctval("BTC-USDT") == Decimal("0.01")
    assert c._ctval("UNKNOWN-USDT") == Decimal("1")   # default when no rule


def test_okx_position_contracts_to_coin():
    c = _okx_with_rule()
    # OKX reports pos in contracts; 5 contracts * 0.01 coin/contract = 0.05 coin
    pos = c._parse_rest_position({"instId": "BTC-USDT-SWAP", "pos": "5",
                                  "avgPx": "60000", "lever": "3"})
    assert pos.size == Decimal("0.05")


def test_okx_order_fillsz_contracts_to_coin():
    c = _okx_with_rule()
    o = c._parse_rest_order({"instId": "BTC-USDT-SWAP", "side": "buy", "ordType": "limit",
                             "sz": "10", "fillSz": "4", "px": "60000", "state": "partially_filled",
                             "fee": "-0.1", "feeCcy": "USDT"}, "BTC-USDT")
    assert o.quantity == Decimal("0.1")     # 10 contracts → 0.1 coin
    assert o.filled_qty == Decimal("0.04")  # 4 contracts → 0.04 coin
    assert o.fee == Decimal("0.1")          # abs()
    assert o.fee_ccy == "USDT"


# ── engine helpers: staleness / fee FX / reaper exclusion ────────────────────

def _engine():
    cfg = AppConfig(exchanges={}, risk=RiskConfig(), engine=EngineConfig())
    return TradingEngine(cfg)


def test_quote_age_none_when_unseen():
    e = _engine()
    assert e._quote_age("binance", "BTC-USDT") is None


def test_quote_age_tracks_recency():
    e = _engine()
    e._last_ticker[("binance", "BTC-USDT")] = (Decimal("1"), Decimal("2"), Decimal("1.5"), time.time() - 3)
    age = e._quote_age("binance", "BTC-USDT")
    assert 2.5 < age < 4.0


def test_normalize_fee_converts_bnb_to_usdt():
    e = _engine()
    e._last_ticker[("binance", "BNB-USDT")] = (Decimal("600"), Decimal("601"), Decimal("600"), time.time())
    from core.types import Order, OrderType
    order = Order(exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=Decimal("0.001"),
                  fee=Decimal("0.01"), fee_ccy="BNB")
    e._normalize_fee(order)
    assert order.fee == Decimal("6.00")   # 0.01 BNB * 600
    assert order.fee_ccy == "USDT"


def test_normalize_fee_skips_usdt():
    e = _engine()
    from core.types import Order, OrderType
    order = Order(exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=Decimal("0.001"),
                  fee=Decimal("0.5"), fee_ccy="USDT")
    e._normalize_fee(order)
    assert order.fee == Decimal("0.5")    # unchanged


def test_normalize_fee_no_price_leaves_raw():
    e = _engine()
    from core.types import Order, OrderType
    order = Order(exchange=Exchange.BINANCE, symbol="BTC-USDT", side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=Decimal("0.001"),
                  fee=Decimal("0.01"), fee_ccy="XYZ")
    e._normalize_fee(order)
    assert order.fee == Decimal("0.01") and order.fee_ccy == "XYZ"


def test_strategy_rests_orders_flag():
    from strategies.grid import SpotGridStrategy
    from strategies.spread_arb import SpreadArbStrategy
    assert SpotGridStrategy.keeps_resting_orders is True
    e = _engine()
    grid = SpotGridStrategy("grid1", {"grid_low": 1, "grid_high": 2})
    e.strategies.append(grid)
    assert e._strategy_rests_orders("grid1") is True
    assert e._strategy_rests_orders("unknown") is False
