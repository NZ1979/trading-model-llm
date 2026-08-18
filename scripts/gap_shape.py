"""Intraday SHAPE of gap-down sessions. Answers a question daily bars cannot.

    python -m scripts.gap_shape SNDK
    python -m scripts.gap_shape SNDK --days 120 --gap -4 --timeframe 5Min

Why this exists
---------------
On 2026-08-18 a claim was made that deep gap-down sessions "trade above the
open first, then sell". That is a statement about ORDER, and daily OHLC bars
contain no order: they say the high was +3.25% above the open and the low was
-6.07% below it, and nothing whatsoever about which came first. The claim was a
fill, not a measurement (Rule 30).

This script measures it. For every session whose gap is at or below the
threshold, it pulls intraday bars and timestamps where the regular-hours high
and low actually landed.

Definitions, stated because each one changes the answer
------------------------------------------------------
- The session is REGULAR HOURS ONLY, 09:30-16:00 ET. Pre-market prints would
  otherwise supply the low on a gap-down day by construction.
- `open` is the first regular-hours bar's open, NOT the daily bar's open. Both
  are printed so a discrepancy is visible rather than assumed away.
- `gap` is the daily bar's open against the prior daily close, matching how the
  session was selected.
- Ties (high bar == low bar, one bar holding both extremes) are reported as
  SAME rather than being forced into an ordering.
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
RTH_OPEN = (9, 30)
RTH_CLOSE = (16, 0)


def _et(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).astimezone(ET)


def _in_rth(dt: datetime) -> bool:
    t = (dt.hour, dt.minute)
    return RTH_OPEN <= t < RTH_CLOSE


def analyse_session(bars) -> dict | None:
    """Shape of one session from its intraday bars. Pure; no I/O."""
    rth = [b for b in bars if _in_rth(_et(b.ts_ns))]
    if len(rth) < 2:
        return None
    o = rth[0].open
    hi_bar = max(rth, key=lambda b: b.high)
    lo_bar = min(rth, key=lambda b: b.low)
    hi_t, lo_t = _et(hi_bar.ts_ns), _et(lo_bar.ts_ns)
    open_t = _et(rth[0].ts_ns)
    if hi_bar is lo_bar:
        order = "SAME"
    elif hi_t < lo_t:
        order = "HIGH first"
    else:
        order = "LOW first"
    return {
        "open": o,
        "high": hi_bar.high, "low": lo_bar.low, "close": rth[-1].close,
        "high_et": hi_t, "low_et": lo_t,
        "mins_to_high": (hi_t - open_t).total_seconds() / 60.0,
        "mins_to_low": (lo_t - open_t).total_seconds() / 60.0,
        "order": order,
        "o2h": (hi_bar.high - o) / o * 100.0,
        "o2l": (lo_bar.low - o) / o * 100.0,
        "o2c": (rth[-1].close - o) / o * 100.0,
        "bars": len(rth),
    }


async def run(symbol: str, days: int, gap_thr: float, timeframe: str) -> int:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    async with AlpacaRESTClient.from_env() as client:
        daily = await client.bars(symbol, timeframe="1Day",
                                  limit=days + 10, start=str(start))
        if len(daily) < 2:
            print(f"no daily bars for {symbol}", file=sys.stderr)
            return 1

        picks = []
        for i in range(1, len(daily)):
            b, pc = daily[i], daily[i - 1].close
            if not pc:
                continue
            gap = (b.open - pc) / pc * 100.0
            if gap <= gap_thr:
                picks.append((_et(b.ts_ns).date(), gap, b.open))

        print(f"{symbol}: {len(daily)} sessions from {start}, "
              f"{len(picks)} with gap <= {gap_thr:+.1f}%  [{timeframe}, RTH only]")
        if not picks:
            return 0

        rows = []
        for d, gap, daily_open in picks:
            intr = await client.bars(
                symbol, timeframe=timeframe, limit=1000,
                start=f"{d}T08:00:00Z", end=f"{d + timedelta(days=1)}T01:00:00Z")
            r = analyse_session(intr)
            if r is None:
                print(f"  {d}  NO INTRADAY BARS RETURNED - excluded", file=sys.stderr)
                continue
            r.update(date=d, gap=gap, daily_open=daily_open)
            rows.append(r)

    if not rows:
        print("no sessions had intraday bars", file=sys.stderr)
        return 1

    print(f"\n{'date':<12}{'gap%':>7}{'HIGH at':>9}{'LOW at':>8}{'order':>12}"
          f"{'o->h%':>8}{'o->l%':>8}{'o->c%':>8}{'bars':>6}")
    for r in rows:
        print(f"{str(r['date']):<12}{r['gap']:>+7.2f}"
              f"{r['high_et'].strftime('%H:%M'):>9}{r['low_et'].strftime('%H:%M'):>8}"
              f"{r['order']:>12}{r['o2h']:>+8.2f}{r['o2l']:>+8.2f}{r['o2c']:>+8.2f}"
              f"{r['bars']:>6}")

    n = len(rows)
    hi_first = sum(1 for r in rows if r["order"] == "HIGH first")
    lo_first = sum(1 for r in rows if r["order"] == "LOW first")
    same = n - hi_first - lo_first
    print(f"\nSEQUENCE, n={n}")
    print(f"  HIGH before LOW (ran up, then sold) : {hi_first}/{n}")
    print(f"  LOW before HIGH (sold, then bounced): {lo_first}/{n}")
    if same:
        print(f"  one bar held both extremes         : {same}/{n}")
    med = lambda xs: sorted(xs)[len(xs) // 2]
    print(f"  median minutes from open to HIGH: {med([r['mins_to_high'] for r in rows]):.0f}")
    print(f"  median minutes from open to LOW : {med([r['mins_to_low'] for r in rows]):.0f}")
    early = sum(1 for r in rows if r["mins_to_high"] <= 30)
    print(f"  high came within 30 min of the open: {early}/{n}")

    # A daily-vs-intraday open mismatch would silently change every percentage.
    bad = [r for r in rows if r["daily_open"] and
           abs(r["open"] - r["daily_open"]) / r["daily_open"] > 0.002]
    print(f"\n  daily-bar open vs first RTH bar open: "
          f"{'all within 0.2%' if not bad else f'{len(bad)} MISMATCH - see below'}")
    for r in bad:
        print(f"    {r['date']}  daily {r['daily_open']:,.2f}  rth {r['open']:,.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?", default="SNDK")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--gap", type=float, default=-5.0,
                    help="select sessions gapping at or below this percent")
    ap.add_argument("--timeframe", default="5Min")
    a = ap.parse_args()
    return asyncio.run(run(a.symbol, a.days, a.gap, a.timeframe))


if __name__ == "__main__":
    raise SystemExit(main())
