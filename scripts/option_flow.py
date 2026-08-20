"""Sign historical option flow with the tick rule, then read it against OI change.

WHY THIS EXISTS
---------------
Open interest is a headcount and carries no sign. `oi_report` can tell you that
strike 1600 gained 566 calls; it cannot tell you whether customers BOUGHT those
calls (leaving dealers short, and short gamma) or SOLD them (leaving dealers
long, and long gamma). Those imply opposite price behaviour from identical open
interest, and the distinction is the whole question.

The clean way to sign a trade is against the quote standing at that instant.
`VERIFIED 2026-08-20`: Alpaca does NOT serve historical option quotes --
/v1beta1/options/quotes returns 404, while /options/quotes/latest returns a full
NBBO. So quote-based signing is available going FORWARD only.

This module uses the fallback that needs trades alone: the TICK RULE. A print
above the previous print is buyer-initiated, below is seller-initiated, equal
carries the previous direction forward.

WHAT THIS IS NOT
----------------
`UNVERIFIED`: the tick rule is well characterised on equities and degrades where
spreads are wide and prints are sparse. Measured 2026-08-20, an at-the-money
SNDK contract quoted 31.30 / 31.90 -- roughly 2% wide -- and averaged 2-3
contracts per print. Consecutive prints bouncing across a spread that wide
produce false signs, so treat every number here as PROVISIONAL until the
forward quote capture measures the agreement rate. The unclassified share is
printed rather than hidden (Rule 18).

    python -m scripts.option_flow --symbol SNDK --expiration 2026-08-21 \
        --strikes 1600 --date 2026-08-19
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from data.alpaca_rest import AlpacaRESTClient

ET = ZoneInfo("America/New_York")
RFC = "%Y-%m-%dT%H:%M:%SZ"


def occ(root: str, expiration: str, put_call: str, strike: float) -> str:
    """SNDK + 260821 + C + 01600000.  Verified against live chain symbols."""
    y, m, d = expiration.split("-")
    return f"{root}{y[2:]}{m}{d}{put_call[0].upper()}{int(round(strike * 1000)):08d}"


async def fetch_trades(client, symbols: list[str], start: datetime,
                       end: datetime, verbose: bool) -> dict[str, list[dict]]:
    """Paginate fully. A truncated pull would read as a complete one."""
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "limit": 10000,
              "start": start.strftime(RFC), "end": end.strftime(RFC)}
    pages, token = 0, None
    while True:
        if token:
            params["page_token"] = token
        payload = await client._get("/v1beta1/options/trades", params)
        block = payload.get("trades") or {}
        for sym, rows in block.items():
            out.setdefault(sym, []).extend(rows)
        pages += 1
        token = payload.get("next_page_token")
        if verbose:
            print(f"    page {pages}: "
                  f"{sum(len(v) for v in block.values())} rows"
                  f"{'' if token else '  (last)'}")
        if not token:
            break
        if pages >= 200:
            raise RuntimeError(
                "aborted after 200 pages — refusing to return a truncated "
                "series that would read as complete")
    for s in out:
        out[s].sort(key=lambda r: r.get("t") or "")
    return out


def tick_rule(trades: list[dict]) -> dict:
    """Buyer-initiated on an uptick, seller-initiated on a downtick, carry
    the last direction across zero ticks. Leading prints before any direction
    is established are UNCLASSIFIED and counted as such."""
    buy = sell = unk = 0
    buy_v = sell_v = unk_v = 0
    prev_p = None
    last_dir = 0
    upticks = downticks = zeroticks = 0
    for tr in trades:
        p, s = tr.get("p"), tr.get("s") or 0
        if p is None:
            unk += 1; unk_v += s
            continue
        if prev_p is None:
            d = 0
        elif p > prev_p:
            d = 1; upticks += 1
        elif p < prev_p:
            d = -1; downticks += 1
        else:
            d = last_dir; zeroticks += 1
        prev_p = p
        if d > 0:
            last_dir = 1; buy += 1; buy_v += s
        elif d < 0:
            last_dir = -1; sell += 1; sell_v += s
        else:
            unk += 1; unk_v += s
    return dict(buy=buy, sell=sell, unk=unk, buy_v=buy_v, sell_v=sell_v,
                unk_v=unk_v, upticks=upticks, downticks=downticks,
                zeroticks=zeroticks, n=len(trades),
                vol=buy_v + sell_v + unk_v)


def oi_change(db: str, underlying: str, expiration: str, strike: float,
              put_call: str, session: str) -> tuple[int | None, int | None, str | None]:
    """OI in a chain fetched on day D is the close of D-1, so flow on `session`
    is (OI stored on the NEXT session) minus (OI stored on `session`)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=4000")
    q = ("SELECT open_interest FROM chain_snapshots WHERE session_date=? "
         "AND underlying=? AND strike=? AND put_call=? "
         "AND substr(expiration,1,10)=?")
    a = con.execute(q, (session, underlying, strike, put_call, expiration)).fetchone()
    nxt = con.execute("SELECT MIN(session_date) FROM chain_snapshots "
                      "WHERE session_date>? AND underlying=?",
                      (session, underlying)).fetchone()
    nxt = nxt[0] if nxt else None
    b = None
    if nxt:
        b = con.execute(q, (nxt, underlying, strike, put_call, expiration)).fetchone()
    con.close()
    return (a[0] if a else None, b[0] if b else None, nxt)


