#!/usr/bin/env python3
"""
Emergency halt script — stops all trading without needing the UI or HTTP server.

Usage:
    python scripts/emergency_halt.py             # halt via SIGUSR1
    python scripts/emergency_halt.py --db-only   # halt via DB flag only (if process hangs)
    python scripts/emergency_halt.py --status    # check current halt state
    python scripts/emergency_halt.py --resume    # remove DB halt flag
"""
import argparse
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trading_data.db"
PID_FILE = Path("/tmp/trading_system.pid")


def find_pid() -> int | None:
    """Find the running trading system process by name."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
    # Fallback: scan process list
    try:
        import subprocess
        out = subprocess.check_output(["pgrep", "-f", "main.py"], text=True)
        pids = [int(p) for p in out.strip().split() if p]
        return pids[0] if pids else None
    except Exception:
        return None


def halt_via_signal(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGUSR1)
        print(f"✓ SIGUSR1 sent to PID {pid} — trading halted")
        return True
    except ProcessLookupError:
        print(f"✗ Process {pid} not found")
        return False
    except PermissionError:
        print(f"✗ Permission denied sending signal to PID {pid}")
        return False


def halt_via_db(reason: str = "emergency_halt_script") -> None:
    """Write a halt flag directly to the database."""
    if not DB_PATH.exists():
        print(f"✗ DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS halt_flags (
            id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            ts REAL NOT NULL
        )
    """)
    conn.execute("DELETE FROM halt_flags")
    conn.execute("INSERT INTO halt_flags(reason, ts) VALUES (?, ?)", (reason, time.time()))
    conn.commit()
    conn.close()
    print(f"✓ Halt flag written to DB: '{reason}'")


def clear_db_halt() -> None:
    if not DB_PATH.exists():
        print(f"✗ DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DROP TABLE IF EXISTS halt_flags")
    conn.commit()
    conn.close()
    print("✓ DB halt flag cleared")


def check_status() -> None:
    pid = find_pid()
    print(f"Process: {'PID ' + str(pid) if pid else 'NOT RUNNING'}")
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute("SELECT reason, ts FROM halt_flags ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                print(f"DB halt flag: '{row[0]}' at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row[1]))}")
            else:
                print("DB halt flag: none")
        except Exception:
            print("DB halt flag: none (table not found)")
        conn.close()
    else:
        print(f"DB: not found at {DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Emergency trading halt")
    parser.add_argument("--db-only", action="store_true", help="Only write DB flag, don't send signal")
    parser.add_argument("--status",  action="store_true", help="Show current status")
    parser.add_argument("--resume",  action="store_true", help="Remove DB halt flag")
    parser.add_argument("--reason",  default="emergency_halt_script", help="Halt reason to record")
    args = parser.parse_args()

    if args.status:
        check_status()
        return

    if args.resume:
        clear_db_halt()
        return

    print("═══ EMERGENCY HALT ════════════════════════════════")
    halt_via_db(args.reason)

    if not args.db_only:
        pid = find_pid()
        if pid:
            halt_via_signal(pid)
        else:
            print("⚠ Trading process not found — DB flag set, will apply on next startup")

    print("═══════════════════════════════════════════════════")
    print("To resume: POST /api/risk/resume  OR  python scripts/emergency_halt.py --resume")


if __name__ == "__main__":
    main()
