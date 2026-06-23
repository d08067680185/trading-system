"""
Portfolio-level risk analytics.

Tracks per-strategy daily PnL series and computes:
  - Historical VaR / CVaR (95%, 99%)
  - Strategy correlation matrix
  - Portfolio beta to BTC/ETH
  - Concentration risk (Herfindahl index)
"""
from __future__ import annotations
import math
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("PortfolioRisk")

_SQRT252 = math.sqrt(252)


class PortfolioRisk:
    def __init__(
        self,
        lookback_days: int = 60,
        var_confidence: float = 0.95,
        max_strategy_correlation: float = 0.80,   # warn if correlation exceeds this
    ):
        self.lookback_days = lookback_days
        self.var_confidence = var_confidence
        self.max_strategy_correlation = max_strategy_correlation

        # strategy_id → deque of daily PnL snapshots
        self._daily_pnl: dict[str, deque] = {}
        # strategy_id → cumulative intra-day PnL (reset at UTC midnight)
        self._intraday_pnl: dict[str, float] = {}
        self._last_reset_day: int = self._today()

        # Price series for beta calculation: symbol → deque of prices
        self._ref_prices: dict[str, deque] = {}

    # ── Update hooks ──────────────────────────────────────────────────────────

    def record_fill_pnl(self, strategy_id: str, pnl_delta: float) -> None:
        """Call after each order fill with the realized PnL of that fill."""
        self._maybe_reset_day()
        self._intraday_pnl[strategy_id] = (
            self._intraday_pnl.get(strategy_id, 0.0) + pnl_delta
        )

    def update_ref_price(self, symbol: str, price: float) -> None:
        """Update reference asset price (BTC-USDT, ETH-USDT) for beta calc."""
        if symbol not in self._ref_prices:
            self._ref_prices[symbol] = deque(maxlen=self.lookback_days + 1)
        self._ref_prices[symbol].append(price)

    def end_of_day_snapshot(self) -> None:
        """Call once per day (e.g. from equity snapshot loop) to commit intraday PnL."""
        for sid, pnl in self._intraday_pnl.items():
            if sid not in self._daily_pnl:
                self._daily_pnl[sid] = deque(maxlen=self.lookback_days)
            self._daily_pnl[sid].append(pnl)
        self._intraday_pnl.clear()

    # ── VaR / CVaR ────────────────────────────────────────────────────────────

    def portfolio_var(self, confidence: Optional[float] = None) -> float:
        """Historical VaR of the total portfolio (sum of strategy PnLs), in USDT.
        Negative number = max expected loss at given confidence level."""
        conf = confidence or self.var_confidence
        combined = self._combined_daily_pnl()
        if len(combined) < 5:
            return 0.0
        sorted_pnl = sorted(combined)
        idx = int((1 - conf) * len(sorted_pnl))
        return float(sorted_pnl[max(0, idx)])

    def portfolio_cvar(self, confidence: Optional[float] = None) -> float:
        """Conditional VaR (Expected Shortfall) — average of losses beyond VaR."""
        conf = confidence or self.var_confidence
        combined = self._combined_daily_pnl()
        if len(combined) < 5:
            return 0.0
        sorted_pnl = sorted(combined)
        idx = int((1 - conf) * len(sorted_pnl))
        tail = sorted_pnl[: max(1, idx)]
        return float(sum(tail) / len(tail)) if tail else 0.0

    def strategy_var(self, strategy_id: str, confidence: Optional[float] = None) -> float:
        conf = confidence or self.var_confidence
        series = list(self._daily_pnl.get(strategy_id, []))
        if len(series) < 5:
            return 0.0
        sorted_pnl = sorted(series)
        idx = int((1 - conf) * len(sorted_pnl))
        return float(sorted_pnl[max(0, idx)])

    # ── Correlation ───────────────────────────────────────────────────────────

    def correlation_matrix(self) -> dict[str, dict[str, float]]:
        """Pairwise Pearson correlation of strategy daily PnL series."""
        sids = [s for s, d in self._daily_pnl.items() if len(d) >= 5]
        if len(sids) < 2:
            return {}
        matrix: dict[str, dict[str, float]] = {}
        for i, s1 in enumerate(sids):
            matrix[s1] = {}
            for s2 in sids:
                if s1 == s2:
                    matrix[s1][s2] = 1.0
                else:
                    matrix[s1][s2] = round(self._pearson(s1, s2), 3)
        return matrix

    def high_correlation_pairs(self) -> list[tuple[str, str, float]]:
        """Return strategy pairs with |correlation| > max_strategy_correlation."""
        matrix = self.correlation_matrix()
        pairs = []
        seen = set()
        for s1, row in matrix.items():
            for s2, corr in row.items():
                if s1 != s2 and (s2, s1) not in seen:
                    if abs(corr) > self.max_strategy_correlation:
                        pairs.append((s1, s2, corr))
                        seen.add((s1, s2))
        return sorted(pairs, key=lambda x: -abs(x[2]))

    # ── Beta ─────────────────────────────────────────────────────────────────

    def portfolio_beta(self, ref_symbol: str = "BTC-USDT") -> float:
        """Estimate portfolio beta to a reference asset price series."""
        ref = list(self._ref_prices.get(ref_symbol, []))
        if len(ref) < 5:
            return 0.0
        combined = self._combined_daily_pnl()
        n = min(len(ref) - 1, len(combined))
        if n < 4:
            return 0.0
        ref_returns = [
            (ref[i] - ref[i - 1]) / ref[i - 1]
            for i in range(len(ref) - n, len(ref))
            if ref[i - 1] != 0
        ]
        port_pnl = list(combined)[-n:]
        if len(ref_returns) != len(port_pnl) or len(ref_returns) < 4:
            return 0.0
        # beta = cov(port, ref) / var(ref)
        cov = _cov(port_pnl, ref_returns)
        var_ref = _var(ref_returns)
        return float(cov / var_ref) if var_ref != 0 else 0.0

    # ── Concentration ─────────────────────────────────────────────────────────

    def herfindahl_index(self, strategy_capital: dict[str, float]) -> float:
        """Herfindahl–Hirschman Index: 0=diversified, 1=fully concentrated."""
        total = sum(strategy_capital.values())
        if total <= 0:
            return 0.0
        shares = [v / total for v in strategy_capital.values()]
        return float(sum(s ** 2 for s in shares))

    # ── Portfolio Sharpe ─────────────────────────────────────────────────────

    def portfolio_sharpe(self) -> float:
        combined = self._combined_daily_pnl()
        if len(combined) < 5:
            return 0.0
        mu = sum(combined) / len(combined)
        std = math.sqrt(_var(list(combined))) or 1e-8
        return float((mu / std) * _SQRT252)

    # ── Full status dict ─────────────────────────────────────────────────────

    def status(self) -> dict:
        corr = self.correlation_matrix()
        high_corr = self.high_correlation_pairs()
        strategies = list(self._daily_pnl.keys())
        per_strategy = {
            sid: {
                "var_95": round(self.strategy_var(sid, 0.95), 4),
                "var_99": round(self.strategy_var(sid, 0.99), 4),
                "days_of_data": len(self._daily_pnl[sid]),
                "intraday_pnl": round(self._intraday_pnl.get(sid, 0.0), 4),
            }
            for sid in strategies
        }
        return {
            "portfolio_var_95": round(self.portfolio_var(0.95), 4),
            "portfolio_var_99": round(self.portfolio_var(0.99), 4),
            "portfolio_cvar_95": round(self.portfolio_cvar(0.95), 4),
            "portfolio_sharpe": round(self.portfolio_sharpe(), 3),
            "beta_btc": round(self.portfolio_beta("BTC-USDT"), 3),
            "beta_eth": round(self.portfolio_beta("ETH-USDT"), 3),
            "correlation_matrix": corr,
            "high_correlation_pairs": [
                {"s1": p[0], "s2": p[1], "corr": round(p[2], 3)} for p in high_corr
            ],
            "strategy_risk": per_strategy,
            "lookback_days": self.lookback_days,
        }

    # ── Internals ────────────────────────────────────────────────────────────

    def _combined_daily_pnl(self) -> list[float]:
        """Sum strategy PnLs per day (aligned by index from end)."""
        if not self._daily_pnl:
            return []
        max_len = max(len(d) for d in self._daily_pnl.values())
        if max_len == 0:
            return []
        combined = []
        series = [list(d) for d in self._daily_pnl.values()]
        for i in range(max_len):
            day_total = 0.0
            for s in series:
                offset = len(s) - max_len + i
                if 0 <= offset < len(s):
                    day_total += s[offset]
            combined.append(day_total)
        return combined

    def _pearson(self, s1: str, s2: str) -> float:
        a = list(self._daily_pnl[s1])
        b = list(self._daily_pnl[s2])
        n = min(len(a), len(b))
        if n < 3:
            return 0.0
        a, b = a[-n:], b[-n:]
        ma = sum(a) / n
        mb = sum(b) / n
        num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        da  = math.sqrt(sum((x - ma) ** 2 for x in a))
        db  = math.sqrt(sum((x - mb) ** 2 for x in b))
        return num / (da * db) if da * db > 0 else 0.0

    def _maybe_reset_day(self) -> None:
        today = self._today()
        if today != self._last_reset_day:
            self.end_of_day_snapshot()
            self._last_reset_day = today

    @staticmethod
    def _today() -> int:
        return int(time.time() // 86400)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _var(series: list[float]) -> float:
    if len(series) < 2:
        return 0.0
    mean = sum(series) / len(series)
    return sum((x - mean) ** 2 for x in series) / (len(series) - 1)


def _cov(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)
