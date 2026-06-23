"""Tests for the funding-rate market scan (high-funding alt discovery)."""
import asyncio
from decimal import Decimal

from strategies.funding_rate import FundingRateArbStrategy


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _FakeSession:
    """Returns premiumIndex payload first, ticker/24hr payload second."""
    def __init__(self, prem, tickers):
        self._responses = {"premiumIndex": prem, "ticker/24hr": tickers}

    def get(self, url, **kw):
        for key, payload in self._responses.items():
            if key in url:
                return _FakeResp(payload)
        raise AssertionError(f"unexpected url {url}")


def _strategy(**params):
    defaults = {"scan_all": True, "scan_top_n": 2, "min_volume_24h_usdt": 1000.0}
    defaults.update(params)
    return FundingRateArbStrategy("funding_arb", defaults)


def test_scan_ranks_by_abs_rate_and_filters_volume():
    prem = [
        {"symbol": "AAAUSDT", "lastFundingRate": "0.0001", "markPrice": "1.0"},
        {"symbol": "BBBUSDT", "lastFundingRate": "-0.0050", "markPrice": "2.0"},   # highest |rate|
        {"symbol": "CCCUSDT", "lastFundingRate": "0.0030", "markPrice": "3.0"},
        {"symbol": "DDDUSDT", "lastFundingRate": "0.0099", "markPrice": "4.0"},    # filtered: low volume
        {"symbol": "EEEUSDC", "lastFundingRate": "0.0090", "markPrice": "5.0"},    # filtered: not USDT
        {"symbol": "BTCUSDT_260626", "lastFundingRate": "0.0090"},                 # filtered: dated future
        {"symbol": "FFFUSDT", "lastFundingRate": "0"},                             # filtered: zero rate
    ]
    tickers = [
        {"symbol": "AAAUSDT", "quoteVolume": "5000"},
        {"symbol": "BBBUSDT", "quoteVolume": "5000"},
        {"symbol": "CCCUSDT", "quoteVolume": "5000"},
        {"symbol": "DDDUSDT", "quoteVolume": "10"},
        {"symbol": "EEEUSDC", "quoteVolume": "5000"},
        {"symbol": "FFFUSDT", "quoteVolume": "5000"},
    ]
    s = _strategy()
    top = asyncio.run(s._scan_binance_candidates(_FakeSession(prem, tickers)))
    assert top == ["BBB-USDT", "CCC-USDT"]


def test_fetch_binance_rates_captures_mark_price():
    prem = [{"symbol": "XYZUSDT", "lastFundingRate": "0.002",
             "markPrice": "42.5", "nextFundingTime": 1781000000000}]
    s = _strategy()
    rates = asyncio.run(s._fetch_binance_rates(_FakeSession(prem, []), ["XYZ-USDT"]))
    assert rates == {"XYZ-USDT": 0.002}
    assert s._mark_prices["XYZ-USDT"] == 42.5
    assert s._next_funding_ts["XYZ-USDT"] == 1781000000.0
