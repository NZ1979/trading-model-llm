"""On-demand market snapshot, for asking Claude about a stock.

    python -m scripts.market SNDK
    python -m scripts.market SNDK NVDA MU
    python -m scripts.market SNDK --chain
    python -m scripts.market SNDK --chain --dte 45 --strikes 20

Prints a readable summary AND writes JSON to ``data/snapshots/``. The JSON
matters: Claude can read that file directly off the disk through the Cowork
device bridge, so the loop is "run one command, ask your question" rather than
"run a command, copy the output, paste it into chat."

This is the delivery mechanism that needs no MCP server, no Claude Desktop
config, and no registration. `mcp_server/server.py` is the smoother version of
the same thing; this one works today.

Why every price prints its age
------------------------------
Ask at 20:00 ET and the last trade may be hours old while rendering exactly
like a price from a second ago. `docs/FEED_SPEC_V4.md` §1a measured 1 of 38
prints last-sale eligible in a post-market window, with the last eligible
price 11.3 seconds stale against raw prints arriving 0.3 seconds ago. Age is
printed next to every timestamped field, and a stale marker is applied rather
than left for the reader to work out.

Data sources, and why they are split
------------------------------------
    quotes, bars, IV, greeks   ->  Alpaca, real-time OPRA (Algo Trader Plus)
    open interest              ->  Schwab /chains, T+1

Alpaca has no open-interest field anywhere in its market data API, so
``--chain`` reports IV and greeks but NOT walls. Open-interest walls come from
``scripts/fetch_option_chain.py`` and ``data/chain_store.py``. Do not read the
absence of OI here as "no open interest today."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.alpaca_rest import AlpacaRESTClient, EquitySnapshot, OptionQuote  # noqa: E402
from analysis.option_walls import (  # noqa: E402
    ChainRow, check_spot_consistency, from_alpaca, risk_reversal,
)

ET = ZoneInfo("America/New_York")
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshots"


def _wrap(text: str, width: int) -> list[str]:
    """Minimal greedy wrap. textwrap would do, but this keeps the guard
    message readable without importing a module for four lines."""
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def _fmt_age(ms: float | None) -> str:
    """Human-readable age. Always shown, never omitted when small."""
    if ms is None:
        return "age unknown"
    if ms < 1000:
        return f"{ms:.0f}ms ago"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s ago"
    if ms < 3_600_000:
        return f"{ms / 60_000:.1f}m ago"
    return f"{ms / 3_600_000:.1f}h ago"


def _fmt_money(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    """None renders as 'n/a', never as 0.0%.

    A missing prior close and a flat open are different facts, and printing
    both as 0.00% would erase the difference at exactly the moment a reader
    is deciding whether the number means anything.
    """
    return "n/a" if v is None else f"{v:+.2f}%"


def _market_state(now_et: datetime) -> str:
    """Plain-language session state, so a weekend price is not read as live."""
    if now_et.weekday() >= 5:
        return "WEEKEND — market closed, figures are from the last session"
    hm = now_et.hour * 60 + now_et.minute
    if hm < 4 * 60:
        return "OVERNIGHT — market closed"
    if hm < 9 * 60 + 30:
        return "PRE-MARKET"
    if hm < 16 * 60:
        return "REGULAR HOURS"
    if hm < 20 * 60:
        return "POST-MARKET"
    return "CLOSED"


def print_snapshot(s: EquitySnapshot) -> None:
    stale = "  ** STALE **" if s.is_stale else ""
    odd = "  ** last print is an ODD LOT, consolidated last is older **" \
        if s.last_is_odd_lot else ""

    print(f"\n{'=' * 62}")
    print(f"  {s.symbol}{stale}")
    print(f"{'=' * 62}")
    print(f"  last      {_fmt_money(s.last_price):>12}   "
          f"{_fmt_age(s.last_age_ms)}{odd}")
    print(f"  bid/ask   {_fmt_money(s.bid):>12} / {_fmt_money(s.ask)}   "
          f"{_fmt_age(s.quote_age_ms)}")
    if s.spread_bps is not None:
        print(f"  spread    {_fmt_money(s.spread):>12}   "
              f"({s.spread_bps:.1f} bps)")
    print()
    print(f"  open      {_fmt_money(s.day_open):>12}   "
          f"gap {_fmt_pct(s.gap_pct)} vs prior close")
    print(f"  high/low  {_fmt_money(s.day_high):>12} / "
          f"{_fmt_money(s.day_low)}")
    print(f"  vwap      {_fmt_money(s.day_vwap):>12}")
    print(f"  volume    {s.day_volume or 0:>12,}   "
          f"prior day {s.prev_volume or 0:,}")
    print(f"  change    {_fmt_pct(s.change_pct):>12}   "
          f"prior close {_fmt_money(s.prev_close)}")


def _to_rows(quotes: list[OptionQuote], today: date) -> list[ChainRow]:
    rows = []
    for q in quotes:
        y, m, d = (int(x) for x in q.expiration.split("-"))
        rows.append(from_alpaca(q, dte=(date(y, m, d) - today).days))
    return rows


def print_chain(quotes: list[OptionQuote], underlying_px: float | None,
                top: int = 12, *, today: date | None = None) -> None:
    if not quotes:
        print("\n  No option contracts returned.")
        return

    rows = _to_rows(quotes, today or date.today())
    check = check_spot_consistency(rows, underlying_px)

    print(f"\n{'=' * 62}")
    print("  OPTION CHAIN — Alpaca OPRA, real-time")
    print("  NO OPEN INTEREST — that comes from Schwab /chains")
    print(f"{'=' * 62}")

    # The guard prints BEFORE the table. A caveat underneath a table of
    # plausible numbers gets read after the numbers have already landed.
    print()
    for line in _wrap(check.explain(), 58):
        print(f"  {line}")

    if not check.trustworthy:
        print()
        print("  IV, delta and gamma below are shown for completeness and")
        print("  are NOT usable. Skew and risk-reversal are suppressed.")

    calls = sorted((q for q in quotes if q.is_call), key=lambda q: q.strike)
    puts = sorted((q for q in quotes if not q.is_call), key=lambda q: q.strike)

    def block(name: str, group: list[OptionQuote]) -> None:
        if not group:
            return
        if underlying_px:
            group = sorted(group, key=lambda q: abs(q.strike - underlying_px))
        group = sorted(group[:top], key=lambda q: q.strike)
        print(f"\n  {name}")
        print(f"  {'strike':>9} {'bid':>8} {'ask':>8} {'IV':>7} "
              f"{'delta':>7} {'gamma':>8} {'exp':>11} {'age':>9}")
        for q in group:
            iv = "—" if q.implied_volatility is None \
                else f"{q.implied_volatility * 100:.1f}"
            d = "—" if q.delta is None else f"{q.delta:+.3f}"
            g = "—" if q.gamma is None else f"{q.gamma:.5f}"
            print(f"  {q.strike:>9,.1f} {_fmt_money(q.bid):>8} "
                  f"{_fmt_money(q.ask):>8} {iv:>7} {d:>7} {g:>8} "
                  f"{q.expiration:>11} {_fmt_age(q.quote_age_ms):>9}")

    block("CALLS (nearest the money)", calls)
    block("PUTS (nearest the money)", puts)

    rr = risk_reversal(rows, check)
    if rr is not None:
        print(f"\n  25d risk reversal — {rr.expiration} ({rr.dte}d): "
              f"{rr.value_vol_points:+.1f} vol points ({rr.direction})")
        print(f"    put {rr.put_strike:,.0f} IV {rr.put_iv * 100:.1f} / "
              f"call {rr.call_strike:,.0f} IV {rr.call_iv * 100:.1f}")


async def run(args: argparse.Namespace) -> int:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)

    print(f"\n  {now_et:%Y-%m-%d %H:%M:%S} ET   "
          f"({now_utc:%H:%M:%S} UTC)   {_market_state(now_et)}")

    payload: dict = {
        "fetched_at_utc": now_utc.isoformat(timespec="seconds"),
        "fetched_at_et": now_et.isoformat(timespec="seconds"),
        "market_state": _market_state(now_et),
        "symbols": {},
    }

    async with AlpacaRESTClient.from_env() as client:
        snaps = await client.snapshots(args.symbols)
        for sym in args.symbols:
            snap = snaps.get(sym)
            if snap is None:
                print(f"\n  {sym}: no data returned")
                payload["symbols"][sym] = {"error": "no data returned"}
                continue
            print_snapshot(snap)
            entry = asdict(snap)
            entry.update({
                "mid": snap.mid, "spread": snap.spread,
                "spread_bps": snap.spread_bps, "gap_pct": snap.gap_pct,
                "change_pct": snap.change_pct, "is_stale": snap.is_stale,
                "last_is_odd_lot": snap.last_is_odd_lot,
            })
            payload["symbols"][sym] = entry

            if args.chain:
                lte = (date.today() + timedelta(days=args.dte)).isoformat()
                quotes = await client.option_chain(
                    sym, expiration_lte=lte)
                print_chain(quotes, snap.last_price, top=args.strikes)
                payload["symbols"][sym]["option_chain"] = [
                    asdict(q) for q in quotes]

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAPSHOT_DIR / f"{'_'.join(args.symbols)}.json"
    out.write_text(json.dumps(payload, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n  JSON written: {out.relative_to(REPO_ROOT)}")
    print("  (Claude can read this directly off disk — no copy/paste needed)\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="On-demand equity and option snapshot from Alpaca.")
    p.add_argument("symbols", nargs="+", help="One or more tickers, e.g. SNDK")
    p.add_argument("--chain", action="store_true",
                   help="Also fetch the option chain (IV and greeks, no OI)")
    p.add_argument("--dte", type=int, default=45,
                   help="With --chain: max days to expiration (default 45). "
                        "Narrow rather than raising the page cap — skew and "
                        "walls live near the money and the front expiries.")
    p.add_argument("--strikes", type=int, default=12,
                   help="With --chain: strikes per side to print (default 12)")
    args = p.parse_args(argv)
    args.symbols = [s.upper() for s in args.symbols]

    try:
        return asyncio.run(run(args))
    except RuntimeError as exc:
        # Credential and entitlement failures both land here, and both carry
        # a message naming the fix. Print it plainly rather than dumping a
        # traceback the reader has to excavate.
        print(f"\n  ERROR: {exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
