#!/usr/bin/env python3
"""Day-over-day open-interest change report.

    python -m scripts.oi_report --symbol SNDK
    python -m scripts.oi_report --symbol SNDK --min-oi 500 --top 15

Reads two stored chain snapshots from `data/chains/chains.db` and reports where
positioning was ADDED and REMOVED between the two closes they describe.

WHY THIS AND NOT THE FLOW TABLE
-------------------------------
`fetch_option_chain`'s FLOW table ranks on volume/OI, which measures TURNOVER
and carries no direction: a contract can print huge volume as pure churn, or as
closing trades that shrink open interest. Open-interest CHANGE is the only
field in the chain that says whether positions were opened or closed.

    OI up   + volume  -> contracts were OPENED (new positioning)
    OI down + volume  -> contracts were CLOSED (unwind)
    OI flat + volume  -> churn between existing holders

THE T+1 OFFSET, AND WHICH VOLUME GOES WITH IT
---------------------------------------------
Open interest published in a chain fetched on day D is the CLOSE OF D-1.
Comparing fetches from D and D-1 therefore yields the change between the closes
of D-2 and D-1 — not "today".

The volume that PRODUCED that OI change is the volume traded during the D-1
SESSION, which is the `volume` column of the EARLIER snapshot. `OIChange.volume`
is the volume on the LATER fetch date and belongs to a different day entirely.

    v1 of this report printed OIChange.volume beside the OI change, implying one
    window when they were two. That is repo trap #6 — a field's name is not its
    definition. Rows showing |change| > volume were not anomalies, just
    mismatched columns. This version joins the earlier snapshot for volume and
    labels the column with the date it came from.

Because the earlier fetch may have run mid-session, its volume can be a
PARTIAL-day figure. The report prints the wall-clock time of both fetches so
that is visible rather than assumed. With the windows aligned, |change| > volume
becomes a real signal — trading cannot move OI by more than the contracts
traded — so surviving rows are flagged as revisions, exercise, or assignment.

THE VINTAGE TRAP (repo trap #5, restated for this report)
---------------------------------------------------------
`chain_snapshots` keeps one row per contract per session, so a narrow fetch
overwrites rows written by a wider earlier one. Any CHAIN-WIDE AGGREGATE read
back out of the database can therefore sum across vintages taken at different
strike windows.

Per-contract joins are immune, and `oi_change()` is a per-contract join — a
contract must exist in BOTH snapshots to appear at all. But the STRIKE ROLLUP
below sums those joined rows, so it inherits whatever coverage the narrower of
the two fetches had. The report prints the comparison basis on every run. A
large size delta means the two fetches used different strike windows and the
rollup understates the edges.

Exit codes: 0 report produced, 2 not enough stored sessions.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from data.chain_store import ChainStore

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


def _fmt(n: int) -> str:
    return f"{n:+,}" if n else "0"


def _fetch_time(rows) -> datetime | None:
    """Newest fetch timestamp in a snapshot, in ET."""
    stamps = [r["fetched_at_ms"] for r in rows if r["fetched_at_ms"]]
    if not stamps:
        return None
    return datetime.fromtimestamp(max(stamps) / 1000, timezone.utc).astimezone(NY)


def volume_basis(fetched: datetime | None) -> tuple[str, str]:
    """Classify whether a snapshot's `volume` column is usable.

    `volume` counts contracts traded on the FETCH date, accumulating through
    the session. When the OI change being reported spans that same session,
    only a post-close fetch holds the complete figure.

        pre-market fetch  -> volume is 0 for every contract, structurally
        intraday fetch    -> partial day, understates
        post-close fetch  -> final and usable

    Returns (status, human explanation). Observed 2026-08-19: the 20260818
    snapshot was fetched 07:30 ET, so its volume column was uniformly zero and
    a naive join reported 285 of 306 contracts as vendor data-quality failures.
    The vendor was fine. Fail visibly instead (Rule 18).
    """
    if fetched is None:
        return "unknown", "no fetch timestamp stored"
    t = fetched.timetz().replace(tzinfo=None)
    stamp = fetched.strftime("%H:%M ET")
    if t < SESSION_OPEN:
        return "premarket", (f"fetched {stamp}, before the 09:30 open — volume is "
                             f"structurally 0 and CANNOT describe this window")
    if t < SESSION_CLOSE:
        return "intraday", f"fetched {stamp}, mid-session — volume is a PARTIAL day"
    return "final", f"fetched {stamp}, after the close — volume is final"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="oi_report",
        description="Day-over-day open-interest change from stored chain snapshots.",
    )
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--db-dir", default="data/chains")
    ap.add_argument("--session", default=None,
                    help="later fetch date YYYYMMDD (default: latest stored)")
    ap.add_argument("--prior", default=None,
                    help="earlier fetch date YYYYMMDD (default: previous stored)")
    ap.add_argument("--min-oi", type=int, default=250,
                    help="ignore contracts below this OI at the later close "
                         "(default 250; illiquid strikes are noise)")
    ap.add_argument("--min-change", type=int, default=0,
                    help="ignore absolute OI changes below this")
    ap.add_argument("--top", type=int, default=12, help="rows per table")
    args = ap.parse_args(argv)

    sym = args.symbol.upper()

    with ChainStore(args.db_dir) as store:
        stored = store.sessions(sym)
        if len(stored) < 2:
            print(f"{sym}: need two stored sessions, have {len(stored)}: {stored}",
                  file=sys.stderr)
            print("run scripts.fetch_option_chain on two separate days first",
                  file=sys.stderr)
            return 2

        rows = store.oi_change(
            sym,
            session_date=args.session,
            prior_session_date=args.prior,
            min_open_interest=args.min_oi,
            min_abs_change=args.min_change,
            min_days_to_expiration=1,
        )

        later = args.session or stored[-1]
        earlier = args.prior or store.prior_session(sym, later)
        snap_later = store.snapshot(sym, later) or []
        snap_earlier = store.snapshot(sym, earlier) or []

    # Volume during the session that PRODUCED the OI change lives in the
    # EARLIER snapshot — but only if that fetch ran after the close.
    earlier_fetched = _fetch_time(snap_earlier)
    later_fetched = _fetch_time(snap_later)
    vol_status, vol_note = volume_basis(earlier_fetched)
    vol_usable = vol_status in ("intraday", "final")
    vol_window = ({r["symbol"]: r["volume"] for r in snap_earlier}
                  if vol_usable else {})

    if not rows:
        print(f"{sym}: no contracts matched (min-oi {args.min_oi}, "
              f"min-change {args.min_change}) between {earlier} and {later}")
        return 0

    as_of = rows[0].as_of_close
    prior_close = rows[0].prior_close

    print("=" * 82)
    print(f"{sym}  OPEN-INTEREST CHANGE")
    print(f"  fetches compared : {earlier} -> {later}")
    print(f"  closes described : {prior_close} -> {as_of}   <- the window this measures")
    if vol_usable:
        print(f"  VOL column       : contracts traded on {earlier} — {vol_note}")
    else:
        print(f"  VOL column       : OMITTED — {vol_note}")
    print(f"  filters          : OI >= {args.min_oi:,}, |change| >= {args.min_change:,}, DTE >= 1")
    print("=" * 82)

    def stamp(dt: datetime | None) -> str:
        return dt.strftime("%Y-%m-%d %H:%M ET") if dt else "?"

    n_later, n_earlier = len(snap_later), len(snap_earlier)
    print(f"BASIS  contracts stored {earlier}: {n_earlier:,} (fetched {stamp(earlier_fetched)})")
    print(f"       contracts stored {later}: {n_later:,} (fetched {stamp(later_fetched)})")
    print(f"       reported after filters: {len(rows):,}")
    if n_later and n_earlier:
        drop = abs(n_later - n_earlier)
        pct = drop / max(n_later, n_earlier) * 100
        flag = "  <- windows differ, edge strikes may be missing" if pct > 5 else ""
        print(f"       snapshot size delta {drop:,} ({pct:.1f}%){flag}")
    print()

    if not vol_usable:
        print("!" * 82)
        print("VOLUME UNAVAILABLE FOR THIS WINDOW")
        print(f"  The OI change spans the {earlier} session. Volume for that session")
        print(f"  was never captured: the {earlier} fetch ran pre-market, so every")
        print("  contract read 0, and the later fetch holds a different day entirely.")
        print("  OI figures below are unaffected — they are a per-contract join and")
        print("  OI is published T+1, so fetch time does not matter for OI.")
        print("  FIX: run scripts.fetch_option_chain AFTER the 16:00 ET close so the")
        print("  volume column is final. Pre-market fetches serve OI but never volume.")
        print("!" * 82)
        print()

    opened = sorted((r for r in rows if r.oi_change > 0),
                    key=lambda r: -r.oi_change)[:args.top]
    closed = sorted((r for r in rows if r.oi_change < 0),
                    key=lambda r: r.oi_change)[:args.top]

    def table(title: str, subset) -> None:
        print(title)
        if not subset:
            print("  (none)")
            print()
            return
        head = (f"  {'TYPE':5} {'STRIKE':>9} {'EXPIRY':>12} {'OI':>9} {'PRIOR':>9} "
                f"{'CHANGE':>9} {'PCT':>8}")
        print(head + (f" {'VOL':>8}  FLAG" if vol_usable else ""))
        for r in subset:
            pct = r.oi_change_pct
            pct_s = f"{pct:+.1f}%" if pct is not None else "  new"
            line = (f"  {r.put_call:5} {r.strike:9.2f} {r.expiration:>12} "
                    f"{r.open_interest:9,} {r.prior_open_interest:9,} "
                    f"{_fmt(r.oi_change):>9} {pct_s:>8}")
            if vol_usable:
                v = vol_window.get(r.symbol)
                v_s = f"{v:,}" if v is not None else "?"
                flag = ("IMPOSSIBLE-FROM-TRADING"
                        if v is not None and abs(r.oi_change) > v else "")
                line += f" {v_s:>8}  {flag}"
            print(line)
        print()

    table("POSITIONS OPENED — largest OI increases", opened)
    table("POSITIONS CLOSED — largest OI decreases", closed)

    impossible = [r for r in rows
                  if (v := vol_window.get(r.symbol)) is not None
                  and abs(r.oi_change) > v] if vol_usable else []
    if impossible:
        print(f"DATA QUALITY  {len(impossible)} of {len(rows):,} contracts moved OI by "
              f"more than the contracts traded.")
        print("  Trading cannot do that. Causes: post-close revision by the vendor "
              "(observed on this")
        print("  chain), exercise or assignment, or a partial-day volume figure in "
              "the earlier fetch.")
        print("  Treat those rows' magnitudes as unreliable; their SIGN is still "
              "informative.")
        print()

    by_strike: dict[float, dict[str, int]] = defaultdict(
        lambda: {"CALL": 0, "PUT": 0})
    for r in rows:
        by_strike[r.strike][r.put_call] += r.oi_change

    ranked = sorted(by_strike.items(),
                    key=lambda kv: -abs(kv[1]["CALL"] + kv[1]["PUT"]))[:args.top]

    print("NET OI CHANGE BY STRIKE — where walls are GROWING or SHRINKING")
    print("  a wall that is building is forward-looking; a static wall is history")
    print(f"  {'STRIKE':>9} {'CALL':>10} {'PUT':>10} {'NET':>10}")
    for strike, d in ranked:
        net = d["CALL"] + d["PUT"]
        print(f"  {strike:9.2f} {_fmt(d['CALL']):>10} {_fmt(d['PUT']):>10} "
              f"{_fmt(net):>10}")
    print()

    call_up = sum(r.oi_change for r in rows if r.put_call == "CALL" and r.oi_change > 0)
    call_dn = sum(r.oi_change for r in rows if r.put_call == "CALL" and r.oi_change < 0)
    put_up = sum(r.oi_change for r in rows if r.put_call == "PUT" and r.oi_change > 0)
    put_dn = sum(r.oi_change for r in rows if r.put_call == "PUT" and r.oi_change < 0)

    print(f"TOTALS over the {len(rows):,} contracts reported "
          f"(NOT the whole chain — see BASIS)")
    print(f"  calls  opened {call_up:+,}   closed {call_dn:+,}   net {call_up + call_dn:+,}")
    print(f"  puts   opened {put_up:+,}   closed {put_dn:+,}   net {put_up + put_dn:+,}")
    print(f"  all    net {call_up + call_dn + put_up + put_dn:+,}")
    print()
    print(f"  Reads as positioning between the {prior_close} and {as_of} closes.")
    print("  Direction of the underlying over that window is NOT in this table —")
    print("  compare against the price move before calling it bullish or bearish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
