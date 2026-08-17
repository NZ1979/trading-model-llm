"""Fetch one Schwab option chain and summarise it. REAL NETWORK CALL.

Requires a valid Schwab token (run `python -m scripts.schwab_login` first).
Stores every fetched chain to data/chains/chains.db by default so that
day-over-day open-interest change is available tomorrow. --no-store opts out.

Run from C:\\trading\\LLM model with the venv active:

    python -m scripts.fetch_option_chain --symbol SNDK
    python -m scripts.fetch_option_chain --symbol SNDK --strikes 20 --dte 45
    python -m scripts.fetch_option_chain --symbol SNDK --raw   # dump one contract

Prints the OI walls, the volume/OI flow outliers, and the IV skew — the three
things /chains delivers — so the response shape can be checked against real
data before any of it is wired into a strategy.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.schwab_auth import get_client, health  # noqa: E402
from data.chain_store import ChainStore  # noqa: E402
from data.schwab_chains import fetch_chain  # noqa: E402


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper()),
                        format="%(levelname)-7s %(name)s: %(message)s")
    for noisy in ("werkzeug", "flask", "authlib", "schwab", "httpx",
                  "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SNDK")
    ap.add_argument("--strikes", type=int, default=25,
                    help="strikes above AND below at-the-money")
    ap.add_argument("--dte", type=int, default=60,
                    help="only expirations within N days")
    ap.add_argument("--top", type=int, default=8, help="rows per table")
    ap.add_argument("--min-oi", type=int, default=250,
                    help="minimum prior open interest for the flow table. "
                         "Guards against the 0DTE artifact where OI~0 makes "
                         "any volume look like enormous new positioning.")
    ap.add_argument("--min-volume", type=int, default=100,
                    help="minimum contracts traded today for the flow table")
    ap.add_argument("--raw", action="store_true",
                    help="dump one full contract dict and exit")
    ap.add_argument("--no-store", action="store_true",
                    help="do NOT persist this chain. Storing is the default "
                         "because open interest is a once-daily figure and an "
                         "uncaptured session is unrecoverable.")
    args = ap.parse_args()

    setup_logging()

    h = health()
    if h["auth_state"] not in ("OK", "WARN_EXPIRING"):
        print(f"FAILED: Schwab auth state is {h['auth_state']}. "
              f"{h.get('action_required', '')}", file=sys.stderr)
        return 2

    client = get_client()
    chain = fetch_chain(
        client, args.symbol,
        strike_count=args.strikes,
        to_date=date.today() + timedelta(days=args.dte),
    )

    if not chain.contracts:
        print(f"No contracts returned for {args.symbol}. Not optionable, or "
              f"the filters matched nothing.", file=sys.stderr)
        return 1

    # ------------------- persist, so tomorrow can diff --------------------
    #
    # ON BY DEFAULT. Open interest is a T+1 figure: it does not move intraday,
    # but it IS overwritten overnight, so a session that is not captured is
    # gone permanently. Day-over-day OI change is the only measurement that
    # separates opening flow from closing flow -- volume cannot, because
    # volume carries no direction. A contract with volume 5x its open
    # interest looks identical whether customers bought it or sold it.
    #
    # write_chain is idempotent per (session_date, symbol), so re-running this
    # during the day updates in place instead of double-counting.
    if not args.no_store:
        db_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "chains")
        try:
            with ChainStore(db_dir) as store:
                n = store.write_chain(chain)
                latest = store.latest_session(chain.underlying)
                prior = store.prior_session(chain.underlying, latest)
            print(f"stored {n:,} contracts -> data/chains/chains.db  "
                  + (f"prior session {prior} available for OI diff"
                     if prior else
                     "first session for this symbol; OI diff needs one more"))
        except Exception as e:
            # A storage failure must never cost the analysis you ran for.
            print(f"WARNING: chain NOT stored: {type(e).__name__}: {e}",
                  file=sys.stderr)

    if args.raw:
        c = chain.contracts[0]
        print(json.dumps({k: getattr(c, k) for k in c.__slots__}, indent=2,
                         default=str))
        return 0

    px = chain.underlying_price
    print("=" * 78)
    print(f"{chain.underlying}  underlying={px}  "
          f"contracts={len(chain.contracts)}  delayed={chain.is_delayed}")
    if chain.is_delayed:
        print("  ** DELAYED FEED ** open interest is a T+1 figure and still "
              "valid;\n     underlying price, volume, greeks and IV are NOT "
              "current. Percentages\n     below are measured against a stale "
              "spot.")
    print(f"expirations: {', '.join(chain.expirations()[:8])}"
          f"{' ...' if len(chain.expirations()) > 8 else ''}")
    print("=" * 78)

    # ---------------- OI walls, aggregated across expirations -------------
    call_oi: dict[float, int] = defaultdict(int)
    put_oi: dict[float, int] = defaultdict(int)
    for c in chain.calls():
        call_oi[c.strike] += c.open_interest
    for p in chain.puts():
        put_oi[p.strike] += p.open_interest

    print("\nCALL WALLS (resistance) — highest open interest")
    for strike, oi in sorted(call_oi.items(), key=lambda kv: -kv[1])[:args.top]:
        rel = f"{(strike / px - 1) * 100:+6.2f}%" if px else "    n/a"
        print(f"  {strike:>10.2f}  {rel}  OI {oi:>9,}")

    print("\nPUT WALLS (support) — highest open interest")
    for strike, oi in sorted(put_oi.items(), key=lambda kv: -kv[1])[:args.top]:
        rel = f"{(strike / px - 1) * 100:+6.2f}%" if px else "    n/a"
        print(f"  {strike:>10.2f}  {rel}  OI {oi:>9,}")

    total_call_oi = sum(call_oi.values())
    total_put_oi = sum(put_oi.values())
    if total_call_oi:
        print(f"\n  put/call OI ratio: {total_put_oi / total_call_oi:.3f}  "
              f"(calls {total_call_oi:,} / puts {total_put_oi:,})")

    # ---------------- flow: today's volume vs existing OI -----------------
    #
    # Open interest is a T+1 figure — yesterday's close. A contract expiring
    # today therefore has OI near zero by construction, and ANY volume against
    # it produces an enormous v/OI ratio that means nothing. Ranking on the
    # raw ratio surfaces only 0DTE churn. Two guards: require real prior OI,
    # and exclude same-day expiries from the ratio entirely.
    flow = [c for c in chain.contracts
            if c.volume_oi_ratio is not None
            and c.volume >= args.min_volume
            and c.open_interest >= args.min_oi
            and c.days_to_expiration >= 1]
    flow.sort(key=lambda c: -(c.volume_oi_ratio or 0))
    print(f"\nFLOW — volume vs open interest "
          f"(vol>={args.min_volume}, OI>={args.min_oi}, DTE>=1)")
    print(f"  ratio > 1 means more contracts traded today than existed at "
          f"yesterday's close = new positioning")
    if not flow:
        print("  (nothing qualifying — expected outside RTH)")
    for c in flow[:args.top]:
        iv = c.volatility if c.volatility is not None else float("nan")
        print(f"  {c.put_call:<4} {c.strike:>9.2f} {c.expiration}  "
              f"vol {c.volume:>7,}  OI {c.open_interest:>8,}  "
              f"v/OI {c.volume_oi_ratio:>6.2f}  IV {iv:>6.1f}")

    # 0DTE volume is genuinely informative, just not as a v/OI ratio. Shown
    # separately and ranked on raw volume so it cannot contaminate the above.
    zero_dte = [c for c in chain.contracts
                if c.days_to_expiration < 1 and c.volume >= args.min_volume]
    if zero_dte:
        zero_dte.sort(key=lambda c: -c.volume)
        z_call = sum(c.volume for c in zero_dte if c.put_call == "CALL")
        z_put = sum(c.volume for c in zero_dte if c.put_call == "PUT")
        print(f"\n0DTE VOLUME (ranked on volume, NOT v/OI — OI is meaningless "
              f"for same-day expiry)")
        print(f"  total: calls {z_call:,} / puts {z_put:,}"
              + (f"  put/call {z_put / z_call:.2f}" if z_call else ""))
        for c in zero_dte[:args.top]:
            rel = f"{(c.strike / px - 1) * 100:+6.2f}%" if px else "    n/a"
            print(f"  {c.put_call:<4} {c.strike:>9.2f} {rel}  "
                  f"vol {c.volume:>7,}  OI {c.open_interest:>7,}")

    # ---------------- IV skew at the nearest usable expiration ------------
    #
    # Never the same-day expiry: implied vol explodes mechanically as time to
    # expiry approaches zero, so a 0DTE series shows deep-ITM contracts at
    # absurd IVs (a delta +0.97 call quoting IV 245) that describe the pricing
    # model's breakdown, not the market's view of volatility.
    #
    # Skew is read on OUT-OF-THE-MONEY contracts only — OTM puts below spot,
    # OTM calls above. ITM contracts on the same strike carry the same IV by
    # put-call parity and just duplicate the curve.
    if px:
        usable = [c for c in chain.contracts if c.days_to_expiration >= 1]
        if not usable:
            print("\nIV SKEW — no expiration beyond today in range")
        else:
            near_dte = min(c.days_to_expiration for c in usable)
            near = next(c.expiration for c in usable
                        if c.days_to_expiration == near_dte)
            rows = [c for c in usable
                    if c.expiration == near and c.volatility is not None
                    and c.delta is not None
                    and ((c.put_call == "PUT" and c.strike <= px)
                         or (c.put_call == "CALL" and c.strike >= px))]
            rows.sort(key=lambda c: c.strike)
            print(f"\nIV SKEW — {near} ({near_dte}d), OTM only")
            atm = min(rows, key=lambda c: abs(c.strike - px), default=None)
            if atm:
                print(f"  ATM ~{atm.strike:.2f}  IV {atm.volatility:.1f}")
            step = max(1, len(rows) // 12)
            for c in rows[::step]:
                moneyness = (c.strike / px - 1) * 100
                print(f"  {c.put_call:<4} {c.strike:>9.2f} {moneyness:+6.2f}%  "
                      f"IV {c.volatility:>6.1f}  delta {c.delta:>+6.3f}")
            # 25-delta risk reversal: the standard one-number skew summary.
            puts = [c for c in rows if c.put_call == "PUT" and c.delta]
            calls = [c for c in rows if c.put_call == "CALL" and c.delta]
            if puts and calls:
                p25 = min(puts, key=lambda c: abs(abs(c.delta) - 0.25))
                c25 = min(calls, key=lambda c: abs(abs(c.delta) - 0.25))
                print(f"  25d risk reversal: put {p25.volatility:.1f} - "
                      f"call {c25.volatility:.1f} = "
                      f"{p25.volatility - c25.volatility:+.1f} "
                      f"({'put' if p25.volatility > c25.volatility else 'call'}"
                      f" skew)")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
