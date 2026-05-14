"""Diagnostic: query the decisions table for LLM shadow rows.

Shows:
  - tables present in trading.db
  - total decisions row count and max id
  - count of rows where setup LIKE 'llm_shadow/%'
  - last 10 shadow rows with their ET timestamp, ticker, action, setup, confidence

Run from C:\\trading\\LLM model (any shell with the .venv activated):
    python scripts/check_shadow_rows.py

Safe to run while main.py is still writing to the DB (SQLite WAL handles
concurrent readers cleanly). No credentials touched. No network calls.
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = "trading.db"


def main() -> int:
    if not Path(DB_PATH).exists():
        print(f"ERROR: {DB_PATH} does not exist. Has main.py booted yet?")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(f"Tables in {DB_PATH}: {tables}")

    if "decisions" not in tables:
        print("ERROR: decisions table not yet created. Boot may have crashed before _init_db.")
        return 1

    total = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM decisions").fetchone()
    print(f"\nDecisions total: count={total[0]}  max_id={total[1]}")

    shadow_count = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE setup LIKE 'llm_shadow/%'"
    ).fetchone()[0]
    print(f"Shadow rows (setup LIKE 'llm_shadow/%'): {shadow_count}")

    # Action distribution among shadow rows
    if shadow_count > 0:
        print("\nShadow action distribution:")
        for row in conn.execute(
            "SELECT action, COUNT(*) AS n FROM decisions "
            "WHERE setup LIKE 'llm_shadow/%' GROUP BY action ORDER BY n DESC"
        ):
            print(f"  {row['action']:<6} {row['n']}")

        print("\nShadow setup_label distribution (top 10):")
        for row in conn.execute(
            "SELECT setup, COUNT(*) AS n FROM decisions "
            "WHERE setup LIKE 'llm_shadow/%' GROUP BY setup ORDER BY n DESC LIMIT 10"
        ):
            print(f"  {row['setup']:<40} {row['n']}")

        print("\nLatest 10 shadow rows:")
        for row in conn.execute("""
            SELECT id,
                   datetime(ts, 'unixepoch', '-4 hours') AS et,
                   ticker, action, setup, confidence, reasons
            FROM decisions
            WHERE setup LIKE 'llm_shadow/%'
            ORDER BY id DESC
            LIMIT 10
        """):
            print(f"  id={row['id']:<5} {row['et']} ET  {row['ticker']:<6} "
                  f"{row['action']:<6} setup={row['setup']:<35} "
                  f"conf={row['confidence']}  reasons={str(row['reasons'])[:80]}")

    # Sanity: any rows mirrored into the orders table from shadow decisions?
    # (Should be zero — shadow path never routes to broker.)
    if "orders" in tables:
        orders_row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print(f"\nOrders table row count: {orders_row}  (informational; "
              "should match real bracket submissions only, never shadow)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