# WHY A FLOOR, AND WHY NOT THE REASON FIRST WRITTEN HERE.
#
# The first version of this comment claimed a measured imbalance had to exceed
# the classifier's error rate to be readable. That is wrong. For a classifier
# with symmetric accuracy `a`:
#
#     measured imbalance = (2a - 1) x true imbalance
#
# Symmetric error ATTENUATES the measurement toward zero. It does not flip the
# sign and it cannot manufacture an imbalance out of balanced flow. So a
# measured imbalance is a LOWER BOUND on the true one, and a small measured
# value means the flow really was close to balanced -- not that a large signal
# is hidden under noise.
#
# The floor exists for two other reasons, both real:
#   1. SAMPLING NOISE. Volume-weighted imbalance on a contract with a few
#      hundred prints turns on a handful of blocks. Measured 2026-08-20: the
#      1820 call's -47.7% rested on 255 prints.
#   2. MULTI-LEG CONTAMINATION. Spread legs print separately and are signed
#      here as if independent, which manufactures directional flow nobody
#      intended. Measured on the 1800 call for 2026-08-19, OPRA conditions
#      f/g/j/n were 429 contracts, 4.5% of volume.
#
# 5% is a judgement, not a derived threshold.
MIN_IMBALANCE_PCT = 5.0

# OPRA conditions that mark a print as one leg of a multi-leg order.
MULTILEG_CONDITIONS = {"f", "g", "j", "n"}


def verdict(oi_delta: int | None, net: int, volume: int = 0) -> str:
    imb = (net / volume * 100) if volume else 0.0
    if abs(imb) < MIN_IMBALANCE_PCT:
        return (f"BALANCED — net is {imb:+.1f}% of volume, below the "
                f"{MIN_IMBALANCE_PCT:.0f}% floor. Symmetric classification "
                f"error attenuates toward zero rather than inventing a sign, "
                f"so this is a genuinely small imbalance and not a large one "
                f"buried in noise. NO directional read.")
    if oi_delta is None:
        return "no OI pair stored — cannot combine"
    if oi_delta > 0 and net > 0:
        return ("OI ROSE on net BUYING -> customers OPENED LONGS, "
                "dealers are SHORT these -> dealer SHORT gamma here")
    if oi_delta > 0 and net < 0:
        return ("OI ROSE on net SELLING -> customers OPENED SHORTS, "
                "dealers are LONG these -> dealer LONG gamma here")
    if oi_delta < 0 and net > 0:
        return "OI FELL on net BUYING -> shorts were bought back (closing)"
    if oi_delta < 0 and net < 0:
        return "OI FELL on net SELLING -> longs were sold out (closing)"
    return "OI flat — churn, no net positioning"


