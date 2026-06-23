"""Tests for the parameter optimizer:
  - grid search picks the best-scoring params and ranks results
  - OptimizeResult / WalkForwardResult .to_dict() shapes (frontend contract)
  - bayesian bounds validation
  - bayesian completion within bounds (seeded)
"""
import asyncio
import random
from types import SimpleNamespace

import pytest

from optimizer.engine import ParameterOptimizer, OptimizeResult, WalkForwardResult


# ── Fake backtest engine: result score is a function of params ───────────────

class _FakeBT:
    """create_job returns an already-'done' job whose sharpe peaks at x=5."""
    def __init__(self):
        self.calls = []

    def create_job(self, strategy_class, strategy_id, params, exchange, symbol,
                   interval, start_ts, end_ts, initial_capital=10000.0, **kw):
        self.calls.append(params)
        x = float(params.get("x", 0))
        sharpe = -((x - 5) ** 2)          # max at x=5
        return SimpleNamespace(status="done", result={
            "sharpe_ratio": sharpe,
            "total_return_pct": sharpe * 2,
            "max_drawdown_pct": 5.0,
            "win_rate": 0.5,
            "total_trades": 10,
        })


def _run(make_job):
    """make_job() is called inside the running loop (it schedules a create_task),
    then we poll the returned result object until it finishes."""
    async def run():
        result = make_job()
        while result.status in ("pending", "running"):
            await asyncio.sleep(0.005)
        return result
    return asyncio.run(run())


# ── Grid search ──────────────────────────────────────────────────────────────

def test_grid_search_picks_best_and_ranks():
    opt = ParameterOptimizer(_FakeBT())
    res = _run(lambda: opt.grid_search(
        strategy_class=object, strategy_id="s", param_grid={"x": [3, 5, 8]},
        exchange="binance", symbol="BTC-USDT", interval="1h", start_ts=0, end_ts=1))
    assert res.status == "done"
    assert res.best_params == {"x": 5}              # peak
    assert res.best_sharpe == 0.0                   # -((5-5)^2)
    assert res.runs == 3
    # all_results sorted by sharpe descending
    sharpes = [r["sharpe"] for r in res.all_results]
    assert sharpes == sorted(sharpes, reverse=True)


def test_grid_search_multi_param_runs_all_combos():
    bt = _FakeBT()
    opt = ParameterOptimizer(bt)
    res = _run(lambda: opt.grid_search(
        strategy_class=object, strategy_id="s",
        param_grid={"x": [3, 5], "y": [1, 2, 3]},
        exchange="binance", symbol="BTC-USDT", interval="1h", start_ts=0, end_ts=1))
    assert res.runs == 6                            # 2 * 3 combinations
    assert len(bt.calls) == 6


# ── OptimizeResult.to_dict ───────────────────────────────────────────────────

def test_optimize_result_to_dict_shape():
    r = OptimizeResult(job_id="j", strategy_id="s", method="grid",
                       best_params={"x": 5}, best_sharpe=1.2345, best_return=10.0,
                       best_drawdown=3.0, runs=5,
                       all_results=[{"sharpe": i} for i in range(30)])
    d = r.to_dict()
    assert d["best_sharpe"] == 1.234                # rounded to 3dp
    assert len(d["top_results"]) == 20              # capped at 20
    assert d["runs"] == 5


# ── WalkForwardResult.to_dict normalization ──────────────────────────────────

def test_walk_forward_to_dict_normalizes_folds():
    wf = WalkForwardResult(job_id="j", strategy_id="s", method="grid",
                           n_folds=2, train_frac=0.7)
    wf.folds = [
        {"fold": 1, "out_sample": {"sharpe": 1.5, "return_pct": 10.0, "drawdown_pct": 4.0}},
        {"fold": 2, "out_sample": {"sharpe": None, "return_pct": 0.0, "drawdown_pct": 0.0}},
    ]
    wf.avg_out_sharpe = 1.5
    d = wf.to_dict()
    assert d["type"] == "walk_forward"
    # frontend reads fold.oos_metrics.sharpe_ratio
    assert d["folds"][0]["oos_metrics"]["sharpe_ratio"] == 1.5
    assert d["folds"][1]["oos_metrics"]["sharpe_ratio"] is None
    # aggregate counts only completed folds (sharpe not None)
    assert d["aggregate"]["n_folds"] == 1
    assert d["aggregate"]["mean_oos_sharpe"] == 1.5


# ── Bayesian ─────────────────────────────────────────────────────────────────

def test_bayesian_invalid_bounds_errors():
    opt = ParameterOptimizer(_FakeBT())
    res = _run(lambda: opt.bayesian_search(
        strategy_class=object, strategy_id="s",
        param_bounds={"x": (10, 2)},   # lo >= hi
        exchange="binance", symbol="BTC-USDT", interval="1h",
        start_ts=0, end_ts=1, n_calls=10))
    assert res.status == "error"
    assert "Invalid bounds" in res.error


def test_bayesian_finds_near_optimum():
    random.seed(7)
    opt = ParameterOptimizer(_FakeBT())
    res = _run(lambda: opt.bayesian_search(
        strategy_class=object, strategy_id="s",
        param_bounds={"x": (0.0, 10.0)},
        exchange="binance", symbol="BTC-USDT", interval="1h",
        start_ts=0, end_ts=1, n_calls=20))
    assert res.status == "done"
    assert 0.0 <= res.best_params["x"] <= 10.0
    # peak is at x=5 (sharpe 0); search should beat a random corner like x=0 (-25)
    assert res.best_sharpe > -10.0
