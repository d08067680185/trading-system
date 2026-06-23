"""
Latency Monitor.

Tracks two categories of latency:

1. REST API Latency:
   Decorator @track_latency wraps connector REST calls and records
   request→response time per exchange and endpoint type.

2. WebSocket Message Latency:
   When exchange sends a message with a server-side timestamp,
   we compute (local_received_ts - server_ts) to measure propagation delay.

Metrics exposed:
  - p50, p95, p99, max per exchange + category
  - Alert when p99 > configured threshold
  - Rolling 5-minute window, auto-resets old data
"""
from __future__ import annotations
import asyncio
import functools
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("LatencyMonitor")

_PERCENTILES = [50, 95, 99]
_WINDOW_S    = 300   # 5-minute rolling window


@dataclass
class LatencyStats:
    exchange: str
    category: str       # "rest" | "ws"
    n_samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    alert: bool
    alert_threshold_ms: float


class LatencyMonitor:
    def __init__(
        self,
        rest_alert_p99_ms: float = 500.0,   # warn if REST p99 > 500ms
        ws_alert_p99_ms:   float = 200.0,   # warn if WS lag p99 > 200ms
        window_s: int = _WINDOW_S,
    ):
        self._rest_threshold = rest_alert_p99_ms
        self._ws_threshold   = ws_alert_p99_ms
        self._window         = window_s

        # (exchange, category) → deque of (ts, latency_ms) pairs
        self._samples: dict[tuple[str, str], deque] = {}
        self._notifier = None

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    # ── REST tracking ─────────────────────────────────────────────────────────

    def record_rest(self, exchange: str, latency_ms: float) -> None:
        self._add("rest", exchange, latency_ms)

    def wrap_rest(self, exchange: str):
        """Decorator factory for async REST methods."""
        def decorator(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                t0 = time.monotonic()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    self.record_rest(exchange, (time.monotonic() - t0) * 1000)
            return wrapper
        return decorator

    # ── WS tracking ───────────────────────────────────────────────────────────

    def record_ws_lag(self, exchange: str, server_ts_ms: int) -> None:
        """Call with server-provided timestamp (milliseconds)."""
        if server_ts_ms <= 0:
            return
        lag_ms = time.time() * 1000 - server_ts_ms
        if 0 < lag_ms < 60_000:   # sanity: ignore negative or >60s lags
            self._add("ws", exchange, lag_ms)

    # ── Statistics ────────────────────────────────────────────────────────────

    def get_stats(self, exchange: Optional[str] = None) -> list[LatencyStats]:
        results = []
        now = time.time()
        cutoff = now - self._window

        for (exch, cat), dq in self._samples.items():
            if exchange and exch != exchange:
                continue
            # Filter to window
            samples = [ms for ts, ms in dq if ts > cutoff]
            if not samples:
                continue
            s = sorted(samples)
            n = len(s)
            p50 = s[int(n * 0.50)]
            p95 = s[min(n - 1, int(n * 0.95))]
            p99 = s[min(n - 1, int(n * 0.99))]
            mx  = s[-1]
            mn  = sum(s) / n
            thresh = self._rest_threshold if cat == "rest" else self._ws_threshold
            alert = p99 > thresh
            results.append(LatencyStats(
                exchange=exch, category=cat, n_samples=n,
                p50_ms=round(p50, 1), p95_ms=round(p95, 1),
                p99_ms=round(p99, 1), max_ms=round(mx, 1),
                mean_ms=round(mn, 1),
                alert=alert, alert_threshold_ms=thresh,
            ))
        return sorted(results, key=lambda s: (s.exchange, s.category))

    def get_stats_dict(self) -> list[dict]:
        return [
            {
                "exchange": s.exchange, "category": s.category,
                "n_samples": s.n_samples,
                "p50_ms": s.p50_ms, "p95_ms": s.p95_ms,
                "p99_ms": s.p99_ms, "max_ms": s.max_ms,
                "mean_ms": s.mean_ms,
                "alert": s.alert, "alert_threshold_ms": s.alert_threshold_ms,
            }
            for s in self.get_stats()
        ]

    def has_alerts(self) -> bool:
        return any(s.alert for s in self.get_stats())

    # ── Internal ─────────────────────────────────────────────────────────────

    def _add(self, category: str, exchange: str, latency_ms: float) -> None:
        key = (exchange, category)
        if key not in self._samples:
            self._samples[key] = deque(maxlen=1000)
        self._samples[key].append((time.time(), latency_ms))


# ── Global singleton (attached to connectors at startup) ─────────────────────

_monitor: Optional[LatencyMonitor] = None


def get_monitor() -> Optional[LatencyMonitor]:
    return _monitor


def init_monitor(**kwargs) -> LatencyMonitor:
    global _monitor
    _monitor = LatencyMonitor(**kwargs)
    return _monitor
