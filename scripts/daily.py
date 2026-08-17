"""Daily bars for multi-day context.

    python -m scripts.daily SNDK
    python -m scripts.daily SNDK --days 60 --level 1800

Everything else in this project is INTRADAY. That gap produced a real error on
2026-08-17: an 84-minute sample was used to test whether strike 1800 was
attracting SNDK, when the stock had been climbing toward 1800 from below for
weeks. At that timescale directional drift dominates, so the test could not
separate trend from attraction, and its negative result was reported as
though it had. A claim about days needs days.

--level marks sessions that touched a price and reports how long it has been
since, which is the question that error turned on.

Alpaca SIP daily bars. Read-only, no credentials beyond the usual env vars.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data.alpaca_rest import AlpacaRESTClient  # noqa: E402

ET = ZoneInfo("America/New_York")


async def run(symbol: str, days: int, level: float | None) -> int:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    async with AlpacaRESTClient.from_env() as client:
        bars = await client.bars(symbol, timeframe="1Day",
                                 limit=days + 10, start=str(start))
    if not bars:
        print(f"no daily bars for {symbol}", file=sys.stderr)
        return 1

    print(f"{symbol}  {len(bars)} sessions from {start}")
    print(f"{'date':<12}{'open':>10}{'high':>10}{'low':>10}{'close':>10}"
          f"{'chg%':>8}{'volume':>14}  ")
    prev = None
    touched_high = touched_close = None
    for b in bars:
        # Alpaca stamps a daily bar at 04:00 UTC of the session date's
        # midnight ET. Convert in ET or the label slips a day.
        d = datetime.fromtimestamp(b.ts_ns / 1e9, tz=timezone.utc).astimezone(
            ET).date()
        chg = ((b.close - prev) / prev * 100) if prev else None
        mark = ""
        if level is not None:
            if b.high is not None and b.high >= level:
                touched_high = d
                mark += " H"
            if b.close is not None and b.close >= level:
                touched_close = d
                mark += "C"
        print(f"{str(d):<12}{b.open:>10,.2f}{b.high:>10,.2f}{b.low:>10,.2f}"
              f"{b.close:>10,.2f}"
              f"{(f'{chg:+.2f}' if chg is not None else '   n/a'):>8}"
              f"{b.volume:>14,}{mark}")
        prev = b.close

    if level is not None:
        last = datetime.fromtimestamp(bars[-1].ts_ns / 1e9,
                                      tz=timezone.utc).astimezone(ET).date()
        print(f"\nlevel {level:,.2f}:")
        if touched_high:
            n = sum(1 for b in bars
                    if datetime.fromtimestamp(b.ts_ns / 1e9,
                                              tz=timezone.utc).astimezone(
                        ET).date() > touched_high)
            print(f"  last session whose HIGH reached it:  {touched_high}"
                  f"  ({n} sessions ago)")
        else:
            print(f"  never reached in this window")
        if touched_close:
            n = sum(1 for b in bars
                    if datetime.fromtimestamp(b.ts_ns / 1e9,
                                              tz=timezone.utc).astimezone(
                        ET).date() > touched_close)
            print(f"  last session that CLOSED above it:   {touched_close}"
                  f"  ({n} sessions ago)")
        else:
            print(f"  never closed above it in this window")
        print(f"  most recent session in window: {last}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?", default="SNDK")
    ap.add_argument("--days", type=int, default=45,
                    help="calendar days back (not sessions)")
    ap.add_argument("--level", type=float, default=None,
                    help="mark sessions touching this price and report how "
                         "many sessions since")
    a = ap.parse_args()
    return asyncio.run(run(a.symbol.upper(), a.days, a.level))


if __name__ == "__main__":
    raise SystemExit(main())
