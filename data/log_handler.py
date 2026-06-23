"""
Async-safe logging handler that persists strategy log records to SQLite.

Usage:
    from data.log_handler import StrategyDBHandler
    handler = StrategyDBHandler(storage, strategy_id="arb_spread")
    logger.addHandler(handler)
"""
from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.storage import DataStorage


class StrategyDBHandler(logging.Handler):
    """Logging handler that writes records to the strategy_logs DB table.

    Must be attached to an async event loop.  Records are dispatched via
    `asyncio.create_task`, so the handler never blocks the caller.
    """

    def __init__(self, storage: "DataStorage", strategy_id: str, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self._storage = storage
        self._strategy_id = strategy_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    self._storage.store_log(self._strategy_id, record.levelname, msg),
                    loop=loop,
                )
        except Exception:
            self.handleError(record)
