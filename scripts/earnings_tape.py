"""Where an earnings move actually forms: post-market, pre-market, or regular hours.

    python -m scripts.earnings_tape BILL --dates 2025-08-28,2025-11-12,2026-02-06,2026-05-08

Why this exists
---------------
Daily bars contain no extended-hours trading, so they cannot answer the only
question that matters to someone who trades the release: does the move complete
in the first thirty minutes after the print, does it keep developing overnight,
and does the post-market level survive to the open?

Each `--dates` entry is a REACTION date (the session after an after-close
report). The tape is reconstructed from the prior session's regular hours
through the reaction day's close, bucketed by session.

Honesty about the feed
----------------------
If Alpaca returns no bars outside 09:30-16:00 ET, this script says so loudly
instead of reporting a regular-hours-only picture as though it were the whole
tape. An empty post-market bucket and an unavailable post-market feed look
identical in the output otherwise, and they mean opposite things.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data.alpaca_rest import AlpacaRESTClient  # noqa: E402

ET = ZoneInfo("America/New_York")

# (label, start_hh, start_mm, end_hh, end_mm, which_day)  day 0 = report day, 1 = reaction day
BUCKETS = [
    ("RTH before print", 9, 30, 16, 0, 0),
    ("POST +0-30min",   16, 0, 16, 30, 0),
    ("POST +30-2h",     16, 30, 18, 0, 0),
    ("POST 18:00-20:00", 18, 0, 20, 0, 0),
    ("PRE-MARKET",       4, 0, 9, 30, 1),
    ("RTH reaction day", 9, 30, 16, 0, 1),
]


def _et(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(ET)


def bucket_tape(bars, report_day, reaction_day):
    """Group bars into named session buckets. Pure; no I/O.

    Returns a list of dicts in BUCKETS order. Buckets with no bars are kept and
    marked empty rather than dropped, so a missing session is visible.
    """
    out = []
    for label, sh, sm, eh, em, which in BUCKETS:
        day = report_day if which == 0 else reaction_day
        lo = datetime(day.year, day.month, day.day, sh, sm, tzinfo=ET)
        hi = datetime(day.year, day.month, day.day, eh, em, tzinfo=ET)
        sel = [b for b in bars if lo <= _et(b.ts_ns) < hi]
        if not sel:
            out.append({"label": label, "empty": True, "bars": 0})
            continue
        out.append({
            "label": label, "empty": False, "bars": len(sel),
            "first": sel[0].open, "last": sel[-1].close,
            "high": max(b.high for b in sel), "low": min(b.low for b in sel),
            "volume": sum(b.volume for b in sel),
            "first_et": _et(sel[0].ts_ns), "last_et": _et(sel[-1].ts_ns),
        })
    return out


async def run(symbol: str, dates: list[str], timeframe: str) -> int:
    async with AlpacaRESTClient.from_env() as client:
        for ds in dates:
            reaction = datetime.strptime(ds, "%Y-%m-%d").date()
            report = reaction - timedelta(days=1)
            while report.weekday() >= 5:
                report -= timedelta(days=1)
            bars = await client.bars(
                symbol, timeframe=timeframe, limit=5000,
                start=f"{report}T12:00:00Z",
                end=f"{reaction + timedelta(days=1)}T01:00:00Z")
            if not bars:
                print(f"\n{ds}: NO BARS RETURNED", file=sys.stderr)
                continue

            ext = [b for b in bars
                   if not ((9, 30) <= (_et(b.ts_ns).hour, _et(b.ts_ns).minute) < (16, 0))]
            buckets = bucket_tape(bars, report, reaction)
            base = next((b["last"] for b in buckets
                         if b["label"] == "RTH before print" and not b["empty"]), None)

            print(f"\n{'='*74}\n{symbol}  report {report}  reaction {reaction}   "
                  f"[{timeframe}]  bars {len(bars)}, extended-hours {len(ext)}")
            if not ext:
                print("  ** NO EXTENDED-HOURS BARS RETURNED BY THE FEED **")
                print("  The post/pre buckets below are empty because the data is "
                      "absent,\n  NOT because nothing traded. Do not read them as zeros.")
            print(f"{'='*74}")
            if base:
                print(f"  baseline = close before the print: {base:,.2f}")
            print(f"\n  {'session':<19}{'first':>9}{'last':>9}{'low':>9}{'high':>9}"
                  f"{'vs base':>10}{'volume':>12}")
            for b in buckets:
                if b["empty"]:
                    print(f"  {b['label']:<19}{'— no data —':>48}")
                    continue
                rel = f"{(b['last']/base-1)*100:+.2f}%" if base else "n/a"
                print(f"  {b['label']:<19}{b['first']:>9,.2f}{b['last']:>9,.2f}"
                      f"{b['low']:>9,.2f}{b['high']:>9,.2f}{rel:>10}{b['volume']:>12,}")

            if base:
                final = next((b["last"] for b in buckets
                              if b["label"] == "RTH reaction day" and not b["empty"]), None)
                p30 = next((b["last"] for b in buckets
                            if b["label"] == "POST +0-30min" and not b["empty"]), None)
                if final and p30:
                    tot = final / base - 1
                    got = p30 / base - 1
                    share = got / tot * 100 if tot else float("nan")
                    print(f"\n  total move to reaction-day close: {tot*100:+.2f}%")
                    print(f"  captured by 16:30 ET on report day: {got*100:+.2f}%  "
                          f"= {share:.0f}% of the total")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?", default="BILL")
    ap.add_argument("--dates", required=True,
                    help="comma-separated REACTION dates, YYYY-MM-DD")
    ap.add_argument("--timeframe", default="5Min")
    a = ap.parse_args()
    return asyncio.run(run(a.symbol, [d.strip() for d in a.dates.split(",")],
                           a.timeframe))


if __name__ == "__main__":
    raise SystemExit(main())
