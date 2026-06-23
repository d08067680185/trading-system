"""
Funding Rate Predictor.

Estimates the probability that the NEXT settlement rate will exceed a threshold,
using historical rate distribution and momentum features.

Key insight: funding rates are highly auto-correlated and mean-reverting.
A high rate now doesn't mean the next settlement will also be high — this
filter prevents entering a carry trade just before the rate reverts.

Features used:
  1. Current rate vs historical percentile
  2. Rate momentum (trend of last N observations)
  3. Time-to-settlement discount (late entries risk reversal)
  4. Annualised expected yield (rate × 3 × 365)
"""
from __future__ import annotations
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("FundingPredictor")


@dataclass
class FundingForecast:
    exchange: str
    symbol: str
    current_rate: float        # current 8h funding rate
    predicted_rate: float      # estimated next settlement rate
    confidence: float          # 0–1 probability estimate
    momentum: float            # positive = accelerating, negative = decelerating
    percentile: float          # current rate vs history (0–100)
    ann_yield_pct: float       # annualised expected yield %
    time_to_settle_h: float    # hours until next settlement
    recommendation: str        # "enter" | "skip" | "exit"
    reason: str


class FundingRatePredictor:
    """
    Maintains a rolling history of observed funding rates and predicts
    whether the next settlement will exceed a given threshold.
    """

    def __init__(
        self,
        history_len: int = 24 * 7,  # 1 week of 8h settlements = 21 observations
        min_history: int = 6,        # minimum observations before making predictions
        momentum_window: int = 3,    # last N rates for momentum calculation
        decay_factor: float = 0.85,  # weight recency: recent obs count more
    ):
        self._history_len     = history_len
        self._min_history     = min_history
        self._momentum_window = momentum_window
        self._decay           = decay_factor

        # key: "exchange:symbol" → deque of (ts, rate) pairs
        self._history: dict[str, deque] = {}

    def record_rate(self, exchange: str, symbol: str, rate: float,
                    ts: Optional[float] = None) -> None:
        """Record a new observed funding rate."""
        key = f"{exchange}:{symbol}"
        if key not in self._history:
            self._history[key] = deque(maxlen=self._history_len)
        self._history[key].append((ts or time.time(), rate))

    def forecast(
        self,
        exchange: str,
        symbol: str,
        current_rate: float,
        next_funding_time: Optional[float] = None,
        min_threshold: float = 0.0005,   # 0.05% per 8h = ~22% annualised
    ) -> FundingForecast:
        """
        Predict next settlement rate and whether it's worth entering now.

        Returns FundingForecast with recommendation "enter" | "skip" | "exit".
        """
        key = f"{exchange}:{symbol}"
        history = list(self._history.get(key, []))
        rates   = [r for _, r in history]

        # Time to settlement
        now = time.time()
        if next_funding_time and next_funding_time > now:
            time_to_h = (next_funding_time - now) / 3600.0
        else:
            time_to_h = 4.0  # assume mid-period if unknown

        # Annualised yield
        ann_yield = current_rate * 3 * 365 * 100

        # Not enough history → simple threshold check
        if len(rates) < self._min_history:
            rec = "enter" if current_rate >= min_threshold else "skip"
            return FundingForecast(
                exchange=exchange, symbol=symbol,
                current_rate=current_rate,
                predicted_rate=current_rate,
                confidence=0.5 if rec == "enter" else 0.2,
                momentum=0.0, percentile=50.0,
                ann_yield_pct=round(ann_yield, 2),
                time_to_settle_h=round(time_to_h, 2),
                recommendation=rec,
                reason="Insufficient history — using current rate only",
            )

        # Compute percentile
        pct = sum(1 for r in rates if r <= current_rate) / len(rates) * 100

        # Momentum: weighted slope of last N observations
        recent = rates[-self._momentum_window:]
        if len(recent) >= 2:
            n = len(recent)
            weights = [self._decay ** (n - 1 - i) for i in range(n)]
            w_sum   = sum(weights)
            w_mean  = sum(w * r for w, r in zip(weights, recent)) / w_sum
            # Simple slope: difference between first and last weighted
            momentum = (recent[-1] - recent[0]) / (n - 1)
        else:
            momentum = 0.0

        # Predicted rate: mean-revert toward rolling mean
        hist_mean = sum(rates) / len(rates)
        # Weight: current rate + history mean, adjusted for momentum
        mean_reversion_speed = 0.35   # how fast it reverts per period
        predicted = current_rate + momentum - mean_reversion_speed * (current_rate - hist_mean)

        # Confidence: higher if current rate is far above mean and trending up
        above_mean_sigma = 0.0
        if len(rates) >= 4:
            std = (sum((r - hist_mean) ** 2 for r in rates) / (len(rates) - 1)) ** 0.5
            above_mean_sigma = (current_rate - hist_mean) / (std + 1e-10)

        confidence = _sigmoid(above_mean_sigma * 0.5 + (momentum / (abs(hist_mean) + 1e-10)) * 2)
        confidence = max(0.05, min(0.95, confidence))

        # Time discount: late-cycle entries risk rate reversal before settlement
        time_discount = min(1.0, time_to_h / 4.0)  # < 4h remaining → reduced confidence

        # Recommendation
        will_yield = predicted >= min_threshold
        high_confidence = confidence * time_discount >= 0.60

        if current_rate >= min_threshold and will_yield and high_confidence:
            recommendation = "enter"
            reason = (f"Rate {current_rate:.4%} ({pct:.0f}th pct), "
                      f"predicted {predicted:.4%}, confidence {confidence:.0%}")
        elif current_rate < min_threshold * 0.5 or (not will_yield and confidence > 0.65):
            recommendation = "exit"
            reason = (f"Rate declining — predicted {predicted:.4%} < threshold {min_threshold:.4%}")
        else:
            recommendation = "skip"
            reason = (f"Marginal: rate {current_rate:.4%} but confidence {confidence:.0%} "
                      f"or {time_to_h:.1f}h to settlement is late")

        return FundingForecast(
            exchange=exchange, symbol=symbol,
            current_rate=current_rate,
            predicted_rate=round(predicted, 6),
            confidence=round(confidence, 3),
            momentum=round(momentum, 6),
            percentile=round(pct, 1),
            ann_yield_pct=round(ann_yield, 2),
            time_to_settle_h=round(time_to_h, 2),
            recommendation=recommendation,
            reason=reason,
        )

    def should_enter(
        self,
        exchange: str, symbol: str, current_rate: float,
        min_threshold: float = 0.0005,
        next_funding_time: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Convenience: returns (should_enter, reason)."""
        fc = self.forecast(exchange, symbol, current_rate,
                           next_funding_time, min_threshold)
        return fc.recommendation == "enter", fc.reason

    def should_exit(
        self,
        exchange: str, symbol: str, current_rate: float,
        min_threshold: float = 0.0005,
        next_funding_time: Optional[float] = None,
    ) -> tuple[bool, str]:
        fc = self.forecast(exchange, symbol, current_rate,
                           next_funding_time, min_threshold)
        return fc.recommendation == "exit", fc.reason

    def all_forecasts(self, min_threshold: float = 0.0005) -> list[dict]:
        """Return latest forecast for every tracked symbol."""
        results = []
        for key, hist in self._history.items():
            if not hist:
                continue
            exchange, symbol = key.split(":", 1)
            _, current = hist[-1]
            fc = self.forecast(exchange, symbol, current, min_threshold=min_threshold)
            results.append({
                "exchange": fc.exchange, "symbol": fc.symbol,
                "current_rate": fc.current_rate,
                "predicted_rate": fc.predicted_rate,
                "confidence": fc.confidence,
                "momentum": fc.momentum,
                "percentile": fc.percentile,
                "ann_yield_pct": fc.ann_yield_pct,
                "time_to_settle_h": fc.time_to_settle_h,
                "recommendation": fc.recommendation,
                "reason": fc.reason,
            })
        return results


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0
