#!/usr/bin/env python3
"""Backfill OHLCV bars into data/bars/bars.db, extended hours included.

    python -m scripts.backfill_bars --symbol SNDK --days 30
    python -m scripts.backfill_bars --symbol SNDK --days 400 --timeframe 1Day
    python -m scripts.backfill_bars --symbol SNDK,MU,WDC,NVDA,SMH --days 60

Pulls from Alpaca (SIP consolidated tape) and UPSERTs into `PriceStore`.
Re-running over the same window is safe and picks up vendor revisions.

WHY THIS EXISTS
---------------
`scripts/watch.py` holds an in-memory deque only. When it stops the session is
gone, and while it runs the ring buffer drops the session's own extremes as it
rolls — on 2026-08-19 the field named `watch_high` read 1640 against a true
high of 1698.9999. Nothing in the repo persisted price history; `data/ticks/`
held a single file from 2026-08-14.

VERIFY EXTENDED HOURS ON THE FIRST RUN
--------------------------------------
The per-phase counts printed at the end are the check. A trading day of 1Min
bars should show roughly:

    PRE      up to 330 bars   (04:00-09:30 ET)
    REGULAR  up to 390 bars   (09:30-16:00 ET)
    POST     up to 240 bars   (16:00-20:00 ET)

If PRE and POST come back 0 while REGULAR is populated, the account or the
requested feed is regular-hours only and every pre/post-market claim built on
this store would be silently wrong. That is why the counts print rather than a
single success line (Rule 18: fail visibly, never silently degrade).

Sparse phases are normal for illiquid names — a minute with no trades produces
no bar. Zero across an entire liquid symbol is not sparsity, it is entitlement.

CREDENTIALS
-----------
Read from the environment by `AlpacaRESTClient.from_env()`
(`ALPACA_API_KEY` / `ALPACA_API_SECRET`). This script never reads, prints, or
logs them. If they are absent the client raises and the run fails loudly.

Exit codes: 0 wrote bars, 1 network/credential failure, 2 bad arguments,
3 ran clean but stored nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from data.alpaca_rest import AlpacaRESTClient
from data.price_store import PriceStore

# 1Min bars, extended hours, one calendar day = at most 960 rows. Alpaca's
# per-request cap is far above that, so a day-at-a-time walk never needs
# page tokens — which the REST client does not expose.
DAY_CHUNK = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 30, "1Day": 365}


async def _run(symbols: list[str], days: int, timeframe: str,
               db_dir: str, verbose: bool) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    step = timedelta(days=DAY_CHUNK.get(timeframe, 1))

    total = 0
    with PriceStore(db_dir) as store:
        async with AlpacaRESTClient.from_env() as client:
            for symbol in symbols:
                sym_total, windows, empty = 0, 0, 0
                cursor = start
                while cursor < end:
                    stop = min(cursor + step, end)
                    bars = await client.bars(
                        symbol,
                        timeframe=timeframe,
                        limit=10000,
                        start=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        end=stop.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    windows += 1
                    if bars:
                        sym_total += store.write_bars(bars, timeframe=timeframe)
                    else:
                        empty += 1
                    if verbose and bars:
                        print(f"  {symbol} {cursor:%Y-%m-%d} -> {len(bars):,} bars")
                    cursor = stop
                total += sym_total
                print(f"{symbol}: {sym_total:,} bars over {windows} windows "
                      f"({empty} empty — weekends and holidays are expected)")

        stats = store.stats()

    print()
    print("=" * 70)
    print(f"STORE  {stats['db_path']}")
    print(f"  rows {stats['rows']:,}   symbols {stats['symbols']}   "
          f"sessions {stats['sessions']}")
    print(f"  span {stats['first']} -> {stats['last']}")
    print(f"  by phase: {stats['by_phase']}")
    print("=" * 70)

    phases = stats["by_phase"]
    if timeframe != "1Day" and not (phases.get("PRE") or phases.get("POST")):
        print()
        print("!" * 70)
        print("NO PRE OR POST MARKET BARS STORED")
        print("  Every extended-hours figure derived from this store would be")
        print("  wrong, silently. Check the Alpaca feed entitlement before")
        print("  building anything on it — settings.yaml alpaca_data_feed is")
        print("  the switch, and 'iex' understates pre-market badly.")
        print("!" * 70)

    if total == 0:
        print("stored nothing — treat this run as FAILED, not empty", file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_bars",
        description="Backfill OHLCV bars (extended hours included) into bars.db",
    )
    ap.add_argument("--symbol", default="SNDK",
                    help="one symbol or a comma-separated list")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--timeframe", default="1Min",
                    choices=sorted(DAY_CHUNK.keys()))
    ap.add_argument("--db-dir", default="data/bars")
    ap.add_argument("--verbose", action="store_true",
                    help="print each window as it lands")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
    if not symbols:
        print("--symbol must name at least one ticker", file=sys.stderr)
        return 2
    if args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(symbols, args.days, args.timeframe,
                                args.db_dir, args.verbose))
    except Exception as exc:  # fail loud, never silently degrade
        print(f"backfill FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
