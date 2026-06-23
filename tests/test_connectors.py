"""Unit tests for connector symbol normalization."""
import pytest
from connectors.base import symbol_to_exchange, symbol_from_exchange
from core.types import Exchange, MarketType


def test_binance_futures_symbol_format():
    raw = symbol_to_exchange("BTC-USDT", Exchange.BINANCE, MarketType.FUTURES)
    assert raw == "BTCUSDT"


def test_binance_spot_symbol_format():
    raw = symbol_to_exchange("BTC-USDT", Exchange.BINANCE_SPOT, MarketType.SPOT)
    assert raw == "BTCUSDT"


def test_okx_swap_symbol_format():
    raw = symbol_to_exchange("BTC-USDT", Exchange.OKX, MarketType.SWAP)
    assert raw == "BTC-USDT-SWAP"


def test_okx_spot_symbol_format():
    raw = symbol_to_exchange("BTC-USDT", Exchange.OKX_SPOT, MarketType.SPOT)
    assert raw == "BTC-USDT"


def test_binance_symbol_roundtrip():
    original = "BTC-USDT"
    raw = symbol_to_exchange(original, Exchange.BINANCE, MarketType.FUTURES)
    restored = symbol_from_exchange(raw, Exchange.BINANCE)
    assert restored == original


def test_okx_swap_symbol_roundtrip():
    original = "BTC-USDT"
    raw = symbol_to_exchange(original, Exchange.OKX, MarketType.SWAP)
    restored = symbol_from_exchange(raw, Exchange.OKX)
    assert restored == original


def test_eth_symbol_formats():
    assert symbol_to_exchange("ETH-USDT", Exchange.BINANCE, MarketType.FUTURES) == "ETHUSDT"
    assert symbol_to_exchange("ETH-USDT", Exchange.OKX, MarketType.SWAP) == "ETH-USDT-SWAP"


def test_symbol_from_binance_various_quotes():
    assert symbol_from_exchange("ETHBTC", Exchange.BINANCE) == "ETH-BTC"
    assert symbol_from_exchange("BNBUSDT", Exchange.BINANCE) == "BNB-USDT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
