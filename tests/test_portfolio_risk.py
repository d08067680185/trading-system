"""Unit tests for PortfolioRisk — VaR, correlation, Sharpe."""
import pytest
import math
from risk.portfolio import PortfolioRisk


def feed_strategy_pnl(pr: PortfolioRisk, strategy_id: str, pnls: list[float]) -> None:
    """Simulate N days of completed PnL."""
    for pnl in pnls:
        pr._daily_pnl[strategy_id] = pr._daily_pnl.get(strategy_id) or __import__("collections").deque(maxlen=pr.lookback_days)
        pr._daily_pnl[strategy_id].append(pnl)


def test_var_negative_pnl_distribution():
    pr = PortfolioRisk(lookback_days=30)
    feed_strategy_pnl(pr, "s1", [-5, -3, -1, 0, 1, 2, 3, 4, 5, -8])
    var = pr.portfolio_var(0.90)
    assert var < 0, "VaR should be negative (representing a loss)"


def test_cvar_worse_than_var():
    pr = PortfolioRisk(lookback_days=30)
    feed_strategy_pnl(pr, "s1", [-10, -8, -5, -2, 0, 1, 2, 3, 4, 5, -15])
    var  = pr.portfolio_var(0.95)
    cvar = pr.portfolio_cvar(0.95)
    assert cvar <= var, "CVaR (expected shortfall) should be ≤ VaR"


def test_var_zero_with_no_data():
    pr = PortfolioRisk()
    assert pr.portfolio_var() == 0.0
    assert pr.portfolio_cvar() == 0.0


def test_correlation_perfect_positive():
    pr = PortfolioRisk()
    same_pnl = [1.0, 2.0, -1.0, 3.0, -2.0, 0.5]
    feed_strategy_pnl(pr, "s1", same_pnl)
    feed_strategy_pnl(pr, "s2", same_pnl)
    matrix = pr.correlation_matrix()
    assert abs(matrix["s1"]["s2"] - 1.0) < 0.01


def test_correlation_perfect_negative():
    pr = PortfolioRisk()
    pnl1 = [1.0, -1.0, 2.0, -2.0, 3.0, -3.0]
    pnl2 = [-1.0, 1.0, -2.0, 2.0, -3.0, 3.0]
    feed_strategy_pnl(pr, "s1", pnl1)
    feed_strategy_pnl(pr, "s2", pnl2)
    matrix = pr.correlation_matrix()
    assert abs(matrix["s1"]["s2"] - (-1.0)) < 0.05


def test_high_correlation_pairs_detected():
    pr = PortfolioRisk(max_strategy_correlation=0.7)
    pnl = [1.0, 2.0, -1.0, 3.0, -2.0, 0.5, 1.5, -0.5]
    feed_strategy_pnl(pr, "s1", pnl)
    feed_strategy_pnl(pr, "s2", [p * 0.9 for p in pnl])  # 90% correlated
    pairs = pr.high_correlation_pairs()
    assert len(pairs) > 0
    assert pairs[0][2] > 0.7


def test_portfolio_sharpe_positive_mean():
    pr = PortfolioRisk()
    # Consistent positive PnL → positive Sharpe
    feed_strategy_pnl(pr, "s1", [2.0, 1.5, 2.5, 1.8, 2.2, 1.9, 2.1])
    sharpe = pr.portfolio_sharpe()
    assert sharpe > 0


def test_herfindahl_equal_weight():
    pr = PortfolioRisk()
    # 4 equal-weight strategies → HHI = 4 × (0.25)² = 0.25
    hhi = pr.herfindahl_index({"a": 25, "b": 25, "c": 25, "d": 25})
    assert abs(hhi - 0.25) < 0.001


def test_herfindahl_single_strategy():
    pr = PortfolioRisk()
    hhi = pr.herfindahl_index({"a": 100})
    assert abs(hhi - 1.0) < 0.001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
