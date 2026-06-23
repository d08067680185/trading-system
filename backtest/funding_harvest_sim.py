"""
Funding-harvest *backtest* (basis-aware), as opposed to the screener estimate in
`funding_harvest.py`.

The screener annualizes the mean funding rate — it ignores the hedge. A real
delta-neutral harvest holds two legs: short the perp + long the spot (when funding
is persistently positive, so the perp short COLLECTS funding from longs), or the
mirror when funding is persistently negative. Its actual PnL is:

    net = funding collected  +  basis PnL  −  fees

where basis PnL = the price move of the spot leg minus the perp leg (i.e. how the
perp premium over spot drifted while held) — the risk the screener can't see. This
simulator replays real perp + spot OHLCV alongside the funding settlements and
reports the three components separately, so you can tell genuine carry from a basis
that happened to drift your way.

Model: enter the full notional delta-neutral at the first aligned bar, hold to the
last, exit there. Funding is applied at each real settlement (deduped via
`funding_harvest._settlements`); a settlement on the side you committed to is
income, one that flipped is a cost. Marks use bar opens (no look-ahead). Fees are
charged on all four fills (two legs in, two legs out).
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.request
from dataclasses import dataclass, asdict
from typing import Optional

import certifi

from backtest.metrics import calculate
from backtest.funding_harvest import _settlements
from data.storage import OHLCVRow

_FAPI = "https://fapi.binance.com/fapi/v1/klines"   # USDT-M perp
_SPOT = "https://api.binance.com/api/v3/klines"     # spot
_INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "8h": 28_800_000, "1d": 86_400_000}


@dataclass
class HarvestBacktestResult:
    symbol: str
    perp_exchange: str
    side: str                    # "short_perp" (collect +funding) | "long_perp"
    n_bars: int
    span_days: float
    n_settlements: int
    favorable_pct: float         # % of applied settlements that were income, not cost
    notional_usdt: float
    funding_collected_usdt: float
    basis_pnl_usdt: float        # price PnL of the hedged pair (the carry's risk)
    fees_usdt: float
    net_pnl_usdt: float
    net_return_pct: float        # net / initial capital
    apr_pct: float               # net return annualized over the held span
    sharpe_ratio: float
    max_drawdown_pct: float

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def simulate(
    symbol: str,
    perp_bars: list,
    spot_bars: list,
    funding_rows: list[dict],
    *,
    perp_exchange: str = "binance",
    initial_capital: float = 10_000.0,
    notional_usdt: Optional[float] = None,
    fee_bps_per_leg: float = 2.0,
    side: Optional[str] = None,
) -> HarvestBacktestResult:
    """Backtest a delta-neutral funding harvest over aligned perp/spot bars.

    `notional_usdt` defaults to the full initial capital (1× delta-neutral). `side`
    forces "short_perp"/"long_perp"; if None it is chosen from the dominant funding
    sign (short the perp when funding is net positive)."""
    perp = {b.ts: b for b in perp_bars}
    spot = {b.ts: b for b in spot_bars}
    ts = sorted(set(perp) & set(spot))
    if len(ts) < 2:
        raise ValueError(f"{symbol}: need ≥2 overlapping perp/spot bars, got {len(ts)}")

    p_open = [float(perp[t].open) for t in ts]
    s_open = [float(spot[t].open) for t in ts]
    span_days = max((ts[-1] - ts[0]) / 86400.0, 1e-9)

    settles = [(t, r) for t, r in _settlements(funding_rows) if ts[0] <= t <= ts[-1]]
    if side is None:
        net_rate = sum(r for _, r in settles)
        side = "short_perp" if net_rate >= 0 else "long_perp"

    notional = float(notional_usdt if notional_usdt is not None else initial_capital)
    qty = notional / p_open[0]                       # coin amount per leg
    # Delta-neutral leg positions (coin): short perp + long spot, or the mirror.
    perp_pos = -qty if side == "short_perp" else qty
    spot_pos = -perp_pos

    fee = fee_bps_per_leg / 10_000.0
    fee_entry = (abs(perp_pos) * p_open[0] + abs(spot_pos) * s_open[0]) * fee
    fee_exit = (abs(perp_pos) * p_open[-1] + abs(spot_pos) * s_open[-1]) * fee

    equity_curve: list[tuple[float, float]] = []
    cum_price = 0.0
    cum_fund = 0.0
    favorable = 0
    applied = 0
    si = 0
    for i, t in enumerate(ts):
        if i > 0:
            cum_price += perp_pos * (p_open[i] - p_open[i - 1])
            cum_price += spot_pos * (s_open[i] - s_open[i - 1])
        # Apply every settlement up to this bar's time.
        while si < len(settles) and settles[si][0] <= t:
            rate = settles[si][1]
            # Long pays short when rate > 0 → cashflow = −perp_pos · rate · price.
            cash = -perp_pos * rate * p_open[i]
            cum_fund += cash
            applied += 1
            if cash > 0:
                favorable += 1
            si += 1
        equity_curve.append((float(t), initial_capital - fee_entry + cum_price + cum_fund))

    equity_curve[-1] = (equity_curve[-1][0], equity_curve[-1][1] - fee_exit)

    funding_collected = cum_fund
    basis_pnl = cum_price
    fees = fee_entry + fee_exit
    net = funding_collected + basis_pnl - fees
    net_ret_pct = net / initial_capital * 100.0
    apr = net_ret_pct / span_days * 365.0

    avg_s = (ts[-1] - ts[0]) / (len(ts) - 1)
    ppy = int(365 * 86400 / avg_s) if avg_s > 0 else 365 * 24
    m = calculate(equity_curve, [], initial_capital, periods_per_year=ppy)

    return HarvestBacktestResult(
        symbol=symbol, perp_exchange=perp_exchange, side=side,
        n_bars=len(ts), span_days=span_days,
        n_settlements=applied,
        favorable_pct=(favorable / applied * 100.0) if applied else 0.0,
        notional_usdt=notional,
        funding_collected_usdt=funding_collected, basis_pnl_usdt=basis_pnl,
        fees_usdt=fees, net_pnl_usdt=net, net_return_pct=net_ret_pct, apr_pct=apr,
        sharpe_ratio=m.sharpe_ratio, max_drawdown_pct=m.max_drawdown_pct,
    )


# ── Real-data runner (fetches perp + spot klines from Binance public API) ─────

def _fetch_klines(base_url: str, symbol: str, interval: str,
                  start_ms: int, end_ms: int) -> list[OHLCVRow]:
    """Fetch Binance klines (perp via FAPI, spot via API) into OHLCVRow bars.
    Empty list = the market doesn't exist (e.g. a perp-only alt has no spot)."""
    ex_symbol = symbol.replace("-", "")
    step = _INTERVAL_MS.get(interval, 3_600_000)
    ctx = ssl.create_default_context(cafile=certifi.where())
    out: list[OHLCVRow] = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{base_url}?symbol={ex_symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit=1500")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                rows = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):     # unknown symbol on this market
                return []
            raise
        if not rows:
            break
        for k in rows:
            out.append(OHLCVRow(exchange="binance", symbol=symbol, interval=interval,
                                ts=int(k[0]) // 1000, open=float(k[1]), high=float(k[2]),
                                low=float(k[3]), close=float(k[4]), volume=float(k[5])))
        last = int(rows[-1][0])
        if len(rows) < 1500:
            break
        cur = last + step
    return out


class HarvestBacktestRunner:
    """Loads a symbol's funding history from storage, fetches its perp + spot
    OHLCV from Binance for that window, and runs the basis-aware harvest sim.

    Many high-funding alts trade perp but NOT spot — those raise ValueError
    ("no spot market"); a real harvest there needs a different hedge venue."""

    def __init__(self, storage):
        self._db = storage

    async def run(
        self, symbol: str, *, days: int = 30, interval: str = "1h",
        initial_capital: float = 10_000.0, notional_usdt: Optional[float] = None,
        fee_bps_per_leg: float = 2.0,
    ) -> HarvestBacktestResult:
        start_ts = time.time() - days * 86400
        funding = await self._db.get_funding_rates(
            exchange="binance", symbol=symbol, start_ts=start_ts, limit=100_000,
        )
        if len(funding) < 2:
            raise ValueError(f"{symbol}: no funding history in DB for the window")
        fts = [float(r["ts"]) for r in funding]
        start_ms, end_ms = int(min(fts) * 1000), int(max(fts) * 1000) + _INTERVAL_MS.get(interval, 3_600_000)

        perp, spot = await asyncio.gather(
            asyncio.to_thread(_fetch_klines, _FAPI, symbol, interval, start_ms, end_ms),
            asyncio.to_thread(_fetch_klines, _SPOT, symbol, interval, start_ms, end_ms),
        )
        if not perp:
            raise ValueError(f"{symbol}: no perp market on Binance")
        if not spot:
            raise ValueError(f"{symbol}: no spot market on Binance — harvest needs a hedge venue")

        return simulate(
            symbol, perp, spot, funding, perp_exchange="binance",
            initial_capital=initial_capital, notional_usdt=notional_usdt,
            fee_bps_per_leg=fee_bps_per_leg,
        )
