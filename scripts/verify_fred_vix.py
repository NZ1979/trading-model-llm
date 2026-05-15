"""Verify the FRED VIX loader against the real St. Louis Fed API.

Pulls the last ~30 calendar days of VIX daily closes and prints a tiny
summary (row count, date range, latest close). This is the Rule-14
verification gate for ``data/fred_vix.py``: until this script runs
end-to-end against the live FRED endpoint, the module is "code on
disk, not yet validated."

Run on Godzilla (the LLM-model workstation), NOT on the VPS — Rule 26
keeps LLM-model work off the gap-and-go infrastructure.

Usage (PowerShell, on Godzilla):

    cd "C:\\trading\\LLM model"
    .\\.venv\\Scripts\\Activate.ps1
    $env:FRED_API_KEY = '<your-fred-key>'
    python scripts/verify_fred_vix.py

The script makes exactly one FRED REST call. FRED_API_KEY travels via
env var only; the script never echoes the key per Rule 21. Per Rule
22, the httpx logger is set to WARNING at startup and any traceback
that surfaces is scrubbed through the apiKey/api_key regex before
hitting stderr.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Rule 22 defense-in-depth: scrub api_key from anything that surfaces to
# stderr before httpx's WARNING-level suppression catches it. Mirrors
# scripts/verify_regime.py.
# ---------------------------------------------------------------------------

_APIKEY_TRACEBACK_RE = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def _scrub_traceback(s: str) -> str:
    return _APIKEY_TRACEBACK_RE.sub(r"\1<redacted>", s)


# Suppress httpx INFO logging — it logs full request URLs which contain
# the FRED api_key query param. The httpx logger suppression is global
# for this process; tests and other scripts can override.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# Make `from data...` work when running as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.fred_vix import get_vix_history  # noqa: E402


async def _main_async() -> int:
    if not os.environ.get("FRED_API_KEY"):
        print(
            "FRED_API_KEY not set. From PowerShell:\n"
            "    $env:FRED_API_KEY = '<your-key>'\n"
            "Sign up at https://fred.stlouisfed.org/docs/api/api_key.html",
            file=sys.stderr,
        )
        return 2

    today = date.today()
    start = today - timedelta(days=30)
    print(f"Fetching VIX history {start.isoformat()} -> {today.isoformat()}...")

    df = await get_vix_history(start, today)

    print()
    print(f"Rows: {len(df)}")
    print(f"Date range: {df.index.min().date()} -> {df.index.max().date()}")
    print(f"Latest close: {float(df['vix_close'].iloc[-1]):.2f} "
          f"on {df.index[-1].date()}")
    print(f"Min over window: {float(df['vix_close'].min()):.2f}")
    print(f"Max over window: {float(df['vix_close'].max()):.2f}")
    print(f"Mean over window: {float(df['vix_close'].mean()):.2f}")
    print()
    print("First 5 rows:")
    print(df.head().to_string())
    print()
    print("Last 5 rows:")
    print(df.tail().to_string())
    return 0


def main() -> int:
    try:
        return asyncio.run(_main_async())
    except Exception:
        # Scrub any apiKey/api_key value from the traceback before it
        # hits stderr. Rule 22.
        tb = traceback.format_exc()
        print(_scrub_traceback(tb), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
