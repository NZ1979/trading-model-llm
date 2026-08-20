"""Probe: does Alpaca serve option TRADES and QUOTES, and can flow be signed?

Nothing in this repository has ever called these endpoints. `option_chain()`
uses /v1beta1/options/snapshots and `probe_alpaca_options.py` covered snapshots
and bars. Trades and quotes are UNVERIFIED, so this establishes capability
before anything is built on it (Rule 11, Rule 12).

The question it answers
-----------------------
Open interest is a headcount and carries no sign. A contract's OI rising by 566
says positions were opened; it does not say by whom or in which direction. The
only public data that recovers intent is the TRADE measured against the QUOTE
that stood at that instant:

    print at or above the ask  ->  buyer-initiated  (customer lifting)
    print at or below the bid  ->  seller-initiated (customer hitting)
    print strictly between     ->  unclassifiable from price alone

That is the standard Lee-Ready style classification. It is not perfect --
a customer lifting the offer may be CLOSING a short, multi-leg spreads print as
separate legs, and negotiated blocks print mid-market -- so this probe reports
the UNCLASSIFIABLE share explicitly rather than burying it.

What it does NOT do
-------------------
Store anything. Run anything unattended. This is a read-only capability probe.

    python -m scripts.probe_option_flow --symbol SNDK
    python -m scripts.probe_option_flow --symbol SNDK --minutes 240 --contracts 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

from data.alpaca_rest import AlpacaRESTClient

RFC = "%Y-%m-%dT%H:%M:%SZ"


def _fmt(ts: str | None) -> str:
    return (ts or "")[:26]


async def _try(client, label: str, path: str, params: dict) -> dict | None:
    """Call an endpoint and report loudly what happened. Never swallow."""
    print(f"\n--- {label}")
    print(f"    GET {path}")
    print(f"    params {json.dumps({k: v for k, v in params.items() if k != 'symbols'})}"
          f"  symbols={params.get('symbols')!r}")
    try:
        payload = await client._get(path, params)
    except Exception as exc:                      # noqa: BLE001 - probe
        print(f"    FAILED  {type(exc).__name__}: {str(exc)[:400]}")
        return None
    print(f"    OK      top-level keys: {sorted(payload.keys())}")
    return payload


def _rows(payload: dict | None, bucket: str, sym: str) -> list[dict]:
    if not payload:
        return []
    blk = payload.get(bucket)
    if isinstance(blk, dict):
        return blk.get(sym) or []
    if isinstance(blk, list):
        return blk
    return []


def classify(trades: list[dict], quotes: list[dict]) -> dict:
    """Sign each trade against the last quote at or before its timestamp."""
    qt = [q.get("t") for q in quotes]
    order = sorted(range(len(quotes)), key=lambda i: qt[i] or "")
    qs = [quotes[i] for i in order]
    keys = [q.get("t") or "" for q in qs]
    out = {"at_or_above_ask": 0, "at_or_below_bid": 0, "between": 0,
           "no_quote": 0, "buy_vol": 0, "sell_vol": 0, "mid_vol": 0}
    for tr in trades:
        t, p, s = tr.get("t"), tr.get("p"), tr.get("s") or 0
        if t is None or p is None:
            out["no_quote"] += 1
            continue
        i = bisect_right(keys, t) - 1
        if i < 0:
            out["no_quote"] += 1
            continue
        q = qs[i]
        bid, ask = q.get("bp"), q.get("ap")
        if not bid or not ask:
            out["no_quote"] += 1
            continue
        if p >= ask:
            out["at_or_above_ask"] += 1; out["buy_vol"] += s
        elif p <= bid:
            out["at_or_below_bid"] += 1; out["sell_vol"] += s
        else:
            out["between"] += 1; out["mid_vol"] += s
    return out


async def _run(symbol: str, minutes: int, n_contracts: int, feed: str | None) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    async with AlpacaRESTClient.from_env() as client:
        print("=" * 74)
        print(f"STEP 1 — pull the chain to obtain VALID contract symbols ({symbol})")
        print("=" * 74)
        snap = await client.snapshot(symbol)
        spot = snap.last_price
        if not spot:
            print("FAILED: no spot from the equity snapshot; cannot rank strikes.")
            return 1
        chain = await client.option_chain(
            symbol,
            expiration_lte=(end + timedelta(days=9)).strftime("%Y-%m-%d"),
            limit=1000, max_pages=2)
        if not chain:
            print("FAILED: chain returned nothing; cannot pick contracts.")
            return 1
        ranked = sorted(chain, key=lambda c: abs(c.strike - spot))
        picks = ranked[:n_contracts]
        print(f"  {len(chain)} contracts returned, spot {spot:.2f} "
              f"(from the EQUITY snapshot - OptionQuote has no underlying_price field)")
        for c in picks:
            print(f"  picked {c.symbol!r}  K={c.strike}  "
                  f"{'CALL' if c.is_call else 'PUT'}")

        syms = ",".join(c.symbol for c in picks)
        common = {"symbols": syms,
                  "start": start.strftime(RFC), "end": end.strftime(RFC),
                  "limit": 10000}
        if feed:
            common["feed"] = feed

        print()
        print("=" * 74)
        print(f"STEP 2 — option TRADES over the last {minutes} minutes")
        print("=" * 74)
        tr_payload = await _try(client, "trades (plural endpoint)",
                                "/v1beta1/options/trades", dict(common))

        print()
        print("=" * 74)
        print("STEP 3 — hunt for a QUOTE source. /options/quotes 404s, so try"
              " every plausible path rather than concluding from one.")
        print("=" * 74)
        q_payload = None
        latest_q = None
        for label, path, prm in (
            ("historical quotes", "/v1beta1/options/quotes", dict(common)),
            ("latest quotes", "/v1beta1/options/quotes/latest",
             {"symbols": syms, **({"feed": feed} if feed else {})}),
            ("latest trades", "/v1beta1/options/trades/latest",
             {"symbols": syms, **({"feed": feed} if feed else {})}),
            ("snapshots by symbol", "/v1beta1/options/snapshots",
             {"symbols": syms, **({"feed": feed} if feed else {})}),
        ):
            got = await _try(client, label, path, prm)
            if got is not None and label == "historical quotes":
                q_payload = got
            if got is not None and label == "latest quotes":
                latest_q = got
                blk = got.get("quotes") or {}
                k = next(iter(blk), None)
                if k:
                    print(f"    sample {k}: {json.dumps(blk[k])[:200]}")

        if tr_payload is None:
            print("\nRESULT: the TRADES endpoint is unavailable. Stop here.")
            return 1

        print()
        print("=" * 74)
        print("STEP 4 — field shape and classification feasibility")
        print("=" * 74)
        any_rows = False
        for c in picks:
            trades = _rows(tr_payload, "trades", c.symbol)
            quotes = _rows(q_payload, "quotes", c.symbol)
            print(f"\n  {c.symbol}  K={c.strike} "
                  f"{'CALL' if c.is_call else 'PUT'}")
            print(f"    trades {len(trades):>6}   quotes {len(quotes):>6}")
            if not trades:
                print("    no trades in window (thin contract or market closed)")
                continue
            any_rows = True
            print(f"    trade record keys: {sorted(trades[0].keys())}")
            if quotes:
                print(f"    quote record keys: {sorted(quotes[0].keys())}")
            print(f"    first trade {_fmt(trades[0].get('t'))}  "
                  f"last {_fmt(trades[-1].get('t'))}")
            if not quotes:
                print("    NO historical quotes available for this contract, so"
                      " trades cannot be signed against the standing quote.")
                px = [t.get("p") for t in trades if t.get("p")]
                sz = [t.get("s") or 0 for t in trades]
                if px:
                    vw = sum(p*s for p, s in zip(px, sz)) / max(sum(sz), 1)
                    print(f"    fallback stats: {len(trades)} prints, "
                          f"volume {sum(sz):,}, vwap {vw:.4f}, "
                          f"px {min(px):.2f}..{max(px):.2f}")
                    big = sorted(zip(sz, px), reverse=True)[:3]
                    print(f"    largest prints: "
                          + ", ".join(f"{s}@{p:.2f}" for s, p in big))
                continue
            r = classify(trades, quotes)
            tot = sum(r[k] for k in
                      ("at_or_above_ask", "at_or_below_bid", "between", "no_quote"))
            if tot:
                print(f"    CLASSIFICATION over {tot} trades:")
                print(f"      buyer-initiated  (>= ask) {r['at_or_above_ask']:>5}"
                      f"  {r['at_or_above_ask']/tot*100:5.1f}%   vol {r['buy_vol']:>7,}")
                print(f"      seller-initiated (<= bid) {r['at_or_below_bid']:>5}"
                      f"  {r['at_or_below_bid']/tot*100:5.1f}%   vol {r['sell_vol']:>7,}")
                print(f"      between the quote         {r['between']:>5}"
                      f"  {r['between']/tot*100:5.1f}%   vol {r['mid_vol']:>7,}")
                print(f"      no usable quote           {r['no_quote']:>5}"
                      f"  {r['no_quote']/tot*100:5.1f}%")
                net = r["buy_vol"] - r["sell_vol"]
                print(f"      NET signed volume: {net:+,} contracts")

        print()
        print("=" * 74)
        if any_rows and q_payload is not None:
            print("RESULT: trades AND historical quotes are served. Signed flow")
            print("        is buildable from history.")
        elif any_rows and latest_q is not None:
            print("RESULT: trades are historical, quotes are LATEST-ONLY.")
            print("        Signed flow is NOT buildable from history. It IS")
            print("        buildable going FORWARD by capturing the latest quote")
            print("        alongside trades on a short poll - which is a new")
            print("        unattended process and therefore a SCOPE DECISION.")
        elif any_rows:
            print("RESULT: trades are served, no quote source found at all.")
        else:
            print("RESULT: endpoints responded but no trades landed in the window.")
            print("        Re-run during regular hours or widen --minutes.")
        print("=" * 74)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="probe_option_flow",
        description="Capability probe: Alpaca option trades + quotes, and "
                    "whether flow can be signed against the quote.")
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--minutes", type=int, default=60,
                    help="lookback window for trades and quotes")
    ap.add_argument("--contracts", type=int, default=4,
                    help="how many near-the-money contracts to probe")
    ap.add_argument("--feed", default=None,
                    help="override the options feed (indicative/opra)")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_run(args.symbol, args.minutes,
                                args.contracts, args.feed))
    except Exception as exc:                      # noqa: BLE001
        print(f"probe FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
