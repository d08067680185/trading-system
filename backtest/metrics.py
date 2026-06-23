"""Performance metrics for backtests."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    fee: float = 0.0


@dataclass
class BacktestMetrics:
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    calmar_ratio: float
    win_rate: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win_pct: float
    avg_loss_pct: float
    avg_holding_h: float
    total_fees: float
    deflated_sharpe: float = 0.0   # Sharpe corrected for multiple-testing bias
    data_quality: Optional[dict] = None   # gap check result
    equity_curve: list[tuple[float, float]] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_return_pct":      round(self.total_return_pct, 4),
            "annualized_return_pct": round(self.annualized_return_pct, 4),
            "sharpe_ratio":          round(self.sharpe_ratio, 3),
            "sortino_ratio":         round(self.sortino_ratio, 3),
            "max_drawdown_pct":      round(self.max_drawdown_pct, 4),
            "calmar_ratio":          round(self.calmar_ratio, 3),
            "win_rate":              round(self.win_rate, 4),
            "profit_factor":         round(self.profit_factor, 3),
            "total_trades":          self.total_trades,
            "winning_trades":        self.winning_trades,
            "losing_trades":         self.losing_trades,
            "avg_win_pct":           round(self.avg_win_pct, 4),
            "avg_loss_pct":          round(self.avg_loss_pct, 4),
            "avg_holding_h":         round(self.avg_holding_h, 2),
            "total_fees":            round(self.total_fees, 4),
            "deflated_sharpe":       round(self.deflated_sharpe, 3),
            "data_quality":          self.data_quality,
            "equity_curve":          [[ts, eq] for ts, eq in self.equity_curve],
            "trades": [
                {"symbol": t.symbol, "side": t.side,
                 "entry_time": t.entry_time, "exit_time": t.exit_time,
                 "entry_price": t.entry_price, "exit_price": t.exit_price,
                 "quantity": t.quantity, "pnl": round(t.pnl, 4),
                 "pnl_pct": round(t.pnl_pct, 4), "fee": t.fee}
                for t in self.trades
            ],
        }


def calculate(
    equity_curve: list[tuple[float, float]],   # [(timestamp, equity)]
    trades: list[BacktestTrade],
    initial_capital: float,
    risk_free_rate: float = 0.05,
    periods_per_year: int = 365 * 24,           # hourly candles
) -> BacktestMetrics:
    if not equity_curve or len(equity_curve) < 2:
        return BacktestMetrics(0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, equity_curve, trades)

    equities = [e for _, e in equity_curve]
    final    = equities[-1]
    total_return = (final - initial_capital) / initial_capital * 100

    # Duration in years
    duration_s  = equity_curve[-1][0] - equity_curve[0][0]
    duration_y  = max(duration_s / (365.25 * 86400), 1 / 365)
    ann_return  = ((final / initial_capital) ** (1 / duration_y) - 1) * 100

    # Period returns
    rets = [(equities[i] - equities[i-1]) / equities[i-1] for i in range(1, len(equities))]

    # Sharpe
    rfr_per_period = risk_free_rate / periods_per_year
    excess = [r - rfr_per_period for r in rets]
    sharpe = 0.0
    if excess:
        mean_e = sum(excess) / len(excess)
        std_e  = math.sqrt(sum((x - mean_e) ** 2 for x in excess) / len(excess)) if len(excess) > 1 else 1e-9
        sharpe = (mean_e / std_e * math.sqrt(periods_per_year)) if std_e > 0 else 0.0

    # Sortino
    down = [r for r in rets if r < rfr_per_period]
    sortino = 0.0
    if down:
        down_std = math.sqrt(sum(x**2 for x in down) / len(down))
        mean_r   = sum(rets) / len(rets)
        sortino  = ((mean_r - rfr_per_period) / down_std * math.sqrt(periods_per_year)) if down_std > 0 else 0.0

    # Max drawdown
    peak = equities[0]
    max_dd = 0.0
    for e in equities:
        if e > peak:
            peak = e
        dd = (peak - e) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = max_dd * 100

    # Calmar
    calmar = (ann_return / max_dd_pct) if max_dd_pct > 0 else 0.0

    # Trade stats
    wins  = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate     = len(wins) / len(trades) if trades else 0
    total_win    = sum(t.pnl for t in wins)
    total_loss   = abs(sum(t.pnl for t in losses))
    # Cap at 999 when there are no losing trades — inf breaks JSON serialization
    profit_factor = min(total_win / total_loss, 999.0) if total_loss > 0 else (999.0 if total_win > 0 else 0.0)
    avg_win   = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
    avg_hold  = sum((t.exit_time - t.entry_time) / 3600 for t in trades) / len(trades) if trades else 0
    total_fees = sum(t.fee for t in trades)

    dsr = deflated_sharpe_ratio(sharpe, len(rets), n_trials=1)

    return BacktestMetrics(
        total_return_pct=total_return,
        annualized_return_pct=ann_return,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd_pct,
        calmar_ratio=calmar,
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        avg_holding_h=avg_hold,
        total_fees=total_fees,
        deflated_sharpe=dsr,
        equity_curve=equity_curve,
        trades=trades,
    )


def deflated_sharpe_ratio(
    observed_sharpe: float,
    n_observations: int,
    n_trials: int = 1,
    skewness: float = 0.0,
    excess_kurtosis: float = 0.0,
) -> float:
    """
    Deflated Sharpe Ratio (DSR) — corrects for multiple-testing bias.

    When a strategy is optimized over many parameter combinations, the
    observed Sharpe is inflated by selection bias. DSR estimates the
    probability that the Sharpe is truly positive after accounting for N trials.

    Reference: Bailey & López de Prado (2012) "The Sharpe Ratio Efficient Frontier"

    Returns: deflated Sharpe (typically lower than observed; > 0 means likely genuine)
    """
    if n_observations < 5 or observed_sharpe <= 0:
        return 0.0

    # Expected max Sharpe from N_trials random draws (Euler-Mascheroni approximation)
    if n_trials > 1:
        gamma = 0.5772156649  # Euler-Mascheroni constant
        e_max = ((1 - gamma) * _inv_normal(1 - 1.0 / n_trials)
                 + gamma * _inv_normal(1 - 1.0 / (n_trials * math.e)))
    else:
        e_max = 0.0

    # Variance of Sharpe estimate
    sr_std = math.sqrt(
        (1 + 0.5 * observed_sharpe ** 2
         - skewness * observed_sharpe
         + (excess_kurtosis - 1) / 4 * observed_sharpe ** 2)
        / (n_observations - 1)
    ) if n_observations > 1 else 1e-6

    # Probability that observed SR > benchmark
    z = (observed_sharpe - e_max) / sr_std if sr_std > 0 else 0.0
    prob = _normal_cdf(z)
    # Return: probability × observed SR (conservative deflation)
    return round(prob * observed_sharpe, 4)


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via error function."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _inv_normal(p: float) -> float:
    """Approximate inverse normal for p in (0,1)."""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
    # Rational approximation (Abramowitz & Stegun)
    c = [2.515517, 0.802853, 0.010328]
    d = [1.432788, 0.189269, 0.001308]
    if p < 0.5:
        t = math.sqrt(-2 * math.log(p))
    else:
        t = math.sqrt(-2 * math.log(1 - p))
    num = c[0] + c[1] * t + c[2] * t ** 2
    den = 1 + d[0] * t + d[1] * t ** 2 + d[2] * t ** 3
    approx = t - num / den
    return -approx if p < 0.5 else approx


def check_data_gaps(
    rows: list,   # list of OHLCVRow
    interval: str,
    max_gap_pct: float = 0.05,   # warn if > 5% of expected bars are missing
) -> dict:
    """Check OHLCV data for gaps before running a backtest."""
    _INTERVAL_S = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "4h": 14400, "1d": 86400,
    }
    if not rows or len(rows) < 2:
        return {"ok": False, "reason": "Insufficient data", "bars": len(rows)}

    bar_s = _INTERVAL_S.get(interval, 3600)
    timestamps = [r.ts for r in rows]
    expected_bars = int((timestamps[-1] - timestamps[0]) / bar_s) + 1
    actual_bars   = len(timestamps)
    gap_pct = max(0.0, 1.0 - actual_bars / expected_bars) if expected_bars > 0 else 0.0

    # Find largest consecutive gap
    gaps = [timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))]
    max_gap_s = max(gaps) if gaps else 0
    max_gap_bars = max_gap_s / bar_s

    ok = gap_pct <= max_gap_pct
    return {
        "ok": ok,
        "gap_pct": round(gap_pct * 100, 2),
        "expected_bars": expected_bars,
        "actual_bars": actual_bars,
        "max_consecutive_gap_bars": round(max_gap_bars, 1),
        "warning": None if ok else f"Data has {gap_pct * 100:.1f}% gaps (max {max_gap_pct * 100:.1f}% allowed)",
    }
