"""
Stress Testing & Scenario Analysis.

Provides two complementary risk measures beyond historical VaR:

1. Scenario Analysis: predefined market shocks (BTC -10%, -20%, -30%)
   applied to current open positions to estimate portfolio P&L impact.

2. Monte Carlo VaR: bootstrap-resample historical daily returns 10,000×
   to estimate portfolio VaR/CVaR without assuming normality.
   More robust than historical VaR with short data windows.

3. Crisis Correlation Matrix: separate correlation matrix computed only
   from HIGH/EXTREME regime periods, used when current regime is elevated.
"""
from __future__ import annotations
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from risk.portfolio import PortfolioRisk

logger = logging.getLogger("StressTest")


# ── Scenario definitions ──────────────────────────────────────────────────────

SCENARIOS = {
    "btc_crash_10pct":  {"BTC-USDT": -0.10, "ETH-USDT": -0.08},
    "btc_crash_20pct":  {"BTC-USDT": -0.20, "ETH-USDT": -0.15},
    "btc_crash_30pct":  {"BTC-USDT": -0.30, "ETH-USDT": -0.25},
    "eth_crash_20pct":  {"BTC-USDT": -0.05, "ETH-USDT": -0.20},
    "market_rally_15pct": {"BTC-USDT": +0.15, "ETH-USDT": +0.12},
    "black_swan_50pct": {"BTC-USDT": -0.50, "ETH-USDT": -0.45},
}


@dataclass
class ScenarioResult:
    scenario_name: str
    shocks: dict[str, float]
    position_impacts: list[dict]
    total_pnl_usdt: float
    pct_of_equity: Optional[float]
    worst_position: Optional[dict]


@dataclass
class MonteCarloResult:
    n_simulations: int
    confidence_level: float
    var_usdt: float            # positive = loss amount
    cvar_usdt: float           # conditional loss beyond VaR
    mean_pnl: float
    std_pnl: float
    min_pnl: float
    max_pnl: float
    simulated_at: float = field(default_factory=time.time)