async def _run(a) -> int:
    day = datetime.strptime(a.date, "%Y-%m-%d")
    start = datetime(day.year, day.month, day.day, 9, 0, tzinfo=ET).astimezone(timezone.utc)
    end = datetime(day.year, day.month, day.day, 16, 30, tzinfo=ET).astimezone(timezone.utc)
    strikes = [float(x) for x in a.strikes.split(",") if x.strip()]
    pairs = [(k, cp) for k in strikes for cp in ("CALL", "PUT")]
    syms = [occ(a.symbol, a.expiration, cp, k) for k, cp in pairs]

    print("=" * 78)
    print(f"SIGNED OPTION FLOW — {a.symbol} {a.expiration}  session {a.date}")
    print(f"  tick rule; NO quote data (Alpaca serves no historical option quotes)")
    print(f"  window {start:%Y-%m-%d %H:%M}Z .. {end:%H:%M}Z")
    print("=" * 78)
    for s in syms:
        print(f"  {s}")

    async with AlpacaRESTClient.from_env() as client:
        print("\n  fetching...")
        by_sym = await fetch_trades(client, syms, start, end, a.verbose)

    for (k, cp), sym in zip(pairs, syms):
        tr = by_sym.get(sym) or []
        print("\n" + "-" * 78)
        print(f"{sym}   strike {k:.0f}  {cp}")
        if not tr:
            print("  NO TRADES returned for this contract in the window.")
            continue
        r = tick_rule(tr)
        net = r["buy_v"] - r["sell_v"]
        ml = sum((t.get("s") or 0) for t in tr
                 if (",".join(t["c"]) if isinstance(t.get("c"), list)
                     else str(t.get("c"))) in MULTILEG_CONDITIONS)
        print(f"  prints {r['n']:,}   volume {r['vol']:,}   "
              f"ticks up/down/zero {r['upticks']}/{r['downticks']}/{r['zeroticks']}")
        print(f"  buyer-initiated  {r['buy']:>5} prints  {r['buy_v']:>7,} contracts")
        print(f"  seller-initiated {r['sell']:>5} prints  {r['sell_v']:>7,} contracts")
        print(f"  unclassified     {r['unk']:>5} prints  {r['unk_v']:>7,} contracts"
              f"   ({r['unk_v']/max(r['vol'],1)*100:.1f}% of volume)")
        print(f"  NET SIGNED VOLUME  {net:+,} contracts")
        print(f"  multi-leg legs (OPRA f/g/j/n) {ml:,} contracts "
              f"({ml/max(r['vol'],1)*100:.1f}% of volume) — signed here as if "
              f"independent, which they are not")
        oi_a, oi_b, nxt = oi_change(a.db, a.symbol, a.expiration, k, cp,
                                    a.date.replace("-", ""))
        if oi_a is not None and oi_b is not None:
            d = oi_b - oi_a
            print(f"  OI {oi_a:,} (close {a.date} minus one) -> {oi_b:,} "
                  f"(stored {nxt})   change {d:+,}")
            print(f"  READ: {verdict(d, net, r['vol'])}")
            print(f"  imbalance {net / max(r['vol'],1) * 100:+.1f}% of volume | "
                  f"OI change is {abs(d) / max(r['vol'],1) * 100:.1f}% of volume "
                  f"({'mostly opening' if abs(d) / max(r['vol'],1) > 0.3 else 'mostly churn'})")
        else:
            print(f"  OI pair unavailable (stored sessions: {a.date.replace('-','')}"
                  f" -> {nxt})")
    print("\n" + "=" * 78)
    print("PROVISIONAL. The tick rule is unvalidated on this data. The forward")
    print("quote capture is what turns these into measured numbers.")
    print("=" * 78)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="option_flow")
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--expiration", required=True, help="YYYY-MM-DD")
    ap.add_argument("--strikes", required=True, help="comma-separated")
    ap.add_argument("--date", required=True, help="session to sign, YYYY-MM-DD")
    ap.add_argument("--db", default="data/chains/chains.db")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:                       # noqa: BLE001
        print(f"option_flow FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
