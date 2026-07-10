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
