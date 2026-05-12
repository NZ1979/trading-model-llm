"""Fetch the current S&P 500 constituents from Wikipedia and save to
config/sp500.json. Run before building the local bar DB with
--universe sp500.

Usage:
    python scripts/fetch_sp500.py
    python scripts/fetch_sp500.py --out config/sp500.json

The output JSON shape:
    {
      "source": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
      "fetched_at": "2026-05-11T12:34:56+00:00",
      "n_tickers": 503,
      "tickers": ["AAPL", "ABBV", ...]
    }

Note on UA: Wikipedia blocks pandas's default urllib UA with 403. We use
stdlib urllib with an explicit User-Agent to avoid that.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = (
    "trading-research-bot/1.0 (+https://github.com/NZ1979/trading-model-llm) "
    "Mozilla/5.0"
)


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, default=Path("config/sp500.json"))
    p.add_argument("--source-url", default=WIKI_URL)
    args = p.parse_args()

    print(f"Fetching S&P 500 list from {args.source_url}...")
    try:
        html = _fetch_html(args.source_url)
    except Exception as exc:
        print(f"ERROR: fetch failed: {exc}", file=sys.stderr)
        return 1

    try:
        tables = pd.read_html(StringIO(html))
    except Exception as exc:
        print(f"ERROR: pd.read_html failed: {exc}", file=sys.stderr)
        return 1

    if not tables:
        print("ERROR: no tables found in HTML", file=sys.stderr)
        return 1

    df = tables[0]
    if "Symbol" not in df.columns:
        print(f"ERROR: first table has no 'Symbol' column. Columns: {list(df.columns)}",
              file=sys.stderr)
        return 1

    raw = df["Symbol"].astype(str).str.strip().tolist()
    tickers = sorted(set(t for t in raw if t and t.lower() != "nan"))

    if len(tickers) < 480 or len(tickers) > 520:
        print(f"WARNING: expected 480-520 tickers, got {len(tickers)}. "
              f"Check the source page format.", file=sys.stderr)

    out_payload = {
        "source": args.source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_tickers": len(tickers),
        "tickers": tickers,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out_payload, f, indent=2)

    print(f"Saved {len(tickers)} tickers to {args.out}")
    print(f"First 5: {tickers[:5]}")
    print(f"Last 5:  {tickers[-5:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
