"""Fetch the current Russell 3000 constituents from the iShares IWV ETF
holdings CSV and save to config/russell3000.json.

The iShares Russell 3000 ETF (IWV) tracks the Russell 3000 index. The
holdings CSV is published daily by BlackRock at a stable URL:
    https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund

The CSV has 9-10 header rows (fund metadata) followed by a column-header
row and the holdings table. We filter to Asset Class == 'Equity' and
strip non-stock instruments (cash, futures, illiquid OTC names with
malformed tickers).

Usage:
    python scripts/fetch_russell3000.py
    python scripts/fetch_russell3000.py --out config/russell3000.json

Output JSON shape:
    {
      "source": "<url>",
      "fetched_at": "2026-05-11T...+00:00",
      "n_tickers": 2900,
      "tickers": ["A", "AAPL", ...]
    }
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IWV_URL = (
    "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
)
USER_AGENT = (
    "trading-research-bot/1.0 (+https://github.com/NZ1979/trading-model-llm) "
    "Mozilla/5.0"
)
# Polygon-compatible symbol: 1-5 uppercase letters, optionally one '.'+1-2 letter
# suffix for class shares (BRK.B, BF.B). Reject anything else.
TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z]{1,2})?$")


def _fetch_csv(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _find_header_row(rows: list[list[str]]) -> int:
    """Return the index of the column-header row (the one starting with 'Ticker')."""
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "ticker":
            return i
    raise ValueError("Could not locate header row starting with 'Ticker'")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", type=Path, default=Path("config/russell3000.json"))
    p.add_argument("--source-url", default=IWV_URL)
    args = p.parse_args()

    print(f"Fetching iShares IWV holdings from {args.source_url[:80]}...")
    try:
        csv_text = _fetch_csv(args.source_url)
    except Exception as exc:
        print(f"ERROR: fetch failed: {exc}", file=sys.stderr)
        return 1

    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        print("ERROR: empty CSV", file=sys.stderr)
        return 1

    try:
        header_idx = _find_header_row(rows)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"  First 5 rows: {rows[:5]}", file=sys.stderr)
        return 1

    headers = [h.strip() for h in rows[header_idx]]
    body = rows[header_idx + 1:]
    print(f"Found header at row {header_idx}; columns: {headers[:8]}...")
    print(f"Holdings rows: {len(body)}")

    try:
        ticker_col = headers.index("Ticker")
    except ValueError:
        print(f"ERROR: 'Ticker' column not in headers: {headers}", file=sys.stderr)
        return 1
    try:
        asset_col = headers.index("Asset Class")
    except ValueError:
        asset_col = None
        print("WARNING: no 'Asset Class' column; including all rows that look like tickers")

    kept = []
    rejected_non_equity = 0
    rejected_bad_ticker = 0
    for row in body:
        if len(row) <= ticker_col:
            continue
        ticker = row[ticker_col].strip()
        if not ticker:
            continue
        if asset_col is not None and len(row) > asset_col:
            if row[asset_col].strip().lower() != "equity":
                rejected_non_equity += 1
                continue
        if not TICKER_RE.match(ticker):
            rejected_bad_ticker += 1
            continue
        kept.append(ticker)

    tickers = sorted(set(kept))

    if len(tickers) < 2500 or len(tickers) > 3200:
        print(f"WARNING: expected 2500-3200 tickers, got {len(tickers)}. "
              f"iShares IWV typically holds ~2900-3000.", file=sys.stderr)

    out_payload = {
        "source": args.source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "n_tickers": len(tickers),
        "n_rejected_non_equity": rejected_non_equity,
        "n_rejected_bad_ticker": rejected_bad_ticker,
        "tickers": tickers,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out_payload, f, indent=2)

    print(f"Saved {len(tickers)} tickers to {args.out}")
    print(f"  Rejected non-equity:  {rejected_non_equity}")
    print(f"  Rejected bad-ticker:  {rejected_bad_ticker}")
    print(f"  First 5: {tickers[:5]}")
    print(f"  Last 5:  {tickers[-5:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
