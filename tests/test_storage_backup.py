"""backup() must produce a consistent, openable snapshot (VACUUM INTO path)."""
import asyncio
import sqlite3

from data.storage import DataStorage


def test_backup_produces_consistent_snapshot(tmp_path):
    db_path = str(tmp_path / "t.db")

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        await st.store_log("s1", "INFO", "hello")
        path = await st.backup()
        await st.close()
        return path

    bak = asyncio.run(scenario())
    con = sqlite3.connect(bak)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        n = con.execute("SELECT COUNT(*) FROM strategy_logs").fetchone()[0]
        assert n == 1
    finally:
        con.close()


# ── Tick write batching ───────────────────────────────────────────────────────

def _tick_count(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    finally:
        con.close()


def test_ticks_buffer_until_row_threshold(tmp_path):
    db_path = str(tmp_path / "t.db")

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        for i in range(st.TICK_FLUSH_ROWS - 1):
            await st.store_tick("binance", "BTC-USDT", 1000.0 + i, 1.0, 2.0, 1.5)
        buffered = len(st._tick_buf)          # not yet flushed
        await st.store_tick("binance", "BTC-USDT", 2000.0, 1.0, 2.0, 1.5)
        flushed_buf = len(st._tick_buf)       # row threshold hit → flushed
        await st.close()
        return buffered, flushed_buf

    buffered, flushed_buf = asyncio.run(scenario())
    assert buffered == DataStorage.TICK_FLUSH_ROWS - 1
    assert flushed_buf == 0
    assert _tick_count(db_path) == DataStorage.TICK_FLUSH_ROWS


def test_close_flushes_pending_ticks(tmp_path):
    db_path = str(tmp_path / "t.db")

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        await st.store_tick("okx", "ETH-USDT", 1.0, 10.0, 11.0, 10.5)
        await st.close()   # only 1 buffered row — close must persist it

    asyncio.run(scenario())
    assert _tick_count(db_path) == 1


def test_backup_includes_buffered_ticks(tmp_path):
    db_path = str(tmp_path / "t.db")

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        await st.store_tick("okx", "ETH-USDT", 1.0, 10.0, 11.0, 10.5)
        bak = await st.backup()   # must flush first
        await st.close()
        return bak

    bak = asyncio.run(scenario())
    assert _tick_count(bak) == 1


def test_timer_flush_writes_within_interval(tmp_path):
    db_path = str(tmp_path / "t.db")

    async def scenario():
        st = DataStorage(db_path)
        st.TICK_FLUSH_INTERVAL_S = 0.05
        await st.connect()
        await st.store_tick("okx", "ETH-USDT", 1.0, 10.0, 11.0, 10.5)
        await asyncio.sleep(0.2)              # timer fires
        count_before_close = _tick_count(db_path)
        await st.close()
        return count_before_close

    assert asyncio.run(scenario()) == 1


# ── Precious-data export ──────────────────────────────────────────────────────

def test_export_precious_contains_irreplaceable_tables(tmp_path):
    db_path = str(tmp_path / "t.db")
    out_dir = str(tmp_path / "backups")

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        await st.store_trade("s1", "binance", "BTC-USDT", "buy", "market", 1.0, 100.0)
        await st.store_tick("binance", "BTC-USDT", 1.0, 1.0, 2.0, 1.5)  # NOT precious
        path = await st.export_precious(out_dir)
        await st.close()
        return path

    path = asyncio.run(scenario())
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "trades" in tables and "strategy_pnl" in tables
        assert "ticks" not in tables and "ohlcv" not in tables   # bulky/reproducible
        assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    finally:
        con.close()


def test_export_precious_prunes_old_exports(tmp_path):
    db_path = str(tmp_path / "t.db")
    out = tmp_path / "backups"
    out.mkdir()
    for i in range(5):
        (out / f"precious-2026010{i}-000000.db").touch()

    async def scenario():
        st = DataStorage(db_path)
        await st.connect()
        await st.export_precious(str(out), keep=3)
        await st.close()

    asyncio.run(scenario())
    remaining = sorted(p.name for p in out.glob("precious-*.db"))
    assert len(remaining) == 3
    # newest survive: the 2 most-recent placeholders + the fresh export
    assert remaining[0] == "precious-20260103-000000.db"
    assert remaining[1] == "precious-20260104-000000.db"
    assert remaining[2].startswith("precious-2026")