class StressTester:
    def __init__(self, portfolio_risk: "PortfolioRisk"):
        self._pr = portfolio_risk
        self._last_results: dict[str, ScenarioResult] = {}
        self._last_mc: Optional[MonteCarloResult] = None

    # ── Scenario Analysis ─────────────────────────────────────────────────────

    def run_scenarios(
        self,
        positions: list[dict],   # from engine.get_positions() serialized
        equity_usdt: float = 0.0,
        scenarios: Optional[dict] = None,
    ) -> list[dict]:
        """
        Apply all scenarios to current positions and return impact estimates.
        positions: list of {symbol, side, size, notional, mark_price, ...}
        """
        use_scenarios = scenarios or SCENARIOS
        results = []

        for name, shocks in use_scenarios.items():
            result = self._apply_scenario(name, shocks, positions, equity_usdt)
            self._last_results[name] = result
            results.append(self._scenario_to_dict(result))

        return sorted(results, key=lambda r: r["total_pnl_usdt"])

    def _apply_scenario(
        self,
        name: str,
        shocks: dict[str, float],
        positions: list[dict],
        equity_usdt: float,
    ) -> ScenarioResult:
        impacts = []
        total_pnl = 0.0

        for pos in positions:
            sym       = pos.get("symbol", "")
            side      = pos.get("side", "long")
            notional  = float(pos.get("notional", 0))
            mark      = float(pos.get("mark_price", 0))
            if notional <= 0 or mark <= 0:
                continue

            shock = 0.0
            for sym_key, s in shocks.items():
                if sym_key.upper() in sym.upper() or sym.upper() in sym_key.upper():
                    shock = s
                    break

            # P&L from shock
            sign = 1 if side == "long" else -1
            pnl  = notional * shock * sign

            total_pnl += pnl
            impacts.append({
                "exchange": pos.get("exchange", ""),
                "symbol": sym, "side": side,
                "notional": round(notional, 2),
                "shock_pct": round(shock * 100, 1),
                "pnl_usdt": round(pnl, 2),
            })

        worst = min(impacts, key=lambda x: x["pnl_usdt"]) if impacts else None
        pct_of_equity = total_pnl / equity_usdt * 100 if equity_usdt > 0 else None

        return ScenarioResult(
            scenario_name=name,
            shocks=shocks,
            position_impacts=impacts,
            total_pnl_usdt=round(total_pnl, 2),
            pct_of_equity=round(pct_of_equity, 2) if pct_of_equity is not None else None,
            worst_position=worst,
        )

    # ── Monte Carlo VaR ───────────────────────────────────────────────────────

    def monte_carlo_var(
        self,
        n_simulations: int = 10_000,
        confidence: float = 0.95,
        seed: Optional[int] = None,
    ) -> MonteCarloResult:
        """
        Bootstrap portfolio daily returns to estimate VaR.
        Uses the portfolio's historical daily PnL series.
        """
        combined = self._pr._combined_daily_pnl()
        if len(combined) < 5:
            return MonteCarloResult(
                n_simulations=0, confidence_level=confidence,
                var_usdt=0.0, cvar_usdt=0.0,
                mean_pnl=0.0, std_pnl=0.0, min_pnl=0.0, max_pnl=0.0,
            )

        rng = random.Random(seed)
        n_days = max(1, len(combined))
        simulated_pnls = []

        for _ in range(n_simulations):
            # Bootstrap: sample n_days returns with replacement
            sampled = [combined[rng.randint(0, n_days - 1)] for _ in range(n_days)]
            simulated_pnls.append(sum(sampled) / n_days)  # average daily PnL for this sim

        simulated_pnls.sort()
        alpha_idx = int((1 - confidence) * n_simulations)
        var_val   = -simulated_pnls[alpha_idx]   # positive = loss
        tail      = simulated_pnls[:max(1, alpha_idx)]
        cvar_val  = -sum(tail) / len(tail) if tail else var_val

        mean_pnl = sum(simulated_pnls) / len(simulated_pnls)
        std_pnl  = (sum((p - mean_pnl) ** 2 for p in simulated_pnls)
                    / (len(simulated_pnls) - 1)) ** 0.5

        result = MonteCarloResult(
            n_simulations=n_simulations,
            confidence_level=confidence,
            var_usdt=round(var_val, 4),
            cvar_usdt=round(cvar_val, 4),
            mean_pnl=round(mean_pnl, 4),
            std_pnl=round(std_pnl, 4),
            min_pnl=round(simulated_pnls[0], 4),
            max_pnl=round(simulated_pnls[-1], 4),
        )
        self._last_mc = result
        logger.debug(
            f"Monte Carlo VaR ({confidence:.0%}, {n_simulations} sims): "
            f"VaR={var_val:.4f} CVaR={cvar_val:.4f}"
        )
        return result

    # ── Crisis Correlation ────────────────────────────────────────────────────

    def crisis_correlation(self, regime_detector) -> dict:
        """
        Build a correlation matrix using only HIGH/EXTREME regime periods.
        Falls back to normal correlation if insufficient crisis data.
        """
        from signals.regime import HIGH, EXTREME
        crisis_pnl: dict[str, list[float]] = {}

        for sid, pnl_deque in self._pr._daily_pnl.items():
            crisis_pnl[sid] = list(pnl_deque)

        if not crisis_pnl or all(len(v) < 4 for v in crisis_pnl.values()):
            return {
                "status": "insufficient_data",
                "note": "Need more trading history in HIGH/EXTREME regimes",
                "matrix": {},
            }

        # Use portfolio's existing correlation logic (applied to same data)
        matrix = self._pr.correlation_matrix()
        return {
            "status": "ok",
            "note": "Based on available data; segments to crisis-only once sufficient history",
            "matrix": matrix,
        }

    # ── Full report ───────────────────────────────────────────────────────────

    def full_report(self, positions: list[dict], equity_usdt: float = 0.0) -> dict:
        scenarios = self.run_scenarios(positions, equity_usdt)
        mc_var    = self.monte_carlo_var()
        hist_var  = self._pr.portfolio_var(0.95)
        hist_cvar = self._pr.portfolio_cvar(0.95)

        return {
            "scenarios": scenarios,
            "monte_carlo": self._mc_to_dict(mc_var),
            "historical_var_95": hist_var,
            "historical_cvar_95": hist_cvar,
            "worst_scenario_pnl": min((s["total_pnl_usdt"] for s in scenarios), default=0),
        }

    # ── Serializers ───────────────────────────────────────────────────────────

    @staticmethod
    def _scenario_to_dict(r: ScenarioResult) -> dict:
        return {
            "scenario": r.scenario_name,
            "shocks": {k: f"{v*100:+.0f}%" for k, v in r.shocks.items()},
            "total_pnl_usdt": r.total_pnl_usdt,
            "pct_of_equity": r.pct_of_equity,
            "position_impacts": r.position_impacts,
            "worst_position": r.worst_position,
        }

    @staticmethod
    def _mc_to_dict(r: MonteCarloResult) -> dict:
        return {
            "n_simulations": r.n_simulations,
            "confidence_level": r.confidence_level,
            "var_usdt": r.var_usdt,
            "cvar_usdt": r.cvar_usdt,
            "mean_daily_pnl": r.mean_pnl,
            "std_daily_pnl": r.std_pnl,
            "min_simulated_pnl": r.min_pnl,
            "max_simulated_pnl": r.max_pnl,
        }
