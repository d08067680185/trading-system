"""
Historical OHLCV data fetcher.
Downloads candle data from Binance/OKX public REST APIs (no auth required).
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

import aiohttp

from data.storage import DataStorage, OHLCVRow

logger = logging.getLogger("HistoricalFetcher")

# Interval string normalization
BINANCE_INTERVALS = {"1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","3d","1w"}
OKX_INTERVAL_MAP  = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "1d": "1D",
}


class HistoricalFetcher:
    def __init__(self, storage: DataStorage):
        self._db = storage
        self._session = None

    async def _get_session(self):
        if not self._session:
            import ssl, certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    # ── Binance ───────────────────────────────────────────────────────────────

    async def fetch_binance(
        self,
        symbol: str,          # "BTC-USDT" → "BTCUSDT"
        interval: str = "1h",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        market_type: str = "futures",
    ) -> int:
        """Fetch and store Binance candles. Returns total rows stored."""
        bn_symbol = symbol.replace("-", "")
        base = "https://fapi.binance.com" if market_type == "futures" else "https://api.binance.com"
        endpoint = f"{base}/{'fapi/v1' if market_type == 'futures' else 'api/v3'}/klines"
        if interval not in BINANCE_INTERVALS:
            raise ValueError(f"Invalid interval: {interval}")

        session = await self._get_session()
        total = 0
        current_start = start_ms or int((time.time() - 365 * 86400) * 1000)
        end = end_ms or int(time.time() * 1000)

        while current_start < end:
            params = {"symbol": bn_symbol, "interval": interval,
                      "startTime": current_start, "endTime": end, "limit": 1000}
            data = None
            for attempt in range(4):
                try:
                    async with session.get(endpoint, params=params,
                                           timeout=aiohttp.ClientTimeout(total=30)) as r:
                        if r.status == 429:
                            wait = 2 ** attempt * 5
                            logger.warning(f"Binance rate-limited, retrying in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        if r.status != 200:
                            logger.error(f"Binance {r.status}: {await r.text()}")
                            break
                        data = await r.json()
                        break
                except Exception as e:
                    wait = 2 ** attempt
                    logger.warning(f"Binance fetch error (attempt {attempt+1}): {e}, retrying in {wait}s")
                    await asyncio.sleep(wait)
            if data is None:
                break

            if not data:
                break

            rows = [OHLCVRow(
                exchange="binance", symbol=symbol, interval=interval,
                ts=int(c[0] / 1000),
                open=float(c[1]), high=float(c[2]),
                low=float(c[3]), close=float(c[4]), volume=float(c[5]),
            ) for c in data]

            await self._db.store_ohlcv(rows)
            total += len(rows)
            current_start = int(data[-1][0]) + 1
            logger.info(f"Binance {symbol} {interval}: fetched {len(rows)} bars (total {total})")

            if len(data) < 1000:
                break
            await asyncio.sleep(0.1)  # rate limit

        return total

    # ── OKX ───────────────────────────────────────────────────────────────────

    async def fetch_okx(
        self,
        symbol: str,          # "BTC-USDT"
        interval: str = "1h",
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        market_type: str = "swap",
    ) -> int:
        inst_id = f"{symbol}-SWAP" if market_type == "swap" else symbol
        okx_bar = OKX_INTERVAL_MAP.get(interval)
        if not okx_bar:
            raise ValueError(f"Unsupported OKX interval: {interval}")

        session = await self._get_session()
        total = 0
        end = end_ms or int(time.time() * 1000)
        current_end = end

        while True:
            params = {"instId": inst_id, "bar": okx_bar, "limit": 100, "after": current_end}
            if start_ms:
                params["before"] = start_ms
            body = None
            for attempt in range(4):
                try:
                    async with session.get(
                        "https://www.okx.com/api/v5/market/history-candles", params=params,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        if r.status == 429:
                            wait = 2 ** attempt * 5
                            logger.warning(f"OKX rate-limited, retrying in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        body = await r.json()
                        break
                except Exception as e:
                    wait = 2 ** attempt
                    logger.warning(f"OKX fetch error (attempt {attempt+1}): {e}, retrying in {wait}s")
                    await asyncio.sleep(wait)
            if body is None:
                break

            data = body.get("data", [])
            if not data:
                break

            rows = [OHLCVRow(
                exchange="okx", symbol=symbol, interval=interval,
                ts=int(c[0]) // 1000,
                open=float(c[1]), high=float(c[2]),
                low=float(c[3]), close=float(c[4]), volume=float(c[5]),
            ) for c in data]

            await self._db.store_ohlcv(rows)
            total += len(rows)
            oldest_ts = int(data[-1][0])
            logger.info(f"OKX {symbol} {interval}: fetched {len(rows)} bars (total {total})")

            if start_ms and oldest_ts <= start_ms:
                break
            if len(data) < 100:
                break
            current_end = oldest_ts - 1
            await asyncio.sleep(0.2)

        return total

    # ── Unified API ───────────────────────────────────────────────────────────

    async def fetch(
        self,
        exchange: str,
        symbol: str,
        interval: str = "1h",
        days: int = 365,
    ) -> int:
        """Convenience: fetch last N days of candles from exchange."""
        start_ms = int((time.time() - days * 86400) * 1000)
        if exchange == "binance":
            return await self.fetch_binance(symbol, interval, start_ms=start_ms)
        elif exchange == "okx":
            return await self.fetch_okx(symbol, interval, start_ms=start_ms)
        else:
            raise ValueError(f"Unknown exchange: {exchange}")

    # ── Integrity check ───────────────────────────────────────────────────────

    INTERVAL_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
        "1d": 86400, "3d": 259200, "1w": 604800,
    }

    async def check_integrity(
        self,
        exchange: str,
        symbol: str,
        interval: str = "1h",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> dict:
        """Check stored OHLCV data for gaps and coverage."""
        rows = await self._db.get_ohlcv(
            exchange, symbol, interval,
            start_ts=start_ts, end_ts=end_ts,
            limit=200_000,
        )
        if not rows:
            return {"total_candles": 0, "gaps": 0, "coverage_pct": 0.0, "gap_list": []}

        step = self.INTERVAL_SECONDS.get(interval, 3600)
        timestamps = [r.ts for r in rows]
        timestamps.sort()

        gaps = []
        for i in range(1, len(timestamps)):
            diff = timestamps[i] - timestamps[i - 1]
            if diff > step * 1.5:
                missing = int(diff / step) - 1
                gaps.append({
                    "from_ts": timestamps[i - 1],
                    "to_ts": timestamps[i],
                    "missing_candles": missing,
                })

        span = timestamps[-1] - timestamps[0]
        expected = max(1, span // step + 1)
        coverage_pct = round(len(rows) / expected * 100, 1)

        return {
            "total_candles": len(rows),
            "expected_candles": expected,
            "gaps": len(gaps),
            "coverage_pct": coverage_pct,
            "first_ts": timestamps[0],
            "last_ts": timestamps[-1],
            "gap_list": gaps[:50],  # return first 50 gaps max
        }

    async def fill_gaps(
        self,
        exchange: str,
        symbol: str,
        interval: str = "1h",
        market_type: str = "futures",
        max_gaps: int = 20,
    ) -> dict:
        """Auto-fill detected gaps by fetching missing candles from the exchange.

        Returns a summary of how many candles were fetched per gap.
        Only fills the first `max_gaps` gaps to avoid runaway requests.
        """
        integrity = await self.check_integrity(exchange, symbol, interval)
        gap_list = integrity.get("gap_list", [])[:max_gaps]
        if not gap_list:
            return {"gaps_found": 0, "gaps_filled": 0, "candles_added": 0}

        total_added = 0
        filled = 0
        for gap in gap_list:
            start_ms = gap["from_ts"] * 1000
            end_ms   = gap["to_ts"] * 1000
            try:
                added = await self.fetch(
                    exchange=exchange,
                    symbol=symbol,
                    interval=interval,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    market_type=market_type,
                )
                total_added += added
                filled += 1
                logger.info(f"Gap fill [{exchange}] {symbol} {interval}: +{added} candles")
            except Exception as e:
                logger.warning(f"Gap fill failed for {gap}: {e}")

        return {
            "gaps_found": len(gap_list),
            "gaps_filled": filled,
            "candles_added": total_added,
        }
