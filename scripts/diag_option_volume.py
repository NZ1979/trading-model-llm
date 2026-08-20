"""Reconcile Alpaca option TRADE volume against Schwab's stored chain volume.

Measured 2026-08-20: for session 2026-08-19 the 1600 call summed to 8,817
against Schwab's 8,818 (1 apart) while the 1800 call summed to 9,564 against
9,656 (92 apart). Same pull, same window, two different agreement rates. This
finds out why instead of guessing.

    python -m scripts.diag_option_volume --contract SNDK260821C01800000 \
        --date 2026-08-19
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from data.alpaca_rest import AlpacaRESTClient

ET = ZoneInfo("America/New_York")
RFC = "%Y-%m-%dT%H:%M:%SZ"


def et_minutes(t: str) -> int | None:
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET)
        return d.hour * 60 + d.minute
    except Exception:                              # noqa: BLE001
        return None


async def _run(a) -> int:
    day = datetime.strptime(a.date, "%Y-%m-%d")
    wide_start = datetime(day.year, day.month, day.day, 0, 1, tzinfo=ET).astimezone(timezone.utc)
    wide_end = datetime(day.year, day.month, day.day, 23, 59, tzinfo=ET).astimezone(timezone.utc)

    rows: list[dict] = []
    async with AlpacaRESTClient.from_env() as client:
        params = {"symbols": a.contract, "limit": 10000,
                  "start": wide_start.strftime(RFC), "end": wide_end.strftime(RFC)}
        pages, token = 0, None
        while True:
            if token:
                params["page_token"] = token
            payload = await client._get("/v1beta1/options/trades", params)
            got = (payload.get("trades") or {}).get(a.contract) or []
            rows.extend(got)
            pages += 1
            token = payload.get("next_page_token")
            print(f"  page {pages}: {len(got)} rows{'' if token else '  (last)'}")
            if not token:
                break
            if pages > 200:
                raise RuntimeError("runaway pagination")
    rows.sort(key=lambda r: r.get("t") or "")

    vol = sum(r.get("s") or 0 for r in rows)
    print("\n" + "=" * 74)
    print(f"{a.contract}   session {a.date}   FULL-DAY pull 00:01-23:59 ET")
    print("=" * 74)
    print(f"  prints {len(rows):,}   volume {vol:,}")
    if rows:
        print(f"  first {rows[0].get('t')}")
        print(f"  last  {rows[-1].get('t')}")

    # --- how much sits outside the 09:00-16:30 ET window option_flow used
    inside, outside = [], []
    for r in rows:
        m = et_minutes(r.get("t") or "")
        (inside if (m is not None and 9 * 60 <= m < 16 * 60 + 30)
         else outside).append(r)
    vin = sum(r.get("s") or 0 for r in inside)
    vout = vol - vin
    print(f"\n  inside 09:00-16:30 ET : {len(inside):,} prints  {vin:,} contracts")
    print(f"  OUTSIDE that window   : {len(outside):,} prints  {vout:,} contracts")
    for r in outside[:12]:
        print(f"      {r.get('t')}  p={r.get('p')} s={r.get('s')} "
              f"x={r.get('x')} c={r.get('c')}")

    # --- condition codes
    print("\n  volume by CONDITION code:")
    cc: Counter = Counter()
    for r in rows:
        c = r.get("c")
        key = ",".join(c) if isinstance(c, list) else str(c)
        cc[key] += r.get("s") or 0
    for k, v in cc.most_common():
        print(f"      c={k!r:<12} {v:>8,} contracts  "
              f"({v/max(vol,1)*100:5.1f}%)")

    # --- exchanges
    print("\n  volume by EXCHANGE:")
    xx: Counter = Counter()
    for r in rows:
        xx[str(r.get("x"))] += r.get("s") or 0
    for k, v in xx.most_common(12):
        print(f"      x={k!r:<6} {v:>8,} contracts  ({v/max(vol,1)*100:5.1f}%)")

    # --- Alpaca's OWN daily bar for the same contract. If the bar agrees with
    # the trade sum, the trades pull is complete and any gap versus Schwab is a
    # vendor-level disagreement. If the bar is higher, the trades pull dropped
    # prints. This is the test that separates those two.
    # A 1Day bar is stamped at the SESSION START, so a window that begins at
    # 00:01 ET excludes the very bar it is asking for. Request a wide range and
    # select on the date instead of trusting the window boundary.
    bar_vol = None
    bars_returned = 0
    pad_start = (wide_start - timedelta(days=4)).strftime(RFC)
    pad_end = (wide_end + timedelta(days=4)).strftime(RFC)
    async with AlpacaRESTClient.from_env() as client2:
        try:
            bp = await client2._get("/v1beta1/options/bars", {
                "symbols": a.contract, "timeframe": "1Day",
                "start": pad_start, "end": pad_end, "limit": 10000})
            brows = (bp.get("bars") or {}).get(a.contract) or []
            bars_returned = len(brows)
            print(f"\n  ALPACA daily bars ({bars_returned} returned over "
                  f"{pad_start[:10]}..{pad_end[:10]}):")
            for b in brows:
                mark = "  <-- target session" if str(b.get("t", "")).startswith(a.date) else ""
                print(f"      {b.get('t')}  o={b.get('o')} h={b.get('h')} "
                      f"l={b.get('l')} c={b.get('c')} v={b.get('v')} "
                      f"n={b.get('n')}{mark}")
            hit = [b for b in brows if str(b.get("t", "")).startswith(a.date)]
            if hit:
                bar_vol = sum(b.get("v") or 0 for b in hit)
            else:
                print(f"      NO bar stamped {a.date} — cannot cross-check.")
        except Exception as exc:                   # noqa: BLE001
            print(f"\n  bars FAILED: {type(exc).__name__}: {exc}")

    # --- Schwab's figure
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=4000")
    # Schwab stores the 21-char OCC symbol with the root padded to 6; Alpaca
    # uses the 19-char unpadded form. Try both rather than silently miss.
    root = a.contract[:len(a.contract) - 15]
    padded = f"{root:<6}{a.contract[len(root):]}"
    r = None
    for cand in (padded, a.contract):
        r = con.execute(
            "SELECT volume, open_interest, "
            "datetime(fetched_at_ms/1000,'unixepoch'), symbol "
            "FROM chain_snapshots WHERE session_date=? AND symbol=?",
            (a.date.replace("-", ""), cand)).fetchone()
        if r:
            break
    con.close()
    print("\n  " + "-" * 70)
    if r:
        print(f"  SCHWAB stored volume  {r[0]:>8,}   (OI {r[1]:,}, "
              f"row written {r[2]} UTC, symbol {r[3]!r})")
        print(f"  ALPACA trade sum      {vol:>8,}")
        if bar_vol is None:
            print(f"  ALPACA daily bar      UNAVAILABLE "
                  f"({bars_returned} bars returned, none stamped {a.date}) "
                  f"-- NO cross-check possible, drawing no conclusion")
        else:
            print(f"  ALPACA daily bar      {bar_vol:>8,}")
            if bar_vol == vol:
                print("  -> trades and Alpaca's OWN bar agree: the trades pull is")
                print("     COMPLETE, and the gap to Schwab is a VENDOR difference.")
            elif bar_vol > vol:
                print(f"  -> bar EXCEEDS the trade sum by {bar_vol-vol:+,}: the")
                print("     trades pull is INCOMPLETE, not a vendor difference.")
            else:
                print(f"  -> bar is BELOW the trade sum by {bar_vol-vol:+,},")
                print("     which neither hypothesis predicts. Unexplained.")
        print(f"  SCHWAB - ALPACA trades {r[0]-vol:+,}  "
              f"({abs(r[0]-vol)/max(r[0],1)*100:.2f}% of Schwab)")
    else:
        print("  no Schwab row stored for that contract/session")
    return 0


async def _reconcile(a) -> int:
    """Chain-wide: Schwab stored volume vs Alpaca daily-bar volume, per contract.

    Four contracts is an anecdote. This measures the discrepancy across every
    contract Schwab stored for one expiration on one session, so the difference
    can be bounded rather than characterised from a sample of four.
    """
    day = datetime.strptime(a.date, "%Y-%m-%d")
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=4000")
    rows = con.execute(
        "SELECT symbol, strike, put_call, volume, open_interest "
        "FROM chain_snapshots WHERE session_date=? AND underlying=? "
        "AND substr(expiration,1,10)=? AND volume>=? ORDER BY volume DESC",
        (a.date.replace("-", ""), a.underlying, a.expiration, a.min_volume)
    ).fetchall()
    con.close()
    if not rows:
        print("no stored contracts matched. check --expiration / --date.")
        return 1
    schwab = {r[0].replace(" ", ""): dict(sym=r[0], k=r[1], cp=r[2],
                                          vol=r[3], oi=r[4]) for r in rows}
    print(f"Schwab stored {len(schwab)} contracts with volume >= {a.min_volume} "
          f"for {a.underlying} {a.expiration} on {a.date}")

    pad_start = (datetime(day.year, day.month, day.day, tzinfo=ET)
                 - timedelta(days=3)).astimezone(timezone.utc).strftime(RFC)
    pad_end = (datetime(day.year, day.month, day.day, tzinfo=ET)
               + timedelta(days=3)).astimezone(timezone.utc).strftime(RFC)
    syms = list(schwab)
    bars: dict[str, int] = {}
    async with AlpacaRESTClient.from_env() as client:
        for i in range(0, len(syms), 40):
            chunk = syms[i:i + 40]
            token = None
            while True:
                prm = {"symbols": ",".join(chunk), "timeframe": "1Day",
                       "start": pad_start, "end": pad_end, "limit": 10000}
                if token:
                    prm["page_token"] = token
                bp = await client._get("/v1beta1/options/bars", prm)
                for sym, brs in (bp.get("bars") or {}).items():
                    for b in brs:
                        if str(b.get("t", "")).startswith(a.date):
                            bars[sym] = bars.get(sym, 0) + (b.get("v") or 0)
                token = bp.get("next_page_token")
                if not token:
                    break
            print(f"  batch {i//40+1}: {len(chunk)} symbols requested, "
                  f"{len(bars)} matched so far")

    exact = within1 = diff_n = missing = 0
    tot_s = tot_a = 0
    worst = []
    for sym, m in schwab.items():
        av = bars.get(sym)
        if av is None:
            missing += 1
            continue
        tot_s += m["vol"]; tot_a += av
        d = m["vol"] - av
        if d == 0:
            exact += 1
        elif abs(d) <= 1:
            within1 += 1
        else:
            diff_n += 1
        worst.append((abs(d), d, m, av))
    n = exact + within1 + diff_n
    print(f"\n  compared {n} contracts ({missing} had no Alpaca bar)")
    print(f"    exact match      {exact:>5}  ({exact/max(n,1)*100:.1f}%)")
    print(f"    within 1         {within1:>5}  ({within1/max(n,1)*100:.1f}%)")
    print(f"    differ by >1     {diff_n:>5}  ({diff_n/max(n,1)*100:.1f}%)")
    gross = sum(w[0] for w in worst)
    pos = sum(w[1] for w in worst if w[1] > 0)
    neg = sum(w[1] for w in worst if w[1] < 0)
    print(f"\n  TOTAL volume  Schwab {tot_s:,}   Alpaca {tot_a:,}")
    print(f"    NET   difference {tot_s-tot_a:+,}  "
          f"({(tot_s-tot_a)/max(tot_s,1)*100:+.3f}% of Schwab)")
    print(f"    GROSS difference {gross:,}  "
          f"({gross/max(tot_s,1)*100:.3f}% of Schwab)   "
          f"[Schwab higher {pos:+,} / lower {neg:+,}]")
    worst.sort(key=lambda x: x[0], reverse=True)   # never fall through to the dict
    print(f"\n  largest discrepancies:")
    print(f"    {'strike':>8} {'side':5} {'schwab':>9} {'alpaca':>9} "
          f"{'diff':>7} {'diff%':>8}")
    for _, d, m, av in worst[:12]:
        print(f"    {m['k']:8.0f} {m['cp']:5} {m['vol']:>9,} {av:>9,} "
              f"{d:>+7,} {d/max(m['vol'],1)*100:>7.2f}%")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="diag_option_volume")
    ap.add_argument("--contract", help="full OCC symbol (single-contract mode)")
    ap.add_argument("--reconcile", action="store_true",
                    help="chain-wide Schwab-vs-Alpaca volume reconciliation")
    ap.add_argument("--underlying", default="SNDK")
    ap.add_argument("--expiration", default="2026-08-21")
    ap.add_argument("--min-volume", type=int, default=50)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--db", default="data/chains/chains.db")
    args = ap.parse_args(argv)
    if not args.reconcile and not args.contract:
        ap.error("give --contract, or --reconcile for chain-wide mode")
    try:
        return asyncio.run(_reconcile(args) if args.reconcile else _run(args))
    except Exception as exc:                       # noqa: BLE001
        print(f"diag FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
