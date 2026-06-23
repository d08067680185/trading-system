"""
Volatility-targeted position sizer.

Position = (target_vol_pct × capital) / realized_vol

target_vol_pct: fraction of capital to risk per unit of daily vol (e.g. 0.01 = 1%)
realized_vol:   annualized volatility estimated from recent price returns

Also supports Kelly fraction sizing as an alternative method.
"""
from __future__ import annotations
import math
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger("PositionSizer")

TRADING_DAYS = 252


class PositionSizer:
    def __init__(
        self,
        target_vol_pct: float = 0.01,   # 1% of capital per 1-sigma daily move
        lookback: int = 20,              # price observations for vol estimation
        min_size_usdt: float = 21.0,    # > Binance futures min_notional (20) w/ margin
        max_size_usdt: float = 500.0,
        min_vol_floor: float = 0.005,    # 0.5% annualized floor (avoid ÷0)
    ):
        self.target_vol_pct = target_vol_pct
        self.lookback       = lookback
        self.min_size_usdt  = min_size_usdt
        self.max_size_usdt  = max_size_usdt
        self.min_vol_floor  = min_vol_floor

        self._prices: dict[str, deque] = {}   # symbol → recent prices
        self._vols:   dict[str, float] = {}   # symbol → cached annualized vol

    # ── Public API ────────────────────────────────────────────────────────────

    def update_price(self, symbol: str, price: float) -> None:
        """Call on every ticker update to keep vol estimate fresh."""
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=self.lookback + 1)
        self._prices[symbol].append(price)
        if len(self._prices[symbol]) >= 2:
            self._vols[symbol] = self._calc_vol(symbol)

    def get_size_usdt(
        self,
        symbol: str,
        capital_usdt: float,
        risk_override: Optional[float] = None,
    ) -> float:
        """
        Return position size in USDT using volatility targeting.

        risk_override: override target_vol_pct for this call (e.g. reduce in HIGH regime)
        """
        vol = self.get_vol(symbol)
        if vol <= 0:
            # No vol estimate yet → fall back to a conservative fixed fraction
            return min(self.max_size_usdt, max(self.min_size_usdt, capital_usdt * 0.005))

        target = risk_override if risk_override is not None else self.target_vol_pct
        # Convert annualized vol to daily
        daily_vol = vol / math.sqrt(TRADING_DAYS)
        size = (target * capital_usdt) / daily_vol if daily_vol > 0 else 0.0
        return float(max(self.min_size_usdt, min(self.max_size_usdt, size)))

    def get_kelly_size_usdt(
        self,
        capital_usdt: float,
        win_rate: float,          # fraction of winning trades
        avg_win_usdt: float,
        avg_loss_usdt: float,
        kelly_fraction: float = 0.25,  # use 1/4 Kelly for safety
    ) -> float:
        """
        Kelly Criterion position sizing.
        f* = (p/|loss| - q/win) where p=win_rate, q=1-p
        Scaled by kelly_fraction to reduce variance.
        """
        if avg_loss_usdt <= 0 or avg_win_usdt <= 0:
            return self.min_size_usdt
        p = max(0.01, min(0.99, win_rate))
        q = 1.0 - p
        b = avg_win_usdt / avg_loss_usdt
        kelly_f = (p * b - q) / b if b > 0 else 0.0
        kelly_f = max(0.0, kelly_f) * kelly_fraction
        size = kelly_f * capital_usdt
        return float(max(self.min_size_usdt, min(self.max_size_usdt, size)))

    def get_vol(self, symbol: str) -> float:
        """Return latest annualized vol estimate (0 if not enough data)."""
        return self._vols.get(symbol, 0.0)

    def get_vol_regime_multiplier(self, symbol: str) -> float:
        """
        Return a size multiplier based on current vol regime.
        HIGH vol → smaller size; LOW vol → larger (up to 1.5×).
        """
        vol = self.get_vol(symbol)
        if vol <= 0:
            return 1.0
        # Typical crypto annualized vol ~60-80%. Use 60% as baseline.
        baseline = 0.60
        ratio = baseline / vol
        return float(max(0.25, min(2.0, ratio)))

    def status(self) -> dict:
        return {
            "target_vol_pct": self.target_vol_pct,
            "lookback": self.lookback,
            "vols": {k: round(v, 4) for k, v in self._vols.items()},
            "data_points": {k: len(v) for k, v in self._prices.items()},
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _calc_vol(self, symbol: str) -> float:
        """Annualized realized vol from log returns."""
        prices = list(self._prices[symbol])
        if len(prices) < 2:
            return self.min_vol_floor
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0 and prices[i] > 0
        ]
        if not returns:
            return self.min_vol_floor
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)
        daily_vol = math.sqrt(variance)
        ann_vol = daily_vol * math.sqrt(TRADING_DAYS)
        return max(self.min_vol_floor, ann_vol)
