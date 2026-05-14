"""Full DB diagnostic: journal mode, table list, row count per table, plus
the actual file path the sqlite connection resolves to.

Run from C:\\trading\\LLM model (any shell with .venv):
    python scripts/dump_db_state.py
"""
import os
import sqlite3
from pathlib import Path

DB = "trading.db"


def main() -> None:
    abs_path = Path(DB).resolve()
    print(f"DB path arg     : {DB}")
    print(f"CWD             : {os.getcwd()}")
    print(f"Resolved abspath: {abs_path}")
    print(f"File exists     : {abs_path.exists()}")
    if abs_path.exists():
        st = abs_path.stat()
        print(f"File size bytes : {st.st_size}")
        print(f"Mtime           : {st.st_mtime}")
    print()

    conn = sqlite3.connect(DB)
    print(f"sqlite version  : {sqlite3.sqlite_version}")
    print(f"journal_mode    : {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"wal_autocheckpoint: {conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0]}")
    print()

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print("Per-table row counts:")
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<22} {n}")
    print()

    # Newest 5 rows in decisions (whatever they look like)
    print("Newest 5 rows in decisions (all setups):")
    rows = conn.execute(
        "SELECT id, datetime(ts,'unixepoch','-4 hours') AS et, ticker, action, setup "
        "FROM decisions ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if not rows:
        print("  (none)")
    else:
        for r in rows:
            print(f"  {r}")


if __name__ == "__main__":
    main()
