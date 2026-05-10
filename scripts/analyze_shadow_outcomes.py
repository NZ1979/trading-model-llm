"""Quick post-backfill analysis of shadow_outcomes.

Pulls aggregates from trading.db's shadow_outcomes table and prints them
in human-readable form. Designed to be run after
scripts/backfill_shadow_outcomes.py has populated rows.

Usage:
    cd /opt/trader/app
    PYTHONPATH=/opt/trader/app /opt/trader/.venv/bin/python scripts/analyze_shadow_outcomes.py

    # or locally:
    cd "C:\\trading\\LLM model"
    $env:PYTHONPATH = "."
    python scripts/analyze_shadow_outcomes.py --db-path trading.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--db-path", default="trading.db",
        help="Path to trading.db. Default: trading.db (relative to cwd)",
    )
    args = p.parse_args()

    db = Path(args.db_path)
    if not db.exists():
        print(f"ERROR: {db} not found")
        return 1
    conn = sqlite3.connect(db)

    # Row count
    (total,) = conn.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()
    print(f"=== shadow_outcomes row count: {total} ===")
    if total == 0:
        print("Run scripts/backfill_shadow_outcomes.py first.")
        return 0

    # By first_touch
    print("\n=== by first_touch (count, avg eod, avg MAE, avg MFE) ===")
    rows = conn.execute(
        "SELECT first_touch, COUNT(*), "
        "ROUND(AVG(return_eod_pct), 2), "
        "ROUND(AVG(mae_pct), 2), "
        "ROUND(AVG(mfe_pct), 2) "
        "FROM shadow_outcomes "
        "GROUP BY first_touch "
        "ORDER BY COUNT(*) DESC"
    ).fetchall()
    print(f"  {'touch':<10} {'n':>4} {'avg_eod%':>10} {'avg_mae%':>10} {'avg_mfe%':>10}")
    for touch, n, eod, mae, mfe in rows:
        print(f"  {touch:<10} {n:>4} {eod:>10} {mae:>10} {mfe:>10}")

    # By ticker (top 10 by count)
    print("\n=== by ticker (top 10 by trade count) ===")
    rows = conn.execute(
        "SELECT d.ticker, COUNT(*), "
        "ROUND(AVG(so.return_eod_pct), 2), "
        "ROUND(AVG(so.mfe_pct), 2), "
        "ROUND(AVG(so.mae_pct), 2), "
        "SUM(CASE WHEN so.first_touch='target' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN so.first_touch='stop' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN so.first_touch='neither' THEN 1 ELSE 0 END) "
        "FROM shadow_outcomes so "
        "JOIN decisions d ON d.id = so.decision_id "
        "GROUP BY d.ticker "
        "ORDER BY COUNT(*) DESC "
        "LIMIT 10"
    ).fetchall()
    print(f"  {'ticker':<8} {'n':>3} {'avg_eod':>8} {'avg_mfe':>8} {'avg_mae':>8} {'tgt':>4} {'stp':>4} {'neit':>5}")
    for ticker, n, eod, mfe, mae, tgt, stp, neit in rows:
        print(f"  {ticker:<8} {n:>3} {eod:>8} {mfe:>8} {mae:>8} {tgt:>4} {stp:>4} {neit:>5}")

    # By trade date (post-decision performance per day)
    print("\n=== by date (US Eastern) ===")
    rows = conn.execute(
        "SELECT DATE(d.ts, 'unixepoch', '-4 hours') as et_date, "
        "COUNT(*), "
        "ROUND(AVG(so.return_eod_pct), 2), "
        "SUM(CASE WHEN so.return_eod_pct > 0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN so.first_touch='target' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN so.first_touch='stop' THEN 1 ELSE 0 END) "
        "FROM shadow_outcomes so "
        "JOIN decisions d ON d.id = so.decision_id "
        "GROUP BY et_date "
        "ORDER BY et_date"
    ).fetchall()
    print(f"  {'date':<12} {'n':>3} {'avg_eod':>8} {'wins':>5} {'tgt':>4} {'stp':>4}")
    for et_date, n, eod, wins, tgt, stp in rows:
        print(f"  {et_date:<12} {n:>3} {eod:>8} {wins:>5} {tgt:>4} {stp:>4}")

    # Realized-R proxy: -1 for stops, +1.33 for targets, eod_return/stop_dist for neither.
    # We need stop_price from orders to compute proper R for the neither bucket.
    print("\n=== expectancy R proxy ===")
    rows = conn.execute(
        "SELECT first_touch, COUNT(*), "
        "ROUND(SUM(CASE "
        "  WHEN first_touch='stop' THEN -1.0 "
        "  WHEN first_touch='target' THEN 1.333 "
        "  ELSE 0.0 END) "
        "+ SUM(CASE WHEN first_touch NOT IN ('stop','target') "
        "  THEN return_eod_pct / NULLIF((SELECT ABS(o.limit_price - o.stop_price)/o.limit_price*100 FROM orders o WHERE o.decision_id=shadow_outcomes.decision_id), 0) "
        "  ELSE 0 END), 2) as total_r "
        "FROM shadow_outcomes "
        "GROUP BY first_touch"
    ).fetchall()
    print(f"  {'touch':<10} {'n':>4} {'total_r':>10}")
    for touch, n, total_r in rows:
        print(f"  {touch:<10} {n:>4} {total_r!s:>10}")

    # Net expectancy across all decisions
    row = conn.execute(
        "SELECT COUNT(*), "
        "ROUND(SUM(CASE "
        "  WHEN first_touch='stop' THEN -1.0 "
        "  WHEN first_touch='target' THEN 1.333 "
        "  ELSE return_eod_pct / NULLIF((SELECT ABS(o.limit_price - o.stop_price)/o.limit_price*100 FROM orders o WHERE o.decision_id=shadow_outcomes.decision_id), 0) "
        "  END), 2), "
        "ROUND(AVG(CASE "
        "  WHEN first_touch='stop' THEN -1.0 "
        "  WHEN first_touch='target' THEN 1.333 "
        "  ELSE return_eod_pct / NULLIF((SELECT ABS(o.limit_price - o.stop_price)/o.limit_price*100 FROM orders o WHERE o.decision_id=shadow_outcomes.decision_id), 0) "
        "  END), 3) "
        "FROM shadow_outcomes"
    ).fetchone()
    n_total, r_total, r_avg = row
    print(f"\n  net R across {n_total} decisions: total={r_total}, mean per trade={r_avg}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
