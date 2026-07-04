"""Trade-permission probes (connectors) — offline via stubbed _request.

A read-only API key or IP-whitelist mismatch passes market-data checks and only
fails at order time; the probe must classify these before real money is at stake.
"""
import asyncio

from connectors.binance import BinanceConnector
from connectors.okx import OKXConnector
from core.types import MarketType


def _binance(market_type=MarketType.SPOT):
    return BinanceConnector("key", "secret", market_type=market_type)


def _okx():
    return OKXConnector("key", "secret", "pass", market_type=MarketType.SPOT)


def _stub(conn, response=None, error=None):
    async def fake_request(method, path, **kw):
        if error is not None:
            raise error
        return response
    conn._request = fake_request


def test_binance_spot_can_trade():
    c = _binance()
    _stub(c, {"canTrade": True, "permissions": ["SPOT"]})
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert ok


def test_binance_spot_read_only_key():
    c = _binance()
    _stub(c, {"canTrade": False, "permissions": ["SPOT"]})
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert not ok
    assert "Spot & Margin Trading" in detail


def test_binance_auth_error_reported():
    c = _binance()
    _stub(c, error=RuntimeError(
        "Binance GET /api/v3/account: 401 {'code': -2015, 'msg': 'Invalid API-key'}"))
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert not ok
    assert "-2015" in detail


def test_binance_futures_can_trade():
    c = _binance(MarketType.FUTURES)
    _stub(c, {"canTrade": True})
    ok, _ = asyncio.run(c.probe_trade_permission())
    assert ok


def test_binance_no_key():
    c = BinanceConnector("", "", market_type=MarketType.SPOT)
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert not ok
    assert "no API key" in detail


def test_okx_perm_includes_trade():
    c = _okx()
    _stub(c, {"code": "0", "data": [{"perm": "read_only,trade"}]})
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert ok
    assert "trade" in detail


def test_okx_read_only_perm():
    c = _okx()
    _stub(c, {"code": "0", "data": [{"perm": "read_only"}]})
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert not ok
    assert "lack trade" in detail


def test_okx_auth_error_reported():
    c = _okx()
    _stub(c, error=RuntimeError("OKX GET /api/v5/account/config: 50111 Invalid OK-ACCESS-KEY"))
    ok, detail = asyncio.run(c.probe_trade_permission())
    assert not ok
    assert "50111" in detail
