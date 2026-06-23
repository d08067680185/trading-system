"""
Funding-harvest analyzer.

Mines the stored `funding_rates` history to rank symbols by how profitable a
delta-neutral funding harvest *would have been* (collect the periodic funding by
holding the perp on the receiving side, hedged against price). This answers the
funding strategy's core economic question — which symbols carry capturable
funding — without backtesting the live funding_arb execution (which HTTP-polls
exchanges and uses real-time blocking maker loops, neither replayable).

Model (per symbol over the window):
  - polled snapshots are first collapsed to one rate per *actual settlement*
    (keyed on next_funding_time, falling back to 8h time-buckets) so oversampling
    a slow-moving rate doesn't bias the mean; `n_periods` is real settlements.
  - dominant_sign = sign of the summed rate → the side you commit to
    (+1 = short the perp, collect from longs; -1 = long the perp, collect from shorts).
  - per-period collected fraction = rate * dominant_sign (negative when funding
    flipped against the committed side).
  - gross annualized % = mean(collected) * settlements_per_year * 100, where the
    cadence is derived from the median gap between settlements (handles 1h/4h/8h
    funding) — uses the MEAN rate so it is robust to sampling.
  - fee drag: one delta-neutral round trip over the window — 2 legs (perp + hedge)
    entered and exited = 4 taker fills — amortized across the window span.
  - net annualized % = gross − fee drag.

Caveats: stored rates are polled snapshots, not settled payments; a real harvest
must also survive the hedge's basis/borrow and flips in funding direction
(see `favorable_pct`). Short windows extrapolate poorly — filter on `span_days`.
Treat the ranking as a screen, not a PnL guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import mean, median
from typing import Optional

_8H = 8 * 3600
_DEFAULT_PPY = 3 * 365   # 8h funding cadence fallback


@dataclass
class HarvestResult:
    exchange: str
    symbol: str
    n_periods: int              # real funding settlements (deduped), not poll count
    span_days: float
    settlements_per_year: int   # derived funding cadence (≈1095 for 8h)
    mean_rate_bps: float        # signed, per settlement
    mean_abs_rate_bps: float
    dominant_sign: int          # +1 = short perp collects, -1 = long perp collects
    favorable_pct: float        # % of settlements funding stayed on the committed side
    gross_annual_pct: float     # annualized funding collected (net of adverse periods)
    fee_drag_annual_pct: float
    net_annual_pct: float

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def _settlements(rows: list[dict]) -> list[tuple[float, float]]:
    """Collapse polled samples to one rate per actual funding settlement. Prefer
    next_funding_time as the settlement key (the last poll before it wins — closest
    to the realized rate); fall back to 8h time-buckets when it is absent. Returns
    sorted [(settlement_ts, rate)]."""
    by_settle: dict[int, tuple[float, float]] = {}
    for r in rows:
        rate = float(r["rate"])
        nft = r.get("next_funding_time")
        if nft:
            key, settle_ts = int(nft), float(nft)
        else:
            key = int(float(r["ts"]) // _8H)
            settle_ts = float(key * _8H)
        by_settle[key] = (settle_ts, rate)
    return sorted(by_settle.values())


def _periods_per_year(ts: list[float]) -> int:
    """Derive funding cadence from the median gap between settlements, clamped to a
    sane range (hourly … daily). Falls back to 8h with too few points."""
    if len(ts) < 3:
        return _DEFAULT_PPY
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1) if ts[i + 1] > ts[i]]
    if not gaps:
        return _DEFAULT_PPY
    med = median(gaps)
    ppy = 365 * 86400 / med
    return int(max(365, min(365 * 24, ppy)))


def analyze(
    exchange: str, symbol: str, rows: list[dict], fee_bps_per_leg: float = 4.0,
) -> Optional[HarvestResult]:
    """Compute harvest metrics from funding rows, or None if fewer than 2 settlements."""
    settles = _settlements(rows)
    if len(settles) < 2:
        return None
    ts = [t for t, _ in settles]
    rates = [r for _, r in settles]
    n = len(rates)
    span_days = max((ts[-1] - ts[0]) / 86400.0, 1e-9)
    ppy = _periods_per_year(ts)

    dom = 1 if sum(rates) >= 0 else -1
    collected = [r * dom for r in rates]
    gross_annual = mean(collected) * ppy * 100.0
    favorable = sum(1 for c in collected if c > 0) / n * 100.0

    # One delta-neutral round trip over the window: 2 legs × (enter+exit) taker fills.
    total_fee_frac = (fee_bps_per_leg / 10_000.0) * 4
    fee_drag_annual = total_fee_frac / span_days * 365.0 * 100.0

    return HarvestResult(
        exchange=exchange, symbol=symbol, n_periods=n, span_days=span_days,
        settlements_per_year=ppy,
        mean_rate_bps=mean(rates) * 10_000.0,
        mean_abs_rate_bps=mean(abs(r) for r in rates) * 10_000.0,
        dominant_sign=dom, favorable_pct=favorable,
        gross_annual_pct=gross_annual, fee_drag_annual_pct=fee_drag_annual,
        net_annual_pct=gross_annual - fee_drag_annual,
    )


def rank(
    rows_by_symbol: dict[str, list[dict]], exchange: str,
    fee_bps_per_leg: float = 4.0, min_periods: int = 10, top_n: int = 50,
    min_span_days: float = 5.0, min_favorable_pct: float = 0.0,
) -> list[HarvestResult]:
    """Analyze every symbol and return the top_n by net annualized %, dropping
    short-history noise (too few settlements / too short a window) and, optionally,
    symbols whose funding flipped sides too often (low favorable_pct)."""
    out: list[HarvestResult] = []
    for symbol, rows in rows_by_symbol.items():
        res = analyze(exchange, symbol, rows, fee_bps_per_leg)
        if (res and res.n_periods >= min_periods
                and res.span_days >= min_span_days
                and res.favorable_pct >= min_favorable_pct):
            out.append(res)
    out.sort(key=lambda r: r.net_annual_pct, reverse=True)
    return out[:top_n]


class FundingHarvestAnalyzer:
    """Storage-backed scanner over the funding_rates table."""

    def __init__(self, storage):
        self._db = storage

    async def scan(
        self, exchange: str = "binance", days: int = 30,
        fee_bps_per_leg: float = 4.0, min_periods: int = 10, top_n: int = 50,
        min_span_days: float = 7.0, min_favorable_pct: float = 0.0,
    ) -> list[dict]:
        import time
        start_ts = time.time() - days * 86400
        rows = await self._db.get_funding_rates(
            exchange=exchange, start_ts=start_ts, limit=2_000_000,
        )
        by_symbol: dict[str, list[dict]] = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)
        return [r.to_dict() for r in rank(
            by_symbol, exchange, fee_bps_per_leg, min_periods, top_n,
            min_span_days=min_span_days, min_favorable_pct=min_favorable_pct,
        )]
