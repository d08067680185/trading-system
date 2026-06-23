"""
Cross-exchange backtest: spread_arb driven through the real TradingEngine over
two SimulatedConnectors (Binance + OKX) fed from two aligned OHLCV streams.

Validates the multi-stream runner plumbing and the engine sim-clock: spread_arb
detects and executes arbs when a persistent cross-exchange spread exists, and
does nothing when the two books are level. (Taker mode is used so legs fill
deterministically at the next bar open; double-maker fills need the price to
cross the resting limits, which OHLCV cannot model faithfully.)
"""
import asyncio

from core.types import Exchange
from data.storage import OHLCVRow
from backtest.runner import BacktestRunner
from strategies.spread_arb import SpreadArbStrategy

SYMBOL = "BTC-USDT"


def _stream(exchange, price, n=30, start=1_700_000_000):
    """A flat OHLCV stream at `price` for `n` bars (one symbol, one exchange)."""
    return [
        OHLCVRow(exchange=exchange, symbol=SYMBOL, interval="1h",
                 ts=start + i * 3600, open=price, high=price, low=price,
                 close=price, volume=100.0)
        for i in range(n)
    ]


def _taker_params(**over):
    p = {
        "min_profit_bps": 5.0, "fee_bps": 4.0, "order_size_usdt": 100.0,
        "cooldown_s": 30.0, "max_position_usdt": 1e9, "leg_timeout_s": 8.0,
        "maker_both_legs": False, "use_maker_leg": False,
        "spread_confirm_ticks": 1, "min_book_depth_mult": 0.0,
    }
    p.update(over)
    return p


def _run(strategy, bn_price, okx_price):
    streams = {
        Exchange.BINANCE: _stream("binance", bn_price),
        Exchange.OKX: _stream("okx", okx_price),
    }
    return asyncio.run(BacktestRunner().run_multi(
        strategy=strategy, streams=streams, symbol=SYMBOL,
        initial_capital=10_000.0, taker_fee_bps=4.0, maker_fee_bps=2.0,
        half_spread_bps=0.5,
    ))


def test_persistent_cross_exchange_spread_triggers_arbs():
    """Binance 30bps above OKX → spread_arb repeatedly sells Binance / buys OKX."""
    strat = SpreadArbStrategy("arb", _taker_params())
    metrics = _run(strat, bn_price=100.30, okx_price=100.00)
    assert strat._arb_count >= 5
    # Accumulated legs are closed at end-of-test liquidation → realized trades exist.
    assert len(metrics.trades) >= 1


def test_no_spread_means_no_arbs():
    """Level books (both at 100) never clear the fee+profit threshold → no arbs."""
    strat = SpreadArbStrategy("arb", _taker_params())
    metrics = _run(strat, bn_price=100.00, okx_price=100.00)
    assert strat._arb_count == 0
    assert len(metrics.trades) == 0


def test_sim_clock_lets_cooldown_advance_across_bars():
    """With wall-clock time the 30s cooldown would block every arb after the first
    (a full replay runs in milliseconds). The engine sim-clock advances 1h per bar,
    so the cooldown clears and arbs recur — i.e. more than one arb is executed."""
    strat = SpreadArbStrategy("arb", _taker_params(cooldown_s=30.0))
    _run(strat, bn_price=100.30, okx_price=100.00)
    assert strat._arb_count > 1
