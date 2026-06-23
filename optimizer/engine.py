"""
Parameter optimizer: Grid Search + Bayesian (scipy) + Walk-Forward validation.
Runs backtests across parameter combinations and ranks by Sharpe ratio.
"""
from __future__ import annotations
import asyncio
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("Optimizer")


@dataclass
class OptimizeResult:
    job_id: str
    strategy_id: str
    method: str          # "grid" | "bayesian"
    best_params: dict
    best_sharpe: float
    best_return: float
    best_drawdown: float
    runs: int
    all_results: list[dict] = field(default_factory=list)
    status: str = "pending"
    progress: float = 0.0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "strategy_id": self.strategy_id,
            "method": self.method,
            "best_params": self.best_params,
            "best_sharpe": round(self.best_sharpe, 3),
            "best_return": round(self.best_return, 3),
            "best_drawdown": round(self.best_drawdown, 3),
            "runs": self.runs,
            "status": self.status,
            "progress": round(self.progress, 3),
            "error": self.error,
            "top_results": self.all_results[:20],
        }


@dataclass
class WalkForwardResult:
    job_id: str
    strategy_id: str
    method: str           # "grid" | "bayesian"
    n_folds: int
    train_frac: float
    folds: list[dict] = field(default_factory=list)
    avg_out_sharpe: float = 0.0
    avg_out_return: float = 0.0
    avg_out_drawdown: float = 0.0
    status: str = "pending"
    progress: float = 0.0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        # Normalize folds so frontend can use fold.oos_metrics.sharpe_ratio etc.
        normalized_folds = []
        for fold in self.folds:
            oos = fold.get("out_sample") or {}
            normalized_folds.append({
                **fold,
                "oos_metrics": {
                    "sharpe_ratio": oos.get("sharpe"),
                    "total_return_pct": oos.get("return_pct"),
                    "max_drawdown_pct": oos.get("drawdown_pct"),
                },
            })
        completed = [f for f in self.folds if (f.get("out_sample") or {}).get("sharpe") is not None]
        return {
            "job_id": self.job_id,
            "strategy_id": self.strategy_id,
            "method": self.method,
            "type": "walk_forward",
            "n_folds": self.n_folds,
            "train_frac": self.train_frac,
            "folds": normalized_folds,
            "aggregate": {
                "mean_oos_sharpe": round(self.avg_out_sharpe, 3),
                "mean_oos_return": round(self.avg_out_return, 3),
                "mean_oos_drawdown": round(self.avg_out_drawdown, 3),
                "n_folds": len(completed),
            },
            # keep legacy fields for any other consumers
            "avg_out_sharpe": round(self.avg_out_sharpe, 3),
            "avg_out_return": round(self.avg_out_return, 3),
            "avg_out_drawdown": round(self.avg_out_drawdown, 3),
            "status": self.status,
            "progress": round(self.progress, 3),
            "error": self.error,
        }


