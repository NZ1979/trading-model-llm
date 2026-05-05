"""One-shot manual trigger for the Finnhub earnings calendar refresh.

Use case: weekends, or any time the daily-routine's weekday gate prevents
the scheduled refresh from running. Reads watchlist from
`config/settings.yaml`, the API key from the `FINNHUB_API_KEY` env var,
and writes to the project's SQLite DB (whichever path is in
`storage.db_path`).

Idempotent: re-running adds 0 rows on the second call (UNIQUE constraint
on the catalysts table).

Run on the VPS:
    set -a
    . /etc/trading-platform/env
    set +a
    cd /opt/trader/app
    /opt/trader/.venv/bin/python scripts/manual_trigger_earnings_refresh.py

Expected output:
    Manual Finnhub earnings calendar refresh
      watchlist: 503 symbols
      db: /opt/trader/app/trading.db
      api key: ...<last4>
    Finnhub earnings refresh: <N> events fetched, <M> in watchlist, <K> new rows persisted (<from>..<to>)
    Done: <K> new rows persisted to catalysts table
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure we can import `data.finnhub_feed` regardless of where the script
# is invoked from. The script lives in <project_root>/scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from data.finnhub_feed import FinnhubClient, refresh_earnings_calendar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def main() -> int:
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        print("FAIL: FINNHUB_API_KEY env var not set.")
        print("Run: set -a; . /etc/trading-platform/env; set +a; "
              "then re-invoke this script")
        return 1

    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not config_path.exists():
        print(f"FAIL: settings.yaml not found at {config_path}")
        return 1

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    watchlist = {s.upper() for s in cfg.get("watchlist", [])}
    if not watchlist:
        print("FAIL: watchlist is empty in settings.yaml")
        return 1

    db_path = PROJECT_ROOT / cfg["storage"]["db_path"]

    print("Manual Finnhub earnings calendar refresh")
    print(f"  watchlist: {len(watchlist)} symbols")
    print(f"  db: {db_path}")
    print(f"  api key: ...{api_key[-4:]}")
    print()

    client = FinnhubClient(api_key)
    await client.__aenter__()
    try:
        added = await refresh_earnings_calendar(
            client, watchlist, db_path, days_forward=14
        )
    finally:
        await client.__aexit__(None, None, None)

    print()
    print(f"Done: {added} new rows persisted to catalysts table")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
