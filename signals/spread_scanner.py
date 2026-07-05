"""Cross-exchange spot spread scanner.

Round-8 diagnosis: BTC/ETH cross-exchange spreads are structurally ~1 bp —
spread_arb starves on the majors. The real opportunities live in the long
tail of symbols listed on BOTH Binance and OKX spot, where thin market
making lets spreads spike well past fee levels.

This scanner polls both exchanges' full public spot tickers (one request
each), intersects the USDT-quoted universe, tracks a rolling window of the
best cross-exchange spread per symbol, and ranks symbols by how often they
exceed the target. Ranked candidates surface in the UI (Markets page),
where the operator can one-click subscribe a symbol's feeds — spread_arb
evaluates any symbol that has ticks from both exchanges, so a watched
symbol becomes tradeable immediately, still guarded by the strategy's
depth/inventory/permission checks.

Public endpoints only — works with broken/absent API keys.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import aiohttp

logger = logging.getLogger("SpreadScanner")

_BINANCE_BOOK = "https://api.binance.com/api/v3/ticker/bookTicker"
_OKX_TICKERS = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"

# Stablecoins and fiat pairs: spreads there are noise, not opportunity.
_EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "PYUSD", "EURI",
    "EUR", "GBP", "TRY", "BRL", "ARS", "JPY", "UAH", "PLN", "RON", "ZAR",
}
# Leveraged-token suffixes (Binance legacy).
_EXCLUDE_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


class SpreadScanner:
    def __init__(
        self,
        *,
        interval_s: float = 45.0,
        target_bps: float = 8.0,
        window_s: float = 3600.0,
        min_vol24h_usdt: float = 300_000.0,
        top_n: int = 15,
        max_sane_bps: float = 300.0,
    ):
        self._interval = interval_s
        self._target = target_bps
        self._window = window_s
        self._min_vol = min_vol24h_usdt
        self._top_n = top_n
        # Ticker collision guard: the same symbol on two exchanges can be two
        # DIFFERENT assets (e.g. "AI" is Sleepless AI on Binance, another
        # project on OKX — observed 4000+ bps apart). A real cross-exchange
        # spread beyond this is not an opportunity, it's a different token;
        # arbing it buys one asset and sells another. Blacklist permanently.
        self._max_sane = max_sane_bps
        self._suspect: set[str] = set()
        # symbol → deque[(ts, best_spread_bps, okx_vol24h_usdt)]
        self._stats: dict[str, deque] = {}
        self._last_scan_ts: float = 0.0
        self._scan_count: int = 0
        self._last_error: str = ""
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info(
                f"SpreadScanner started (every {self._interval:.0f}s, "
                f"target {self._target} bps, min vol ${self._min_vol:,.0f}/24h)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(15)  # let the engine finish starting first
        while True:
            try:
                await self.scan_once()
                self._last_error = ""
            except Exception as e:
                self._last_error = str(e)
                logger.warning(f"Scan failed: {e}")
            await asyncio.sleep(self._interval)

    # ── Scanning ──────────────────────────────────────────────────────────────

    async def scan_once(self) -> None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async def _get_json(url):
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            bn_raw, okx_raw = await asyncio.gather(
                _get_json(_BINANCE_BOOK), _get_json(_OKX_TICKERS))
        self.ingest(bn_raw, okx_raw)

    def ingest(self, bn_raw: list, okx_raw: dict) -> None:
        """Pure-ish core (no network) so tests can feed captured payloads."""
        bn_map: dict[str, tuple[float, float]] = {}
        for r in bn_raw:
            sym = r.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in _EXCLUDE_BASES or base.endswith(_EXCLUDE_SUFFIXES):
                continue
            try:
                bid, ask = float(r["bidPrice"]), float(r["askPrice"])
            except (KeyError, ValueError, TypeError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            bn_map[f"{base}-USDT"] = (bid, ask)

        okx_map: dict[str, tuple[float, float]] = {}
        vol_map: dict[str, float] = {}
        for r in okx_raw.get("data", []):
            inst = r.get("instId", "")
            if not inst.endswith("-USDT"):
                continue
            base = inst[:-5]
            if base in _EXCLUDE_BASES or base.endswith(_EXCLUDE_SUFFIXES):
                continue
            try:
                bid, ask = float(r["bidPx"]), float(r["askPx"])
                vol_q = float(r.get("volCcy24h") or 0)  # spot: quote-ccy turnover
            except (KeyError, ValueError, TypeError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            okx_map[inst] = (bid, ask)
            vol_map[inst] = vol_q

        now = time.time()
        for sym in bn_map.keys() & okx_map.keys():
            if sym in self._suspect:
                continue
            if vol_map.get(sym, 0.0) < self._min_vol:
                continue
            bn_bid, bn_ask = bn_map[sym]
            okx_bid, okx_ask = okx_map[sym]
            # Same definition as spread_arb: sell high side, buy low side.
            s_bn_over_okx = (bn_bid - okx_ask) / okx_ask * 10_000
            s_okx_over_bn = (okx_bid - bn_ask) / bn_ask * 10_000
            best = max(s_bn_over_okx, s_okx_over_bn)
            if abs(best) > self._max_sane:
                self._suspect.add(sym)
                self._stats.pop(sym, None)
                logger.warning(
                    f"Blacklisting {sym}: {best:.0f} bps apart — almost certainly "
                    f"different assets sharing a ticker, not an arb")
                continue
            dq = self._stats.setdefault(sym, deque())
            dq.append((now, best, vol_map.get(sym, 0.0)))
            while dq and dq[0][0] < now - self._window:
                dq.popleft()

        # Forget symbols that dropped out of the intersection entirely.
        cutoff = now - self._window
        for sym in [s for s, dq in self._stats.items() if not dq or dq[-1][0] < cutoff]:
            del self._stats[sym]

        self._last_scan_ts = now
        self._scan_count += 1

    # ── Reporting ─────────────────────────────────────────────────────────────

    def report(self, top_n: Optional[int] = None) -> dict:
        out = []
        for sym, dq in self._stats.items():
            if not dq:
                continue
            vals = [b for _, b, _ in dq]
            hits = sum(1 for b in vals if b >= self._target)
            out.append({
                "symbol": sym,
                "last_bps": round(vals[-1], 2),
                "avg_bps": round(sum(vals) / len(vals), 2),
                "max_bps": round(max(vals), 2),
                "hits": hits,
                "samples": len(vals),
                "vol24h_usdt": round(dq[-1][2]),
            })
        out.sort(key=lambda r: (-r["hits"], -r["max_bps"]))
        return {
            "ts": self._last_scan_ts,
            "scan_count": self._scan_count,
            "interval_s": self._interval,
            "target_bps": self._target,
            "window_s": self._window,
            "min_vol24h_usdt": self._min_vol,
            "last_error": self._last_error,
            "blacklisted": sorted(self._suspect),
            "symbols": out[: top_n or self._top_n],
        }