class ParameterOptimizer:
    def __init__(self, backtest_engine):
        self._bt = backtest_engine
        self._jobs: dict[str, OptimizeResult] = {}
        self._wf_jobs: dict[str, WalkForwardResult] = {}

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: -x.created_at)]

    def get_job(self, job_id: str) -> Optional[OptimizeResult]:
        return self._jobs.get(job_id)

    def list_wf_jobs(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self._wf_jobs.values(), key=lambda x: -x.created_at)]

    def get_wf_job(self, job_id: str) -> Optional[WalkForwardResult]:
        return self._wf_jobs.get(job_id)

    # ── Walk-Forward Validation ───────────────────────────────────────────────

    def walk_forward(
        self,
        strategy_class,
        strategy_id: str,
        param_grid: dict[str, list],
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        n_folds: int = 5,
        train_frac: float = 0.7,
        method: str = "grid",
        initial_capital: float = 10_000.0,
        metric: str = "sharpe_ratio",
    ) -> WalkForwardResult:
        result = WalkForwardResult(
            job_id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
            method=method,
            n_folds=n_folds,
            train_frac=train_frac,
        )
        self._wf_jobs[result.job_id] = result
        asyncio.create_task(self._run_walk_forward(
            result, strategy_class, param_grid,
            exchange, symbol, interval, start_ts, end_ts,
            n_folds, train_frac, method, initial_capital, metric,
        ))
        return result

    async def _run_walk_forward(
        self, result: WalkForwardResult, strategy_class, param_grid,
        exchange, symbol, interval, start_ts, end_ts,
        n_folds, train_frac, method, capital, metric,
    ) -> None:
        result.status = "running"
        total_span = end_ts - start_ts
        fold_span  = total_span // n_folds

        out_sharpes, out_returns, out_drawdowns = [], [], []

        for fold_i in range(n_folds):
            fold_start = start_ts + fold_i * fold_span
            fold_end   = fold_start + fold_span
            train_end  = int(fold_start + fold_span * train_frac)

            logger.info(
                f"Walk-forward fold {fold_i+1}/{n_folds}: "
                f"train [{fold_start}..{train_end}] test [{train_end}..{fold_end}]"
            )

            # ── In-sample optimization ────────────────────────────────────────
            if method == "bayesian":
                # Convert grid lists to (min, max) bounds for bayesian
                bounds = {k: (min(v), max(v)) for k, v in param_grid.items()}
                opt_job = self.bayesian_search(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    param_bounds=bounds,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=fold_start, end_ts=train_end,
                    n_calls=max(10, len(param_grid) * 5),
                    initial_capital=capital, metric=metric,
                )
            else:
                opt_job = self.grid_search(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    param_grid=param_grid,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=fold_start, end_ts=train_end,
                    initial_capital=capital, metric=metric,
                )
            while opt_job.status in ("pending", "running"):
                await asyncio.sleep(0.1)

            best_params = opt_job.best_params if opt_job.status == "done" else {}
            in_sharpe   = opt_job.best_sharpe if opt_job.status == "done" else float("-inf")
            in_return   = opt_job.best_return if opt_job.status == "done" else 0.0

            # ── Out-of-sample test ────────────────────────────────────────────
            out_sharpe, out_return, out_drawdown = float("-inf"), 0.0, 0.0
            if best_params:
                oos_job = self._bt.create_job(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    params=best_params,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=train_end, end_ts=fold_end, initial_capital=capital,
                )
                while oos_job.status in ("pending", "running"):
                    await asyncio.sleep(0.05)
                if oos_job.status == "done" and oos_job.result:
                    out_sharpe   = oos_job.result.get("sharpe_ratio", float("-inf"))
                    out_return   = oos_job.result.get("total_return_pct", 0.0)
                    out_drawdown = oos_job.result.get("max_drawdown_pct", 0.0)

            result.folds.append({
                "fold": fold_i + 1,
                "train_start": fold_start,
                "train_end": train_end,
                "test_start": train_end,
                "test_end": fold_end,
                "best_params": best_params,
                "in_sample": {"sharpe": round(in_sharpe, 3), "return_pct": round(in_return, 3)},
                "out_sample": {
                    "sharpe": round(out_sharpe, 3) if out_sharpe > float("-inf") else None,
                    "return_pct": round(out_return, 3),
                    "drawdown_pct": round(out_drawdown, 3),
                },
            })

            if out_sharpe > float("-inf"):
                out_sharpes.append(out_sharpe)
                out_returns.append(out_return)
                out_drawdowns.append(out_drawdown)

            result.progress = (fold_i + 1) / n_folds

        if out_sharpes:
            result.avg_out_sharpe   = sum(out_sharpes) / len(out_sharpes)
            result.avg_out_return   = sum(out_returns) / len(out_returns)
            result.avg_out_drawdown = sum(out_drawdowns) / len(out_drawdowns)

        result.status = "done"
        logger.info(
            f"Walk-forward done. Avg OOS Sharpe={result.avg_out_sharpe:.3f} "
            f"Return={result.avg_out_return:.2f}%"
        )

    # ── Grid Search ───────────────────────────────────────────────────────────

    def grid_search(
        self,
        strategy_class,
        strategy_id: str,
        param_grid: dict[str, list],   # {"min_spread_bps": [3,5,8], "cooldown_s": [20,30]}
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        initial_capital: float = 10_000.0,
        metric: str = "sharpe_ratio",
    ) -> OptimizeResult:
        result = OptimizeResult(
            job_id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
            method="grid",
            best_params={},
            best_sharpe=float("-inf"),
            best_return=0.0,
            best_drawdown=0.0,
            runs=0,
        )
        self._jobs[result.job_id] = result
        asyncio.create_task(self._run_grid(
            result, strategy_class, param_grid,
            exchange, symbol, interval, start_ts, end_ts, initial_capital, metric,
        ))
        return result

    async def _run_grid(self, result: OptimizeResult, strategy_class, param_grid,
                         exchange, symbol, interval, start_ts, end_ts, capital, metric):
        result.status = "running"
        keys = list(param_grid.keys())
        combos = list(itertools.product(*[param_grid[k] for k in keys]))
        total = len(combos)
        logger.info(f"Grid search: {total} combinations for {result.strategy_id}")

        for i, combo in enumerate(combos):
            params = dict(zip(keys, combo))
            try:
                job = self._bt.create_job(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    params=params,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=start_ts, end_ts=end_ts, initial_capital=capital,
                )
                # Wait for job to complete
                while job.status in ("pending", "running"):
                    await asyncio.sleep(0.05)

                if job.status == "done" and job.result:
                    r = job.result
                    score = r.get(metric, 0)
                    entry = {"params": params, "sharpe": r.get("sharpe_ratio", 0),
                             "total_return_pct": r.get("total_return_pct", 0),
                             "max_drawdown_pct": r.get("max_drawdown_pct", 0),
                             "win_rate": r.get("win_rate", 0),
                             "total_trades": r.get("total_trades", 0)}
                    result.all_results.append(entry)

                    if score > result.best_sharpe:
                        result.best_sharpe   = score
                        result.best_params   = params
                        result.best_return   = r.get("total_return_pct", 0)
                        result.best_drawdown = r.get("max_drawdown_pct", 0)
            except Exception as e:
                logger.warning(f"Grid combo {params} failed: {e}")

            result.runs = i + 1
            result.progress = (i + 1) / total

        result.all_results.sort(key=lambda x: -x.get("sharpe", float("-inf")))
        result.status = "done"
        logger.info(f"Grid search done. Best sharpe={result.best_sharpe:.3f} params={result.best_params}")

    # ── Bayesian (scipy) ──────────────────────────────────────────────────────

    def bayesian_search(
        self,
        strategy_class,
        strategy_id: str,
        param_bounds: dict[str, tuple[float, float]],  # {"min_spread_bps": (2, 15)}
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: int,
        end_ts: int,
        n_calls: int = 30,
        initial_capital: float = 10_000.0,
        metric: str = "sharpe_ratio",
    ) -> OptimizeResult:
        result = OptimizeResult(
            job_id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
            method="bayesian",
            best_params={},
            best_sharpe=float("-inf"),
            best_return=0.0,
            best_drawdown=0.0,
            runs=0,
        )
        self._jobs[result.job_id] = result
        asyncio.create_task(self._run_bayesian(
            result, strategy_class, param_bounds,
            exchange, symbol, interval, start_ts, end_ts, n_calls, initial_capital, metric,
        ))
        return result

    async def _run_bayesian(self, result: OptimizeResult, strategy_class, param_bounds,
                             exchange, symbol, interval, start_ts, end_ts, n_calls, capital, metric):
        # Validate bounds before starting
        for key, (lo, hi) in param_bounds.items():
            if lo >= hi:
                result.status = "error"
                result.error = f"Invalid bounds for '{key}': lo={lo} >= hi={hi}"
                return
        result.status = "running"
        keys   = list(param_bounds.keys())
        bounds = [param_bounds[k] for k in keys]

        # (x, score, full_result_dict)
        evaluated: list[tuple[list, float, dict]] = []

        async def evaluate(x: list) -> float:
            params = {k: float(v) for k, v in zip(keys, x)}
            try:
                job = self._bt.create_job(
                    strategy_class=strategy_class,
                    strategy_id=result.strategy_id,
                    params=params,
                    exchange=exchange, symbol=symbol, interval=interval,
                    start_ts=start_ts, end_ts=end_ts, initial_capital=capital,
                )
                while job.status in ("pending", "running"):
                    await asyncio.sleep(0.05)
                if job.status == "done" and job.result:
                    r = job.result
                    score = r.get(metric, float("-inf"))
                    evaluated.append((x, score, r))
                    return score
            except Exception as e:
                logger.warning(f"Bayesian eval failed: {e}")
            evaluated.append((x, float("-inf"), {}))
            return float("-inf")

        # Latin hypercube initial sampling (n_calls // 3 points)
        import random
        n_init = max(5, n_calls // 3)
        for _ in range(n_init):
            x = [random.uniform(lo, hi) for (lo, hi) in bounds]
            await evaluate(x)
            result.runs += 1
            result.progress = result.runs / n_calls

        # Gaussian process surrogate via scipy
        try:
            from scipy.optimize import minimize
            import numpy as np

            def gp_surrogate(x_new):
                # Simple RBF kernel surrogate
                if not evaluated:
                    return 0.0
                X = np.array([e[0] for e in evaluated])
                y = np.array([e[1] for e in evaluated])
                x_new = np.array(x_new)
                dists = np.sqrt(((X - x_new) ** 2).sum(axis=1))
                sigma = np.median(dists) + 1e-8
                weights = np.exp(-0.5 * (dists / sigma) ** 2)
                weights /= weights.sum() + 1e-12
                pred = (weights * y).sum()
                uncertainty = np.sqrt((weights * (y - pred) ** 2).sum())
                return -(pred + 0.1 * uncertainty)  # UCB acquisition (minimized)

            remaining = n_calls - n_init
            for _ in range(remaining):
                best_acq, best_x = float("inf"), None
                for _ in range(10):
                    x0 = [random.uniform(lo, hi) for (lo, hi) in bounds]
                    res = minimize(gp_surrogate, x0, method="L-BFGS-B",
                                   bounds=bounds, options={"maxiter": 50})
                    if res.fun < best_acq:
                        best_acq = res.fun
                        best_x = res.x.tolist()
                if best_x:
                    await evaluate(best_x)
                result.runs += 1
                result.progress = result.runs / n_calls

        except ImportError:
            logger.warning("scipy not available, running random search for remaining evals")
            remaining = n_calls - len(evaluated)
            for _ in range(remaining):
                x = [random.uniform(lo, hi) for (lo, hi) in bounds]
                await evaluate(x)
                result.runs += 1
                result.progress = result.runs / n_calls

        # Collect results
        for x, score, r in evaluated:
            params = {k: float(v) for k, v in zip(keys, x)}
            result.all_results.append({
                "params": params,
                "sharpe": score,
                "total_return_pct": r.get("total_return_pct", 0),
                "max_drawdown_pct": r.get("max_drawdown_pct", 0),
                "win_rate": r.get("win_rate", 0),
                "total_trades": r.get("total_trades", 0),
            })

        if evaluated:
            best_x, best_score, best_r = max(evaluated, key=lambda e: e[1])
            result.best_params   = {k: float(v) for k, v in zip(keys, best_x)}
            result.best_sharpe   = best_score
            result.best_return   = best_r.get("total_return_pct", 0)
            result.best_drawdown = best_r.get("max_drawdown_pct", 0)
        result.all_results.sort(key=lambda x: -x.get("sharpe", float("-inf")))
        result.status = "done"
        logger.info(f"Bayesian search done. Best sharpe={result.best_sharpe:.3f}")
