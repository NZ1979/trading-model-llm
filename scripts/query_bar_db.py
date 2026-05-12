"""Quick sanity / exploration queries against the local bar DB.

Usage:
    python scripts/query_bar_db.py                     # default summary
    python scripts/query_bar_db.py --base D:\trading_data
    python scripts/query_bar_db.py --base D:\trading_data --ticker AAPL

Reads partitioned Parquet at <base>/bars/1min/**/*.parquet via DuckDB.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base", type=Path, default=Path("D:/trading_data"),
                   help="Database root (default D:/trading_data)")
    p.add_argument("--ticker", default=None,
                   help="If set, show a sample of bars for this ticker")
    p.add_argument("--smoke", action="store_true",
                   help="Query 1min_smoke instead of 1min")
    args = p.parse_args()

    glob = str(args.base / "bars" / ("1min_smoke" if args.smoke else "1min")
               / "**" / "*.parquet").replace("\\", "/")
    print(f"glob: {glob}")
    con = duckdb.connect()

    # Summary
    r = con.sql(
        f"SELECT COUNT(*) AS rows, COUNT(DISTINCT ticker) AS tickers, "
        f"MIN(ts) AS earliest, MAX(ts) AS latest "
        f"FROM read_parquet('{glob}')"
    ).fetchone()
    print(f"\n=== Summary ===")
    print(f"  rows:     {r[0]:,}")
    print(f"  tickers:  {r[1]}")
    print(f"  earliest: {r[2]}")
    print(f"  latest:   {r[3]}")

    # Top tickers by bar count
    print(f"\n=== Top 10 tickers by bar count ===")
    rows = con.sql(
        f"SELECT ticker, COUNT(*) AS bars "
        f"FROM read_parquet('{glob}') GROUP BY ticker ORDER BY bars DESC LIMIT 10"
    ).fetchall()
    for ticker, bars in rows:
        print(f"  {ticker:<8} {bars:>10,}")

    # Bottom tickers by bar count
    print(f"\n=== Bottom 5 tickers by bar count ===")
    rows = con.sql(
        f"SELECT ticker, COUNT(*) AS bars "
        f"FROM read_parquet('{glob}') GROUP BY ticker ORDER BY bars ASC LIMIT 5"
    ).fetchall()
    for ticker, bars in rows:
        print(f"  {ticker:<8} {bars:>10,}")

    # Ticker-specific sample
    if args.ticker:
        print(f"\n=== {args.ticker} latest 5 bars ===")
        rows = con.sql(
            f"SELECT ts, open, high, low, close, volume "
            f"FROM read_parquet('{glob}') WHERE ticker = '{args.ticker.upper()}' "
            f"ORDER BY ts DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            print(f"  {r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
