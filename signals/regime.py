"""
Market regime detector based on realized volatility.

Classifies current market state into 4 regimes:
  LOW      → vol < 25th percentile → enlarge positions, tighten thresholds
  NORMAL   → 25th–75th percentile  → standard parameters
  HIGH     → 75th–90th percentile  → shrink positions, widen thresholds
  EXTREME  → vol > 90th percentile → reduce or pause trading

Strategies query regime to scale their parameters dynamically.
"""
from __future__ import annotations
import math
import time
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("RegimeDetector")

# Regime constants
LOW     = "low"
NORMAL  = "normal"
HIGH    = "high"
EXTREME = "extreme"

_REGIMES = (LOW, NORMAL, HIGH, EXTREME)
# Ordinal rank — higher = more volatile / more conservative
_ORDER = {LOW: 0, NORMAL: 1, HIGH: 2, EXTREME: 3}
# Percentile boundary above which regime index i escalates to i+1
#   LOW→NORMAL @25, NORMAL→HIGH @75, HIGH→EXTREME @90
_UP_BOUNDS = (25.0, 75.0, 90.0)


@dataclass
class RegimeSnapshot:
    symbol: str
    regime: str
    realized_vol_ann: float    # annualized
    vol_percentile: float      # 0–100, position within history
    pos_size_mult: float       # suggested position multiplier
    threshold_mult: float      # suggested spread/profit threshold multiplier
    data_points: int


# ── Regime parameter maps ────────────────────────────────────────────────────

_POS_MULT: dict[str, float] = {
    LOW:     1.5,   # low vol → can take bigger positions
    NORMAL:  1.0,
    HIGH:    0.6,
    EXTREME: 0.2,
}

_THRESHOLD_MULT: dict[str, float] = {
    LOW:     0.8,   # tighter spreads still profitable
    NORMAL:  1.0,
    HIGH:    1.5,   # need wider spreads to justify risk
    EXTREME: 2.5,
}


class RegimeDetector:
    def __init__(
        self,
        short_window: int = 20,    # fast vol estimate (num price observations)
        long_window: int = 200,    # history for percentile ranking
        min_data: int = 10,        # minimum data points before classifying
        hysteresis_pct: float = 7.0,   # percentile buffer past a boundary before switching
        min_dwell_s: float = 30.0,     # min seconds a regime must hold before de-escalating
    ):
        self._short = short_window
        self._long  = long_window
        self._min   = min_data
        self._hysteresis = hysteresis_pct
        self._min_dwell  = min_dwell_s

        # symbol → recent prices (short window for vol)
        self._prices: dict[str, deque] = {}
        # symbol → vol history (long window for percentile)
        self._vol_history: dict[str, deque] = {}
        # symbol → latest snapshot
        self._snapshots: dict[str, RegimeSnapshot] = {}
        # symbol → monotonic time of last accepted regime change (debounce)
        self._last_change: dict[str, float] = {}

    def update(self, symbol: str, price: float) -> Optional[RegimeSnapshot]:
        """Update with a new price tick. Returns updated snapshot if regime changed."""
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self._short + 1)
            self._vol_history[symbol] = deque(maxlen=self._long)

        self._prices[symbol].append(price)
        if len(self._prices[symbol]) < self._min:
            return None

        vol = self._calc_vol(symbol)
        if vol <= 0:
            return None

        self._vol_history[symbol].append(vol)
        pct = self._percentile(symbol, vol)

        prev = self._snapshots.get(symbol)
        prev_regime = prev.regime if prev else None
        regime = self._classify(prev_regime, pct)

        # Debounce: escalation (de-risking) takes effect immediately, but
        # de-escalation (relaxing) must wait out the dwell window. This stops
        # noisy tick-level vol estimates from flapping the regime — and the
        # strategy parameters that key off it — many times per second.
        now = time.monotonic()
        if prev_regime is None:
            self._last_change[symbol] = now
        elif regime != prev_regime:
            escalating = _ORDER[regime] > _ORDER[prev_regime]
            if escalating or (now - self._last_change.get(symbol, 0.0)) >= self._min_dwell:
                self._last_change[symbol] = now
            else:
                regime = prev_regime  # too soon to relax; hold current regime

        snap = RegimeSnapshot(
            symbol=symbol,
            regime=regime,
            realized_vol_ann=round(vol, 4),
            vol_percentile=round(pct, 1),
            pos_size_mult=_POS_MULT[regime],
            threshold_mult=_THRESHOLD_MULT[regime],
            data_points=len(self._prices[symbol]),
        )
        if prev_regime != regime:
            logger.info(
                f"Regime change [{symbol}]: {prev_regime or '—'} → {regime} "
                f"(vol={vol:.1%}, pct={pct:.0f})"
            )
        self._snapshots[symbol] = snap
        return snap

    def get(self, symbol: str) -> Optional[RegimeSnapshot]:
        return self._snapshots.get(symbol)

    def get_regime(self, symbol: str) -> str:
        snap = self._snapshots.get(symbol)
        return snap.regime if snap else NORMAL

    def pos_size_mult(self, symbol: str) -> float:
        snap = self._snapshots.get(symbol)
        return snap.pos_size_mult if snap else 1.0

    def threshold_mult(self, symbol: str) -> float:
        snap = self._snapshots.get(symbol)
        return snap.threshold_mult if snap else 1.0

    def all_snapshots(self) -> dict[str, dict]:
        return {
            sym: {
                "regime": s.regime,
                "realized_vol_ann": s.realized_vol_ann,
                "vol_percentile": s.vol_percentile,
                "pos_size_mult": s.pos_size_mult,
                "threshold_mult": s.threshold_mult,
            }
            for sym, s in self._snapshots.items()
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    def _calc_vol(self, symbol: str) -> float:
        prices = list(self._prices[symbol])
        if len(prices) < 2:
            return 0.0
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0 and prices[i] > 0
        ]
        if not returns:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
        # Annualize: multiply by sqrt of periods per year
        # For tick data we approximate using the data frequency
        # (short_window ticks per "period" * 252 "periods" per year)
        ann_factor = math.sqrt(252 * 24 * 60)   # assume ~minute-level ticks
        return math.sqrt(var) * ann_factor

    def _percentile(self, symbol: str, current_vol: float) -> float:
        hist = list(self._vol_history[symbol])
        if len(hist) < 2:
            return 50.0
        below = sum(1 for v in hist if v <= current_vol)
        return below / len(hist) * 100.0

    def _classify(self, prev_regime: Optional[str], pct: float) -> str:
        """Classify with hysteresis around the previous regime.

        From a cold start (no prev_regime) use the raw thresholds. Otherwise
        only move up/down a band once the percentile clears the boundary by
        ``hysteresis_pct``, so values hovering on a boundary don't oscillate.
        """
        if prev_regime is None:
            return self._classify_raw(pct)
        idx = _ORDER[prev_regime]
        m = self._hysteresis
        # escalate while clearly above the next boundary
        while idx < 3 and pct >= _UP_BOUNDS[idx] + m:
            idx += 1
        # de-escalate while clearly below the lower boundary
        while idx > 0 and pct < _UP_BOUNDS[idx - 1] - m:
            idx -= 1
        return _REGIMES[idx]

    @staticmethod
    def _classify_raw(percentile: float) -> str:
        if percentile < 25:
            return LOW
        if percentile < 75:
            return NORMAL
        if percentile < 90:
            return HIGH
        return EXTREME
