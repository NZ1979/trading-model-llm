"""Live smoke test: prove the Alpaca SIP tape is real and parseable.

REAL NETWORK CALL to stream.data.alpaca.markets. Requires ALPACA_API_KEY and
ALPACA_API_SECRET in the environment. Writes nothing to disk.

Run from C:\\trading\\LLM model with the venv active:

    python -m scripts.verify_alpaca_sip
    python -m scripts.verify_alpaca_sip --symbols NVDA,MU --seconds 45
    python -m scripts.verify_alpaca_sip --feed iex        # A/B against free feed

What it proves
--------------
1. Credentials authenticate against the requested feed. A SIP auth failure on
   an account without Algo Trader Plus surfaces here, loudly.
2. THE EXCHANGE HISTOGRAM. IEX-routed data carries a single exchange code (V).
   Genuine consolidated SIP carries many. One code means you are on IEX no
   matter what config/settings.yaml says.
3. Trade and quote payloads parse through data/tick_types.py against real
   messages rather than synthetic fixtures.
4. Which trade CONDITION CODES actually arrive, so the filter in spec v4 §5.2
   can be written against observed data instead of guesswork.
5. RECONCILIATION against your chart. Raw last print != charted last price,
   because odd lots are not last-sale eligible. The reconciliation block
   prints both so you can hold them against TradingView and see which one
   agrees. If a future get_snapshot reports the raw last, it will silently
   disagree with every chart you own.

Post-market is a good window: the tape is thin enough to read but real.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter, deque

# Allow both `python -m scripts.verify_alpaca_sip` and `python scripts/...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.alpaca_market_data import AlpacaMarketStream  # noqa: E402
from data.tick_types import Quote, Trade, TradingStatus  # noqa: E402

DEFAULT_SYMBOLS = "NVDA,MU,SNDK"

# Condition codes that make a print NOT last-sale eligible.
#
# PROVISIONAL — derived from codes observed live on 2026-08-14 plus the
# well-established odd-lot rule. This is NOT the full CTA/UTP eligibility
# table. Any code not in KNOWN_CONDITIONS below is reported separately so an
# unrecognised code surfaces loudly instead of being silently treated as
# eligible (Rule 18). Verify against Alpaca's condition-code reference before
# this logic moves into analysis/microstructure.py.
NOT_LAST_ELIGIBLE = {
    "I",  # odd lot — never updates last on the consolidated tape
}

KNOWN_CONDITIONS = {
    "@": "regular sale",
    "T": "extended hours (Form T)",
    "I": "odd lot",
    "F": "intermarket sweep (ISO)",
}


def is_last_eligible(t: Trade) -> bool:
    return not any(c in NOT_LAST_ELIGIBLE for c in t.conditions)


class SymbolBook:
    """Per-symbol reconciliation state."""

    def __init__(self) -> None:
        self.last_raw: Trade | None = None
        self.last_eligible: Trade | None = None
        self.last_quote: Quote | None = None
        self.shares = 0
        self.prints = 0
        self.odd_lot_prints = 0


class Collector:
    def __init__(self, sample_n: int) -> None:
        self.sample_n = sample_n
        # deque so we keep the MOST RECENT prints, not the first ones — the
        # newest data is what you compare against a live chart.
        self.trades: deque[Trade] = deque(maxlen=sample_n)
        self.quotes: deque[Quote] = deque(maxlen=sample_n)
        self.statuses: list[TradingStatus] = []
        self.trade_count = 0
        self.quote_count = 0
        self.trade_exchanges: Counter[str] = Counter()
        self.quote_exchanges: Counter[str] = Counter()
        self.conditions: Counter[str] = Counter()
        self.tapes: Counter[str] = Counter()
        self.books: dict[str, SymbolBook] = {}

    def _book(self, sym: str) -> SymbolBook:
        b = self.books.get(sym)
        if b is None:
            b = self.books[sym] = SymbolBook()
        return b

    async def on_bar(self, _bar) -> None:
        return None

    async def on_trade(self, t: Trade) -> None:
        self.trade_count += 1
        self.trade_exchanges[t.exchange] += 1
        self.tapes[t.tape] += 1
        for c in t.conditions:
            self.conditions[c] += 1
        self.trades.append(t)

        b = self._book(t.symbol)
        b.prints += 1
        b.shares += t.size
        # Guard against out-of-order arrival: only advance on a newer print.
        if b.last_raw is None or t.ts_ns >= b.last_raw.ts_ns:
            b.last_raw = t
        if is_last_eligible(t):
            if b.last_eligible is None or t.ts_ns >= b.last_eligible.ts_ns:
                b.last_eligible = t
        else:
            b.odd_lot_prints += 1

    async def on_quote(self, q: Quote) -> None:
        self.quote_count += 1
        self.quote_exchanges[q.bid_exchange] += 1
        self.quote_exchanges[q.ask_exchange] += 1
        self.quotes.append(q)
        b = self._book(q.symbol)
        if b.last_quote is None or q.ts_ns >= b.last_quote.ts_ns:
            b.last_quote = q

    async def on_status(self, s: TradingStatus) -> None:
        self.statuses.append(s)


def _hist(c: Counter[str], label: str) -> None:
    if not c:
        print(f"  {label}: (none seen)")
        return
    total = sum(c.values())
    print(f"  {label}:")
    for k, n in c.most_common():
        shown = k if k else "(empty)"
        note = ""
        if label.startswith("trade condition"):
            note = "  " + KNOWN_CONDITIONS.get(k, "*** UNKNOWN CODE ***")
        print(f"    {shown:<10} {n:>7}  {100.0 * n / total:5.1f}%{note}")


def _age(ts_ns: int, now_ns: int) -> str:
    secs = (now_ns - ts_ns) / 1e9
    return f"{secs:6.2f}s ago"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--feed", default="sip", choices=("sip", "iex"))
    ap.add_argument("--sample", type=int, default=5,
                    help="how many of the MOST RECENT messages to print")
    args = ap.parse_args()

    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        print("FAILED: ALPACA_API_KEY / ALPACA_API_SECRET not set in the "
              "environment. main.py reads these the same way, so this is a "
              "real blocker, not a script quirk.", file=sys.stderr)
        return 2

    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    col = Collector(args.sample)

    stream = AlpacaMarketStream(
        api_key=key, api_secret=secret, symbols=symbols,
        on_bar=col.on_bar, feed=args.feed,
        on_trade=col.on_trade, on_quote=col.on_quote, on_status=col.on_status,
        tick_symbols=symbols,
    )

    print(f"Connecting: feed={args.feed}  symbols={sorted(symbols)}  "
          f"listening {args.seconds:.0f}s ...")
    task = asyncio.create_task(stream.run())
    try:
        await asyncio.sleep(args.seconds)
    finally:
        stream.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    now_ns = time.time_ns()

    print()
    print("=" * 74)
    print(f"RESULT  feed={args.feed}  window={args.seconds:.0f}s")
    print("=" * 74)
    print(f"  trades: {col.trade_count}   quotes: {col.quote_count}   "
          f"status msgs: {len(col.statuses)}")

    if col.trade_count == 0 and col.quote_count == 0:
        print("\nNO DATA RECEIVED. Either the session is closed for these "
              "symbols, auth failed, or the symbols are wrong. Check the log "
              "lines above for an auth error.")
        return 1

    print()
    _hist(col.trade_exchanges, "trade exchanges")
    print()
    _hist(col.quote_exchanges, "quote exchanges (bid+ask)")
    print()
    _hist(col.tapes, "tapes")
    print()
    _hist(col.conditions, "trade condition codes")

    unknown = set(col.conditions) - set(KNOWN_CONDITIONS)
    if unknown:
        print(f"\n  *** {len(unknown)} UNRECOGNISED condition code(s): "
              f"{sorted(unknown)}. NOT_LAST_ELIGIBLE may be incomplete — "
              f"check Alpaca's reference before trusting eligibility.")

    # ---------------- reconciliation against your chart ----------------
    print()
    print("-" * 74)
    print("  RECONCILIATION — compare 'last eligible' against your chart")
    print("-" * 74)
    for sym in sorted(col.books):
        b = col.books[sym]
        print(f"  {sym}")
        if b.last_raw:
            t = b.last_raw
            print(f"    last RAW print      {t.price:>10.4f} x {t.size:<6} "
                  f"ex={t.exchange:<3} cond={list(t.conditions)}  "
                  f"{_age(t.ts_ns, now_ns)}")
        if b.last_eligible:
            t = b.last_eligible
            print(f"    last ELIGIBLE print {t.price:>10.4f} x {t.size:<6} "
                  f"ex={t.exchange:<3} cond={list(t.conditions)}  "
                  f"{_age(t.ts_ns, now_ns)}   <-- should match your chart")
        else:
            print("    last ELIGIBLE print  (none — every print was an odd lot)")
        if b.last_quote:
            q = b.last_quote
            print(f"    NBBO                {q.bid_price:>10.4f} x "
                  f"{q.bid_size:<6} / {q.ask_price:.4f} x {q.ask_size:<6} "
                  f"spread={q.spread:.4f}  {_age(q.ts_ns, now_ns)}")
        if b.last_raw and b.last_eligible:
            d = b.last_raw.price - b.last_eligible.price
            print(f"    raw - eligible      {d:+.4f}")
        print(f"    prints {b.prints} ({b.odd_lot_prints} odd lot = "
              f"{100.0 * b.odd_lot_prints / max(b.prints, 1):.1f}%), "
              f"{b.shares:,} shares")
        print()

    print(f"  most recent {len(col.trades)} trades:")
    for t in col.trades:
        flag = " " if is_last_eligible(t) else "x"
        print(f"   {flag}{t.as_datetime().strftime('%H:%M:%S.%f')[:-3]}  "
              f"{t.symbol:<6} {t.price:>10.4f} x {t.size:<7} "
              f"ex={t.exchange:<3} cond={list(t.conditions)}")
    print("    ('x' = not last-sale eligible, excluded from charted last)")

    print()
    print(f"  most recent {len(col.quotes)} quotes:")
    for q in col.quotes:
        print(f"    {q.as_datetime().strftime('%H:%M:%S.%f')[:-3]}  "
              f"{q.symbol:<6} {q.bid_price:>10.4f} x {q.bid_size:<6} "
              f"({q.bid_exchange})  /  {q.ask_price:>10.4f} x {q.ask_size:<6} "
              f"({q.ask_exchange})  spread={q.spread:.4f}")

    for s in col.statuses:
        print(f"  STATUS {s.symbol}: {s.status_code} {s.status_message!r} "
              f"reason={s.reason_code} halt={s.is_halt}")

    n_ex = len([e for e in col.trade_exchanges if e])
    print()
    print("=" * 74)
    if args.feed == "sip":
        if n_ex <= 1:
            print(f"SUSPECT: only {n_ex} trade exchange seen "
                  f"({list(col.trade_exchanges) or ['(none)']}). Consolidated "
                  f"SIP should show several. Either the window was too quiet, "
                  f"or this is not really SIP data.")
        else:
            print(f"CONFIRMED SIP: {n_ex} distinct trade exchanges. "
                  f"Consolidated tape, not IEX-only.")
    else:
        print(f"IEX baseline: {n_ex} distinct trade exchange(s). Expect 1 (V). "
              f"Re-run with --feed sip and compare.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
