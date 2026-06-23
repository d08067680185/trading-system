"""
SQLite-based storage for OHLCV candles, live ticks, and trades.
All writes are async via aiosqlite.
"""
from __future__ import annotations
import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger("DataStorage")

DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    ts          INTEGER NOT NULL,   -- unix seconds (candle open time)
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    UNIQUE(exchange, symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_lookup ON ohlcv(exchange, symbol, interval, ts);

CREATE TABLE IF NOT EXISTS ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    bid         REAL,
    ask         REAL,
    last        REAL,
    volume_24h  REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_lookup ON ticks(exchange, symbol, ts);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     TEXT,
    exchange        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    order_type      TEXT    NOT NULL,
    quantity        REAL    NOT NULL,
    price           REAL    NOT NULL,
    fee             REAL    DEFAULT 0,
    order_id        TEXT,
    status          TEXT    NOT NULL,
    ts              REAL    NOT NULL DEFAULT (unixepoch('now','subsec'))
);
CREATE INDEX IF NOT EXISTS idx_trades_ts           ON trades(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_ts  ON trades(strategy_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_exchange_sym ON trades(exchange, symbol, ts DESC);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    total_usdt  REAL    NOT NULL,
    daily_pnl   REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts DESC);

CREATE TABLE IF NOT EXISTS strategy_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    strategy_id TEXT    NOT NULL,
    level       TEXT    NOT NULL,
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_strategy_ts ON strategy_logs(strategy_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_ts           ON strategy_logs(ts DESC);

CREATE TABLE IF NOT EXISTS strategy_pnl (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT    NOT NULL,
    date        TEXT    NOT NULL,        -- UTC date YYYY-MM-DD
    daily_pnl   REAL    NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    cumulative_pnl REAL NOT NULL DEFAULT 0,
    UNIQUE(strategy_id, date)
);
CREATE INDEX IF NOT EXISTS idx_strategy_pnl ON strategy_pnl(strategy_id, date DESC);

CREATE TABLE IF NOT EXISTS funding_rates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    rate        REAL    NOT NULL,
    next_funding_time INTEGER,
    annualized_pct REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_uniq ON funding_rates(exchange, symbol, ts);
CREATE INDEX IF NOT EXISTS idx_funding_lookup ON funding_rates(exchange, symbol, ts DESC);

CREATE TABLE IF NOT EXISTS pnl_attribution (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    strategy_id TEXT    NOT NULL,
    exchange    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    order_id    TEXT,
    source      TEXT    NOT NULL,  -- 'spread'|'funding'|'execution'|'fee'
    pnl_usdt    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_strategy_ts ON pnl_attribution(strategy_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_attr_ts           ON pnl_attribution(ts DESC);

CREATE TABLE IF NOT EXISTS arb_triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    strategy_id   TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    spread_bps    REAL    NOT NULL,   -- gross spread at trigger
    threshold_bps REAL    NOT NULL,
    mode          TEXT    NOT NULL,   -- 'maker2' | 'maker1' | 'taker'
    buy_exchange  TEXT,
    sell_exchange TEXT,
    outcome       TEXT    NOT NULL,   -- 'attempted'|'place_failed'|'completed'|'timeout'|'hedged'
    legs_filled   INTEGER NOT NULL DEFAULT 0,
    realized_bps  REAL,               -- net spread actually captured (completed only)
    duration_s    REAL
);
CREATE INDEX IF NOT EXISTS idx_arb_triggers_ts ON arb_triggers(ts DESC);

CREATE TABLE IF NOT EXISTS backtest_jobs (
    job_id      TEXT    PRIMARY KEY,
    strategy_id TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    params      TEXT,           -- JSON
    result      TEXT,           -- JSON
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_jobs_ts ON backtest_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS auto_actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    action    TEXT    NOT NULL,   -- 'feed_reconnect'|'connector_reconnect'|'margin_reduce'
    target    TEXT    NOT NULL,   -- exchange or exchange:symbol the action acted on
    reason    TEXT    NOT NULL,   -- why the system acted (human-readable)
    outcome   TEXT    NOT NULL,   -- 'success'|'failed'
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_actions_ts ON auto_actions(ts DESC);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass
class OHLCVRow:
    exchange: str
    symbol: str
    interval: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class DataStorage:
    def __init__(self, db_path: str = "trading_data.db"):
        self._path = db_path
        self._db = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            import aiosqlite
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._path)
            self._db.row_factory = aiosqlite.Row
            # WAL mode: readers don't block writers; better concurrent read performance
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
            await self._db.execute("PRAGMA temp_store=MEMORY")
            await self._db.executescript(DDL)
            await self._db.commit()
            logger.info(f"DataStorage connected: {self._path}")
        except ImportError:
            raise RuntimeError("aiosqlite not installed. Run: pip install aiosqlite>=0.20")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def backup(self, suffix: str = ".bak") -> str:
        """Copy the DB file to <path><suffix>. Returns backup path."""
        src = Path(self._path)
        dst = Path(str(src) + suffix)
        await asyncio.to_thread(shutil.copy2, str(src), str(dst))
        logger.info(f"DB backup written: {dst}")
        return str(dst)

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def store_ohlcv(self, rows: list[OHLCVRow]) -> int:
        """Upsert OHLCV rows. Returns number inserted."""
        if not rows:
            return 0
        async with self._lock:
            await self._db.executemany(
                """INSERT OR REPLACE INTO ohlcv
                   (exchange, symbol, interval, ts, open, high, low, close, volume)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(r.exchange, r.symbol, r.interval, r.ts,
                  r.open, r.high, r.low, r.close, r.volume) for r in rows],
            )
            await self._db.commit()
        return len(rows)

    async def get_ohlcv(
        self,
        exchange: str,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 5000,
    ) -> list[OHLCVRow]:
        q = "SELECT * FROM ohlcv WHERE exchange=? AND symbol=? AND interval=?"
        params: list = [exchange, symbol, interval]
        if start_ts:
            q += " AND ts >= ?"; params.append(start_ts)
        if end_ts:
            q += " AND ts <= ?"; params.append(end_ts)
        q += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        async with self._db.execute(q, params) as cur:
            rows = await cur.fetchall()
        return [OHLCVRow(
            exchange=r["exchange"], symbol=r["symbol"], interval=r["interval"],
            ts=r["ts"], open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"],
        ) for r in rows]

    async def get_symbols(self, exchange: Optional[str] = None) -> list[dict]:
        """Return available (exchange, symbol, interval, count, min_ts, max_ts)."""
        q = """SELECT exchange, symbol, interval, COUNT(*) as bars,
                      MIN(ts) as start_ts, MAX(ts) as end_ts
               FROM ohlcv"""
        params = []
        if exchange:
            q += " WHERE exchange=?"; params.append(exchange)
        q += " GROUP BY exchange, symbol, interval ORDER BY exchange, symbol, interval"
        async with self._db.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Ticks ─────────────────────────────────────────────────────────────────

    async def store_tick(self, exchange: str, symbol: str, ts: float,
                         bid: Optional[float], ask: Optional[float],
                         last: Optional[float], volume_24h: Optional[float] = None) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO ticks(exchange,symbol,ts,bid,ask,last,volume_24h) VALUES(?,?,?,?,?,?,?)",
                (exchange, symbol, ts, bid, ask, last, volume_24h),
            )
            await self._db.commit()

    # ── Trades ────────────────────────────────────────────────────────────────

    async def store_trade(
        self, strategy_id: str, exchange: str, symbol: str,
        side: str, order_type: str, quantity: float, price: float,
        fee: float = 0.0, order_id: str = "", status: str = "filled",
    ) -> None:
        async with self._lock:
            await self._db.execute(
                """INSERT INTO trades(strategy_id,exchange,symbol,side,order_type,
                   quantity,price,fee,order_id,status,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (strategy_id, exchange, symbol, side, order_type,
                 quantity, price, fee, order_id, status, time.time()),
            )
            await self._db.commit()

    async def get_trades(
        self,
        strategy_id: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        start_ts: Optional[float] = None,
        limit: int = 1000,
    ) -> list[dict]:
        q = "SELECT * FROM trades WHERE 1=1"
        params = []
        if strategy_id: q += " AND strategy_id=?"; params.append(strategy_id)
        if exchange:    q += " AND exchange=?";    params.append(exchange)
        if symbol:      q += " AND symbol=?";      params.append(symbol)
        if start_ts:    q += " AND ts >= ?";       params.append(start_ts)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Equity snapshots ──────────────────────────────────────────────────────

    async def store_equity(self, total_usdt: float, daily_pnl: float) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO equity_snapshots(ts,total_usdt,daily_pnl) VALUES(?,?,?)",
                (time.time(), total_usdt, daily_pnl),
            )
            await self._db.commit()

    # ── Strategy logs ─────────────────────────────────────────────────────────

    async def store_log(self, strategy_id: str, level: str, message: str) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO strategy_logs(ts,strategy_id,level,message) VALUES(?,?,?,?)",
                (time.time(), strategy_id, level.upper(), message[:2000]),
            )
            await self._db.commit()

    async def get_logs(
        self,
        strategy_id: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        q = "SELECT ts, strategy_id, level, message FROM strategy_logs WHERE 1=1"
        params: list = []
        if strategy_id: q += " AND strategy_id=?"; params.append(strategy_id)
        if level:       q += " AND level=?";       params.append(level.upper())
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        async with self._db.execute(q, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Arb trigger quality log ───────────────────────────────────────────────

    async def record_arb_trigger(
        self,
        strategy_id: str,
        symbol: str,
        spread_bps: float,
        threshold_bps: float,
        mode: str,
        buy_exchange: str,
        sell_exchange: str,
    ) -> int:
        """Insert a trigger row (outcome='attempted'); returns row id for later update."""
        async with self._lock:
            cur = await self._db.execute(
                "INSERT INTO arb_triggers"
                "(ts,strategy_id,symbol,spread_bps,threshold_bps,mode,"
                " buy_exchange,sell_exchange,outcome) "
                "VALUES(?,?,?,?,?,?,?,?,'attempted')",
                (time.time(), strategy_id, symbol, spread_bps, threshold_bps,
                 mode, buy_exchange, sell_exchange),
            )
            await self._db.commit()
            return cur.lastrowid

    async def update_arb_trigger(
        self,
        trigger_id: int,
        outcome: str,
        legs_filled: int = 0,
        realized_bps: Optional[float] = None,
        duration_s: Optional[float] = None,
    ) -> None:
        async with self._lock:
            await self._db.execute(
                "UPDATE arb_triggers SET outcome=?, legs_filled=?, realized_bps=?, duration_s=? "
                "WHERE id=?",
                (outcome, legs_filled, realized_bps, duration_s, trigger_id),
            )
            await self._db.commit()

    async def get_arb_trigger_stats(self, hours: float = 168.0) -> dict:
        """Aggregate trigger quality over the window: counts per outcome, fill rate, avg bps."""
        cutoff = time.time() - hours * 3600
        async with self._db.execute(
            "SELECT outcome, COUNT(*) n, AVG(spread_bps) avg_spread, "
            "AVG(realized_bps) avg_realized, AVG(duration_s) avg_duration "
            "FROM arb_triggers WHERE ts >= ? GROUP BY outcome", (cutoff,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        total = sum(r["n"] for r in rows)
        completed = next((r["n"] for r in rows if r["outcome"] == "completed"), 0)
        return {
            "window_hours": hours,
            "total_triggers": total,
            "completed": completed,
            "completion_rate": round(completed / total, 4) if total else None,
            "by_outcome": rows,
        }

    async def get_arb_triggers(self, limit: int = 100) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM arb_triggers ORDER BY ts DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Automated-action audit ──────────────────────────────────────────────────

    async def record_auto_action(
        self, action: str, target: str, reason: str,
        outcome: str, detail: Optional[str] = None,
    ) -> int:
        """Append an audit row for an action the system took on its own (auto-heal
        reconnect, margin auto-reduce, …). Returns the row id."""
        async with self._lock:
            cur = await self._db.execute(
                "INSERT INTO auto_actions(ts,action,target,reason,outcome,detail) "
                "VALUES(?,?,?,?,?,?)",
                (time.time(), action, target, reason, outcome, detail),
            )
            await self._db.commit()
            return cur.lastrowid

    async def get_auto_actions(self, limit: int = 100) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM auto_actions ORDER BY ts DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_equity_curve(self, limit: int = 1440) -> list[dict]:
        async with self._db.execute(
            "SELECT ts, total_usdt, daily_pnl FROM equity_snapshots ORDER BY ts DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Strategy PnL persistence ─────────────────────────────────────────────

    async def upsert_strategy_pnl(
        self, strategy_id: str, date: str,
        daily_pnl: float, trade_count: int,
    ) -> None:
        """Upsert today's PnL for a strategy. Cumulative is computed from history."""
        # Get previous cumulative PnL
        async with self._db.execute(
            """SELECT cumulative_pnl FROM strategy_pnl
               WHERE strategy_id=? AND date < ? ORDER BY date DESC LIMIT 1""",
            (strategy_id, date),
        ) as cur:
            row = await cur.fetchone()
        prev_cumulative = row[0] if row else 0.0
        cumulative = prev_cumulative + daily_pnl

        async with self._lock:
            await self._db.execute(
                """INSERT INTO strategy_pnl(strategy_id, date, daily_pnl, trade_count, cumulative_pnl)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(strategy_id, date) DO UPDATE SET
                     daily_pnl=excluded.daily_pnl,
                     trade_count=excluded.trade_count,
                     cumulative_pnl=excluded.cumulative_pnl""",
                (strategy_id, date, daily_pnl, trade_count, cumulative),
            )
            await self._db.commit()

    async def get_strategy_pnl_history(
        self, strategy_id: str, days: int = 30,
    ) -> list[dict]:
        async with self._db.execute(
            """SELECT date, daily_pnl, trade_count, cumulative_pnl
               FROM strategy_pnl WHERE strategy_id=?
               ORDER BY date DESC LIMIT ?""",
            (strategy_id, days),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_all_strategy_pnl_latest(self) -> list[dict]:
        """Return latest cumulative PnL per strategy (for restart recovery)."""
        async with self._db.execute(
            """SELECT strategy_id, date, daily_pnl, trade_count, cumulative_pnl
               FROM strategy_pnl WHERE (strategy_id, date) IN (
                 SELECT strategy_id, MAX(date) FROM strategy_pnl GROUP BY strategy_id
               )""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_strategy_summary(self) -> list[dict]:
        """Aggregate per-strategy: total trades, total PnL, best/worst day."""
        async with self._db.execute(
            """SELECT strategy_id,
                      MAX(cumulative_pnl) as total_pnl,
                      SUM(trade_count) as total_trades,
                      MAX(daily_pnl) as best_day,
                      MIN(daily_pnl) as worst_day,
                      COUNT(*) as trading_days
               FROM strategy_pnl GROUP BY strategy_id ORDER BY total_pnl DESC""",
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Funding rates ─────────────────────────────────────────────────────────

    async def store_funding_rate(
        self, exchange: str, symbol: str, ts: float,
        rate: float, next_funding_time: Optional[int] = None,
        annualized_pct: Optional[float] = None,
    ) -> None:
        async with self._lock:
            await self._db.execute(
                """INSERT OR IGNORE INTO funding_rates
                   (exchange,symbol,ts,rate,next_funding_time,annualized_pct)
                   VALUES(?,?,?,?,?,?)""",
                (exchange, symbol, ts, rate, next_funding_time, annualized_pct),
            )
            await self._db.commit()

    async def get_funding_rates(
        self,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        start_ts: Optional[float] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM funding_rates WHERE 1=1"
        params: list = []
        if exchange: q += " AND exchange=?"; params.append(exchange)
        if symbol:   q += " AND symbol=?";   params.append(symbol)
        if start_ts: q += " AND ts >= ?";    params.append(start_ts)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_funding_rate_stats(
        self, exchange: str, symbol: str, days: int = 30,
    ) -> dict:
        """Return mean, std, min, max funding rate over last N days."""
        start = time.time() - days * 86400
        async with self._db.execute(
            """SELECT AVG(rate) as mean_rate, MIN(rate) as min_rate,
                      MAX(rate) as max_rate, COUNT(*) as count,
                      AVG(annualized_pct) as mean_ann_pct
               FROM funding_rates WHERE exchange=? AND symbol=? AND ts>=?""",
            (exchange, symbol, start),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["count"]:
            return {}
        rates = [r["rate"] for r in await self.get_funding_rates(exchange, symbol, start, limit=5000)]
        if len(rates) > 1:
            mean = sum(rates) / len(rates)
            std = (sum((r - mean) ** 2 for r in rates) / (len(rates) - 1)) ** 0.5
        else:
            std = 0.0
        return {
            "exchange": exchange, "symbol": symbol, "days": days,
            "mean_rate": round(row["mean_rate"] or 0, 6),
            "mean_ann_pct": round(row["mean_ann_pct"] or 0, 2),
            "min_rate": round(row["min_rate"] or 0, 6),
            "max_rate": round(row["max_rate"] or 0, 6),
            "std_rate": round(std, 6),
            "count": row["count"],
        }

    # ── PnL attribution ───────────────────────────────────────────────────────

    async def store_attribution(
        self, strategy_id: str, exchange: str, symbol: str,
        source: str, pnl_usdt: float, order_id: str = "",
    ) -> None:
        async with self._lock:
            await self._db.execute(
                """INSERT INTO pnl_attribution
                   (ts,strategy_id,exchange,symbol,order_id,source,pnl_usdt)
                   VALUES(?,?,?,?,?,?,?)""",
                (time.time(), strategy_id, exchange, symbol, order_id, source, pnl_usdt),
            )
            await self._db.commit()

    async def get_attribution(
        self,
        strategy_id: Optional[str] = None,
        source: Optional[str] = None,
        start_ts: Optional[float] = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM pnl_attribution WHERE 1=1"
        params: list = []
        if strategy_id: q += " AND strategy_id=?"; params.append(strategy_id)
        if source:       q += " AND source=?";      params.append(source)
        if start_ts:     q += " AND ts >= ?";       params.append(start_ts)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(q, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Database maintenance ──────────────────────────────────────────────────

    async def purge_old_data(
        self,
        ticks_days: int = 7,
        logs_days: int = 30,
        attribution_days: int = 90,
        batch_rows: int = 50_000,
    ) -> dict:
        """Delete old rows to keep database size manageable.

        Deletes in batches with a commit per batch — a single multi-million-row
        DELETE holds the write lock long enough that concurrent tick writes fail
        with 'database table is locked' (and vice versa).
        """
        now = time.time()
        counts = {}
        for table, cutoff, key in (
            ("ticks", now - ticks_days * 86400, "ticks_deleted"),
            ("strategy_logs", now - logs_days * 86400, "logs_deleted"),
            ("pnl_attribution", now - attribution_days * 86400, "attribution_deleted"),
        ):
            deleted = 0
            retries = 0
            while True:
                try:
                    async with self._lock:
                        cur = await self._db.execute(
                            f"DELETE FROM {table} WHERE rowid IN "
                            f"(SELECT rowid FROM {table} WHERE ts < ? LIMIT ?)",
                            (cutoff, batch_rows),
                        )
                        await self._db.commit()
                except Exception as e:
                    if "locked" in str(e).lower() and retries < 5:
                        retries += 1
                        await asyncio.sleep(2.0 * retries)
                        continue
                    raise
                retries = 0
                deleted += cur.rowcount
                if cur.rowcount < batch_rows:
                    break
                await asyncio.sleep(0.1)  # let writers interleave between batches
            counts[key] = deleted
        try:
            async with self._lock:
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            logger.warning(f"WAL checkpoint after purge failed: {e}")
        logger.info(f"DB purge: {counts}")
        return counts

    async def get_db_size(self) -> dict:
        """Return database file size and per-table row counts."""
        path = Path(self._path)
        size_mb = path.stat().st_size / 1024 / 1024 if path.exists() else 0
        tables = {}
        for tbl in ("ohlcv", "ticks", "trades", "equity_snapshots",
                    "strategy_logs", "funding_rates", "pnl_attribution"):
            try:
                async with self._db.execute(f"SELECT COUNT(*) FROM {tbl}") as cur:
                    row = await cur.fetchone()
                tables[tbl] = row[0] if row else 0
            except Exception:
                tables[tbl] = 0
        return {"size_mb": round(size_mb, 2), "tables": tables}

    async def get_attribution_summary(self, days: int = 30) -> dict:
        start = time.time() - days * 86400
        async with self._db.execute(
            """SELECT strategy_id, source,
                      SUM(pnl_usdt) as total_pnl,
                      COUNT(*) as count
               FROM pnl_attribution WHERE ts >= ?
               GROUP BY strategy_id, source
               ORDER BY strategy_id, source""",
            (start,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        # Reshape: {strategy_id: {source: total_pnl}}
        result: dict = {}
        for row in rows:
            sid = row["strategy_id"]
            result.setdefault(sid, {})
            result[sid][row["source"]] = {
                "total_pnl": round(row["total_pnl"], 4),
                "count": row["count"],
            }
        return {"days": days, "by_strategy": result}

    async def get_stats(self) -> dict:
        """Aggregate performance stats across all trades and equity snapshots."""
        async with self._db.execute(
            """SELECT COUNT(*) as total_trades,
                      SUM(quantity * price) as total_volume,
                      SUM(fee) as total_fees,
                      MIN(ts) as first_ts,
                      MAX(ts) as last_ts
               FROM trades"""
        ) as cur:
            t = dict(await cur.fetchone())

        async with self._db.execute(
            """SELECT strategy_id,
                      COUNT(*) as trades,
                      SUM(fee) as fees,
                      SUM(quantity * price) as volume
               FROM trades GROUP BY strategy_id ORDER BY trades DESC"""
        ) as cur:
            by_strategy = [dict(r) for r in await cur.fetchall()]

        async with self._db.execute(
            "SELECT ts, total_usdt, daily_pnl FROM equity_snapshots ORDER BY ts DESC LIMIT 1"
        ) as cur:
            latest_eq = await cur.fetchone()

        async with self._db.execute(
            "SELECT total_usdt FROM equity_snapshots ORDER BY ts ASC LIMIT 1"
        ) as cur:
            first_eq = await cur.fetchone()

        async with self._db.execute(
            "SELECT MIN(total_usdt) as low, MAX(total_usdt) as high FROM equity_snapshots"
        ) as cur:
            minmax = dict(await cur.fetchone())

        current = float(latest_eq["total_usdt"]) if latest_eq else None
        initial = float(first_eq["total_usdt"]) if first_eq else None
        peak = float(minmax["high"]) if minmax["high"] else None
        trough = float(minmax["low"]) if minmax["low"] else None

        total_return_pct = ((current - initial) / initial * 100) if current and initial and initial > 0 else None
        max_drawdown_pct = ((trough - peak) / peak * 100) if peak and trough and peak > 0 else None

        return {
            "total_trades": t["total_trades"] or 0,
            "total_volume_usdt": round(t["total_volume"] or 0, 2),
            "total_fees_usdt": round(t["total_fees"] or 0, 4),
            "first_trade_ts": t["first_ts"],
            "last_trade_ts": t["last_ts"],
            "current_equity_usdt": current,
            "initial_equity_usdt": initial,
            "peak_equity_usdt": peak,
            "trough_equity_usdt": trough,
            "total_return_pct": round(total_return_pct, 2) if total_return_pct is not None else None,
            "max_drawdown_pct": round(max_drawdown_pct, 2) if max_drawdown_pct is not None else None,
            "daily_pnl_usdt": float(latest_eq["daily_pnl"]) if latest_eq else 0.0,
            "by_strategy": by_strategy,
        }

    # ── Backtest job persistence ───────────────────────────────────────────────

    async def save_backtest_job(self, job_id: str, strategy_id: str, status: str,
                                 params: dict = None, result: dict = None, error: str = None) -> None:
        import json
        await self._db.execute(
            """INSERT INTO backtest_jobs(job_id, strategy_id, created_at, status, params, result, error)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status=excluded.status, result=excluded.result, error=excluded.error""",
            (job_id, strategy_id, __import__('time').time(), status,
             json.dumps(params) if params else None,
             json.dumps(result) if result else None,
             error)
        )
        await self._db.commit()

    async def get_backtest_jobs(self, limit: int = 50) -> list[dict]:
        import json
        async with self._db.execute(
            "SELECT job_id, strategy_id, created_at, status, params, result, error "
            "FROM backtest_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(zip(["job_id","strategy_id","created_at","status","params","result","error"], r))
            d["params"] = json.loads(d["params"]) if d["params"] else {}
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out

    async def get_backtest_job(self, job_id: str) -> dict | None:
        import json
        async with self._db.execute(
            "SELECT job_id, strategy_id, created_at, status, params, result, error "
            "FROM backtest_jobs WHERE job_id=?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        d = dict(zip(["job_id","strategy_id","created_at","status","params","result","error"], row))
        d["params"] = json.loads(d["params"]) if d["params"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    async def get_setting(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )
        await self._db.commit()

    async def delete_setting(self, key: str) -> None:
        await self._db.execute("DELETE FROM settings WHERE key=?", (key,))
        await self._db.commit()
