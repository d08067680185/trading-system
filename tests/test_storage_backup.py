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
