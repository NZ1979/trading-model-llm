"""One-shot manual trigger for the dynamic watchlist build (Phase B).

Use case: validate the live Wikipedia + Polygon flow end-to-end without
waiting for the next 08:30 ET scheduled refresh, OR seed the dynamic
watchlist file ahead of a service restart so the service uses the new
list immediately.

Reads `POLYGON_API_KEY` from env, writes
`<project_root>/config/watchlist_dynamic.json`. Idempotent: re-running
overwrites the file with the latest data.

Run on the VPS:
    set -a
    . /etc/trading-platform/env
    set +a
    cd /opt/trader/app
    /opt/trader/.venv/bin/python scripts/manual_build_watchlist.py

Expected output:
    Manual dynamic watchlist build
      polygon api key: ...<last4>
      output path: /opt/trader/app/config/watchlist_dynamic.json
    <log lines from data.watchlist_builder showing source counts, ADV fetch,
     and final write>
    Done: dynamic watchlist refreshed (success=True)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure we can import `data.watchlist_builder` regardless of where the
# script is invoked from. The script lives in <project_root>/scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.watchlist_builder import refresh_dynamic_watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def main() -> int:
    polygon_key = os.environ.get("POLYGON_API_KEY")
    if not polygon_key:
        print("FAIL: POLYGON_API_KEY env var not set.")
        print("Run: set -a; . /etc/trading-platform/env; set +a; "
              "then re-invoke this script")
        return 1

    output_path = PROJECT_ROOT / "config" / "watchlist_dynamic.json"

    print("Manual dynamic watchlist build")
    print(f"  polygon api key: ...{polygon_key[-4:]}")
    print(f"  output path: {output_path}")
    print()

    success = await refresh_dynamic_watchlist(
        polygon_key, output_path, top_n=500
    )

    print()
    print(f"Done: dynamic watchlist refreshed (success={success})")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
