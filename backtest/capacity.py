"""
Strategy capacity analysis.

Simulates the same strategy at increasing capital levels and measures
how Sharpe ratio, return, and effective spread degrade due to market impact.

The inflection point where Sharpe begins declining is the "capacity" estimate.

Usage:
    analyzer = CapacityAnalyzer(backtest_engine)
    result = await analyzer.run(
        strategy_class=SpreadArbStrategy,
        strategy_id="arb_spread",
        base_params={...},
        exchange="binance", symbol="BTC-USDT", interval="1h",
        start_ts=..., end_ts=...,
        capital_levels=[100, 500, 1000, 5000, 10000, 50000],
    )
"""
from __future__ import annotations
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional
import time

logger = logging.getLogger("CapacityAnalyzer")


@dataclass
class CapacityResult:
    job_id: str
    strategy_id: str
    status: str = "pending"
    progress: float = 0.0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    # List of {capital, sharpe, return_pct, max_drawdown_pct, trades, effective_spread_bps}
    curve: list[dict] = field(default_factory=list)
    estimated_capacity_usdt: Optional[float] = None
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "strategy_id": self.strategy_id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "error": self.error,
            "curve": self.curve,
            "estimated_capacity_usdt": self.estimated_capacity_usdt,
            "notes": self.notes,
        }


class CapacityAnalyzer:
    def __init__(self, backtest_engine):
        self._bt = backtest_engine
        self._jobs: dict[str, CapacityResult] = {}

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: -x.created_at)]

    def get_job(self, job_id: str) -> Optional[CapacityResult]:
        return self._jobs.get(job_id)

    def run(
        self,
        strategy_class,
        strategy_id: str,
        base_params: dict,
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        capital_levels: Optional[list[float]] = None,
    ) -> CapacityResult:
        """Launch capacity analysis. Returns result immediately; runs in background."""
        if capital_levels is None:
            capital_levels = [100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000]
        result = CapacityResult(
            job_id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
        )
        self._jobs[result.job_id] = result
        asyncio.create_task(self._run(
            result, strategy_class, base_params,
            exchange, symbol, interval, start_ts, end_ts, capital_levels,
        ))
        return result

    async def _run(
        self, result: CapacityResult, strategy_class, base_params,
        exchange, symbol, interval, start_ts, end_ts, capitals,
    ) -> None:
        result.status = "running"
        sharpes = []

        for i, capital in enumerate(capitals):
            # Scale position sizes proportionally with capital
            # Find any USDT-denominated params and scale them
            scaled_params = self._scale_params(base_params, capital, capitals[0])

            try:
                job = self._bt.create_job(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    params=scaled_params,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=start_ts, end_ts=end_ts,
                    initial_capital=float(capital),
                    slippage_bps=self._estimate_slippage(capital),
                )
                while job.status in ("pending", "running"):
                    await asyncio.sleep(0.05)

                if job.status == "done" and job.result:
                    r = job.result
                    sharpe = r.get("sharpe_ratio", 0.0) or 0.0
                    sharpes.append(sharpe)
                    result.curve.append({
                        "capital_usdt": capital,
                        "sharpe_ratio": round(sharpe, 3),
                        "total_return_pct": round(r.get("total_return_pct", 0), 2),
                        "max_drawdown_pct": round(r.get("max_drawdown_pct", 0), 2),
                        "total_trades": r.get("total_trades", 0),
                        "slippage_bps_assumed": self._estimate_slippage(capital),
                    })
                else:
                    result.curve.append({"capital_usdt": capital, "error": job.error})

            except Exception as e:
                logger.warning(f"Capacity run at {capital} USDT failed: {e}")
                result.curve.append({"capital_usdt": capital, "error": str(e)})

            result.progress = (i + 1) / len(capitals)

        # Find capacity: point where Sharpe drops > 20% from peak
        result.estimated_capacity_usdt = self._find_capacity(result.curve)
        result.notes = self._capacity_note(result)
        result.status = "done"
        logger.info(
            f"Capacity analysis done for {result.strategy_id}: "
            f"estimated_capacity={result.estimated_capacity_usdt} USDT"
        )

    @staticmethod
    def _scale_params(params: dict, capital: float, base_capital: float) -> dict:
        """Scale USDT-denominated params linearly with capital."""
        if base_capital <= 0:
            return params
        ratio = capital / base_capital
        scaled = {}
        usdt_keys = [k for k in params if "usdt" in k.lower() or "size" in k.lower()]
        for k, v in params.items():
            if k in usdt_keys and isinstance(v, (int, float)):
                scaled[k] = round(v * ratio, 2)
            else:
                scaled[k] = v
        return scaled

    @staticmethod
    def _estimate_slippage(capital_usdt: float) -> int:
        """Estimate market impact in bps based on order size.
        Small orders: ~1 bps; large: ~20 bps (rough empirical crypto estimate).
        """
        if capital_usdt < 500:
            return 1
        if capital_usdt < 2000:
            return 3
        if capital_usdt < 10000:
            return 7
        if capital_usdt < 50000:
            return 15
        return 25

    @staticmethod
    def _find_capacity(curve: list[dict]) -> Optional[float]:
        valid = [p for p in curve if "sharpe_ratio" in p and p.get("total_trades", 0) > 0]
        if len(valid) < 2:
            return None
        peak_sharpe = max(p["sharpe_ratio"] for p in valid)
        if peak_sharpe <= 0:
            return None
        # Capacity = last level where Sharpe > 80% of peak
        threshold = peak_sharpe * 0.80
        capacity = None
        for p in valid:
            if p["sharpe_ratio"] >= threshold:
                capacity = p["capital_usdt"]
        return capacity

    @staticmethod
    def _capacity_note(result: CapacityResult) -> str:
        cap = result.estimated_capacity_usdt
        if cap is None:
            return "Insufficient data to estimate capacity."
        if cap >= 50000:
            return f"Strategy scales well; capacity ≥ ${cap:,.0f} USDT."
        return (
            f"Estimated capacity ~${cap:,.0f} USDT. "
            f"Beyond this level, market impact degrades returns significantly."
        )
