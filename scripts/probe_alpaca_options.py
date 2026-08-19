#!/usr/bin/env python3
"""Probe what Alpaca's options API actually returns for a symbol.

    python -m scripts.probe_alpaca_options --symbol SNDK

The options half of Algo Trader Plus has been paid for since the subscription
started and, per data/alpaca_rest.py's module docstring, was never called.
This answers what is actually in there before anything is built on it, and
before any further data subscription is bought.

WHAT IT CHECKS
--------------
1. RAW snapshot keys — the parsed `OptionQuote` dataclass drops fields it does
   not model, so a census of the parsed object cannot prove a field is absent.
   This prints the raw JSON keys the endpoint returns, which is the only way
   to test the docstring's claim that open interest does not exist here.
2. Field population — which greeks and quote fields come back non-null on a
   liquid front-month contract versus a far-dated one. "Present in the schema"
   and "populated for this contract" are different facts.
3. Historical option bars — how far back /v1beta1/options/bars actually serves
   for a real contract, against the documented February 2024 floor.
4. An explicit OPEN INTEREST verdict, stated either way.

Prints field NAMES, counts and dates only. No credential value is read,
printed, or logged; keys come from the environment via
`AlpacaRESTClient.from_env()`.

Exit codes: 0 probe completed, 1 network/credential failure, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from data.alpaca_rest import AlpacaRESTClient

OI_KEYS = ("open_interest", "openInterest", "oi", "open_interest_date")


async def _probe(symbol: str, days_back: int,
                 spot: float | None = None, band_pct: float = 8.0) -> int:
    now = datetime.now(timezone.utc)

    async with AlpacaRESTClient.from_env() as client:
        # ---------------------------------------------------------- 1. raw
        print("=" * 78)
        print(f"RAW SNAPSHOT PAYLOAD — /v1beta1/options/snapshots/{symbol}")
        print("=" * 78)

        params = {"feed": client._options_feed, "limit": 100}
        if spot:
            # OCC symbols sort by strike, so an unfiltered page returns the
            # LOWEST strikes — deep ITM, barely quoted, and the worst possible
            # sample for asking whether greeks exist. Band around spot instead.
            lo, hi = spot * (1 - band_pct / 100), spot * (1 + band_pct / 100)
            params["strike_price_gte"] = round(lo, 2)
            params["strike_price_lte"] = round(hi, 2)
            print(f"  NEAR-MONEY BAND: strikes {lo:,.2f} - {hi:,.2f} "
                  f"(spot {spot:,.2f} +/-{band_pct:.0f}%)")
        else:
            print("  NO strike filter — sample will be the LOWEST strikes, "
                  "deep ITM and illiquid.")
            print("  Pass --spot to band around the money; greeks are often "
                  "absent on dead contracts.")

        payload = await client._get(
            f"/v1beta1/options/snapshots/{symbol}", params)
        snaps = payload.get("snapshots") or {}
        print(f"  top-level keys : {sorted(payload.keys())}")
        print(f"  contracts      : {len(snaps)}")
        if not snaps:
            print("  EMPTY — cannot probe further", file=sys.stderr)
            return 1

        block_keys: Counter = Counter()
        sub_keys: dict[str, Counter] = {}
        for block in snaps.values():
            if not isinstance(block, dict):
                continue
            block_keys.update(block.keys())
            for k, v in block.items():
                if isinstance(v, dict):
                    sub_keys.setdefault(k, Counter()).update(v.keys())

        print(f"  per-contract keys : {sorted(block_keys)}")
        for k in sorted(sub_keys):
            print(f"    {k}.* : {sorted(sub_keys[k])}")

        # ------------------------------------------------- 2. OI verdict
        print()
        print("=" * 78)
        print("OPEN INTEREST VERDICT")
        print("=" * 78)
        found = [k for k in block_keys if k in OI_KEYS]
        found += [f"{p}.{k}" for p, c in sub_keys.items()
                  for k in c if k in OI_KEYS]
        if found:
            print(f"  PRESENT: {found}")
            print("  This CONTRADICTS data/alpaca_rest.py's module docstring.")
            print("  Verify before acting on it — the docstring cites Alpaca staff.")
        else:
            print("  ABSENT from every key returned by the snapshots endpoint.")
            print("  Confirms the docstring. OI must keep coming from Schwab /chains.")

        # ------------------------------------------- 3. field population
        print()
        print("=" * 78)
        print("FIELD POPULATION across the sampled contracts")
        print("=" * 78)
        greek_pop: Counter = Counter()
        total = 0
        for block in snaps.values():
            if not isinstance(block, dict):
                continue
            total += 1
            g = block.get("greeks") or {}
            for k, v in g.items():
                if v is not None:
                    greek_pop[k] += 1
            for k in ("impliedVolatility", "latestQuote", "latestTrade",
                      "dailyBar", "minuteBar", "prevDailyBar"):
                if block.get(k) is not None:
                    greek_pop[k] += 1
        for k, n in sorted(greek_pop.items(), key=lambda kv: -kv[1]):
            print(f"  {k:22} {n:4}/{total}  ({n/total*100:5.1f}%)")

        # ------------------------- 3b. the OTHER snapshot endpoint
        # /options/snapshots/{underlying} is Alpaca's "option chain".
        # /options/snapshots?symbols=  is a different endpoint whose docs
        # explicitly promise "latest trade, latest quote and greeks". If the
        # chain route omits greeks but this one carries them, the fix is a
        # call-site change, not a data purchase.
        print()
        print("=" * 78)
        print("SECOND ENDPOINT — /v1beta1/options/snapshots?symbols=")
        print("=" * 78)
        sample = sorted(snaps.keys())[:20]
        try:
            alt = await client._get(
                "/v1beta1/options/snapshots",
                {"symbols": ",".join(sample), "feed": client._options_feed},
            )
            alt_snaps = alt.get("snapshots") or {}
            alt_keys: Counter = Counter()
            greeky = 0
            for block in alt_snaps.values():
                if not isinstance(block, dict):
                    continue
                alt_keys.update(block.keys())
                if block.get("greeks") or block.get("impliedVolatility"):
                    greeky += 1
            print(f"  contracts returned : {len(alt_snaps)}")
            print(f"  per-contract keys  : {sorted(alt_keys)}")
            print(f"  carrying greeks/IV : {greeky}/{len(alt_snaps) or 1}")
            if greeky:
                one = next(b for b in alt_snaps.values()
                           if isinstance(b, dict) and b.get("greeks"))
                print(f"  greeks fields      : {sorted((one.get('greeks') or {}).keys())}")
                print("  VERDICT: greeks ARE available — the chain route is the "
                      "wrong call site.")
            else:
                print("  VERDICT: greeks absent here too. Not a call-site issue.")
        except Exception as exc:
            print(f"  FAILED {type(exc).__name__}: {exc}")

        # --------------------------------------------- 4. historical bars
        print()
        print("=" * 78)
        print("HISTORICAL OPTION BARS — /v1beta1/options/bars")
        print("=" * 78)
        occ = sorted(snaps.keys())
        probe_syms = [occ[0], occ[len(occ) // 2], occ[-1]]
        start = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for sym in probe_syms:
            try:
                bars = await client._get(
                    "/v1beta1/options/bars",
                    {"symbols": sym, "timeframe": "1Day",
                     "start": start, "limit": 10000},
                )
                rows = (bars.get("bars") or {}).get(sym) or []
                if rows:
                    print(f"  {sym}: {len(rows):,} daily bars  "
                          f"{rows[0].get('t','?')[:10]} -> {rows[-1].get('t','?')[:10]}")
                    print(f"      bar keys: {sorted(rows[0].keys())}")
                else:
                    print(f"  {sym}: 0 bars in the window (contract may be new "
                          f"or illiquid)")
            except Exception as exc:
                print(f"  {sym}: FAILED {type(exc).__name__}: {exc}")

        print()
        print("  Alpaca documents historical option data from February 2024;")
        print("  SNDK listed 2025-02-24. If those hold, coverage spans the")
        print("  stock's entire life — but trust the earliest bar printed")
        print("  above over either claim.")

    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="probe_alpaca_options")
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--days-back", type=int, default=900,
                    help="how far back to request option bars (default 900)")
    ap.add_argument("--spot", type=float, default=None,
                    help="current underlying price; bands the sample around "
                         "the money instead of sampling the lowest strikes")
    ap.add_argument("--band-pct", type=float, default=8.0,
                    help="half-width of the strike band as %% of spot")
    args = ap.parse_args(argv)

    if args.days_back < 1:
        print("--days-back must be >= 1", file=sys.stderr)
        return 2
    if args.spot is not None and args.spot <= 0:
        print("--spot must be positive", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_probe(args.symbol.upper(), args.days_back,
                                  args.spot, args.band_pct))
    except Exception as exc:
        print(f"probe FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
