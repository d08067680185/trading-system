"""
Continuous position reconciler.

Every `interval_s` seconds, fetches actual positions from each exchange via REST
and compares with the risk manager's local state. Discrepancies are logged and
the risk manager is corrected to prevent compounding errors.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.engine import TradingEngine

logger = logging.getLogger("Reconciler")


class PositionReconciler:
    def __init__(self, engine: "TradingEngine", interval_s: int = 300):
        self._engine   = engine
        self._interval = interval_s
        self._task: asyncio.Task | None = None
        self._last_run: float = 0.0
        self._discrepancy_count: int = 0
        self._last_report: dict = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Reconciler started (interval={self._interval}s)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def reconcile_now(self) -> dict:
        """Run a single reconciliation pass and return the report."""
        return await self._run_once()

    def status(self) -> dict:
        return {
            "last_run_ts": round(self._last_run, 1),
            "seconds_since_run": round(time.time() - self._last_run, 0) if self._last_run else None,
            "discrepancy_count": self._discrepancy_count,
            "last_report": self._last_report,
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        # Initial delay so engine has time to settle
        await asyncio.sleep(30)
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Reconciliation error: {e}")
            await asyncio.sleep(self._interval)

    async def _run_once(self) -> dict:
        self._last_run = time.time()
        report: dict = {
            "ts": self._last_run,
            "exchanges_checked": [],
            "discrepancies": [],
            "corrections": [],
        }

        for ex, connector in self._engine.connectors.items():
            if self._engine._connector_states.get(ex.value) != "connected":
                continue
            report["exchanges_checked"].append(ex.value)

            try:
                actual_positions = await connector.get_positions()
            except Exception as e:
                logger.warning(f"Reconcile: failed to fetch positions [{ex.value}]: {e}")
                continue

            # Build lookup: symbol → actual notional
            actual: dict[str, float] = {}
            for pos in actual_positions:
                actual[pos.symbol] = float(pos.notional)

            # Compare with risk manager tracked notionals
            rm = self._engine.risk_manager
            # Get positions tracked by risk manager for this exchange
            local: dict[str, float] = {
                sym: float(notional)
                for (exch, sym), notional in rm.state.position_notionals.items()
                if exch == ex.value
            }

            # Find discrepancies
            all_symbols = set(actual.keys()) | set(local.keys())
            for symbol in all_symbols:
                a_val = actual.get(symbol, 0.0)
                l_val = local.get(symbol, 0.0)
                diff = abs(a_val - l_val)
                # Tolerance: ignore differences < $1 or < 2% of actual
                tolerance = max(1.0, a_val * 0.02)
                if diff > tolerance:
                    self._discrepancy_count += 1
                    disc = {
                        "exchange": ex.value,
                        "symbol": symbol,
                        "actual_notional": round(a_val, 2),
                        "local_notional": round(l_val, 2),
                        "diff": round(diff, 2),
                    }
                    report["discrepancies"].append(disc)
                    logger.warning(
                        f"Position mismatch [{ex.value}] {symbol}: "
                        f"actual={a_val:.2f} local={l_val:.2f} diff={diff:.2f}"
                    )
                    # Correct: overwrite local with actual
                    rm.record_position_notional(ex.value, symbol, a_val)
                    report["corrections"].append({**disc, "corrected_to": a_val})

        if report["discrepancies"]:
            logger.info(
                f"Reconciliation complete: {len(report['discrepancies'])} discrepancies "
                f"corrected across {report['exchanges_checked']}"
            )
        else:
            logger.debug(f"Reconciliation clean: {report['exchanges_checked']}")

        self._last_report = report
        return report
