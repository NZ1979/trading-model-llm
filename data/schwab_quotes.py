"""Schwab `/quotes` — real-time NBBO equity snapshots.

Verified 2026-08-17 06:51 ET against SNDK: `quoteType: "NBBO"`,
`realtime: true`, with `quoteTime` and `tradeTime` 48 seconds old. Schwab
equity quotes are real-time.

Why this exists alongside data/alpaca_rest.py
---------------------------------------------
`docs/FEED_SPEC_V4.md` §0 records that Schwab has no time-and-sales service
and its level one is conflated. That is true and still binding for the TAPE —
Schwab gives no per-print size, exchange or condition codes, so aggressor
classification, volume-at-price and large-print detection all need Alpaca.

But a conflated tape and a real-time quote are different things, and Schwab's
snapshot carries three fields Alpaca's does not:

  totalVolume        today's cumulative volume, live. Alpaca's dailyBar does
                     not roll at the pre-market open, so its day_volume is
                     still the PREVIOUS session's until some point after
                     09:30 (see EquitySnapshot.day_bar_matches_last).
  avg10DaysVolume    a volume baseline, in the same call.
  postMarketChange   the extended-hours move computed against `closePrice`,
                     which Schwab carries as its own field rather than
                     inferring from a daily bar. This is exactly the figure
                     alpaca_rest suppresses during pre-market.

So the split is by question, not by vendor:

    "what is this stock doing"      -> Schwab /quotes   (this module)
    "show me the prints and bars"   -> Alpaca SIP       (alpaca_rest)
    "what is the options market
     positioned for"                -> Schwab /chains   (schwab_chains)

THE VOLUME RATIO IS NOT RVOL
----------------------------
`totalVolume / avg10DaysVolume` compares a PARTIAL day against a FULL-day
average. At 06:51 ET on 2026-08-17 that ratio was 0.0264 for SNDK — which
says nothing about whether 521,010 shares before the open is a lot, because
an average full day is not the right denominator for the first three hours
of one.

Real RVOL needs a time-of-day baseline: today's volume at 06:51 against the
typical volume by 06:51. That is what `scripts/build_pm_rvol_thresholds.py`
builds from Polygon, and Schwab cannot supply it.

The field here is therefore named `volume_vs_avg_full_day` and documented as
a fraction, never `rvol`. Naming it rvol would put a number that looks like a
gate input in front of a caller who has one, which is the shape of every
metric trap in this project so far.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Quote older than this is flagged. Schwab quotes update continuously during
# market hours; outside them the last print can legitimately be hours old.
DEFAULT_STALE_MS = 60_000


def _ms_to_ns(ms: Any) -> int | None:
    """Schwab timestamps are epoch MILLISECONDS. Zero means 'no value'."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return int(ms) * 1_000_000


def _age_ms(ts_ns: int | None, now_ns: int) -> float | None:
    if ts_ns is None:
        return None
    return (now_ns - ts_ns) / 1_000_000.0


def _f(d: dict, key: str) -> float | None:
    """Float or None. Schwab uses 0.0 as 'no value' on price fields.

    Deliberately treats 0.0 as absent for prices: an equity never trades at
    zero, and `openPrice: 0.0` before the open means "not yet", not "opened
    at nothing". A caller computing a percentage against 0.0 gets a division
    error or an infinity, both of which read as data rather than absence.
    """
    v = d.get(key)
    if not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f == 0.0 else f


@dataclass(frozen=True, slots=True)
class EquityQuote:
    """One Schwab real-time NBBO snapshot. Carries no credential."""

    symbol: str
    fetched_at: datetime
    realtime: bool | None
    quote_type: str | None          # "NBBO" = real-time, "NFL" = non-fee-liable
    security_status: str | None     # "Normal", "Halted", ...

    last_price: float | None
    last_size: int | None
    last_mic: str | None
    trade_ts_ns: int | None
    trade_age_ms: float | None

    bid: float | None
    bid_size: int | None
    bid_mic: str | None
    ask: float | None
    ask_size: int | None
    ask_mic: str | None
    quote_ts_ns: int | None
    quote_age_ms: float | None

    open_price: float | None
    high_price: float | None
    low_price: float | None
    close_price: float | None       # PRIOR session's official close
    total_volume: int | None        # TODAY's cumulative, live

    net_change: float | None
    net_percent_change: float | None
    post_market_change: float | None
    post_market_percent_change: float | None

    regular_last_price: float | None
    regular_last_size: int | None

    avg_10day_volume: float | None
    avg_1year_volume: float | None
    pe_ratio: float | None
    eps: float | None
    shares_outstanding: float | None
    last_earnings_date: str | None

    description: str | None
    exchange_name: str | None
    is_shortable: bool | None
    is_hard_to_borrow: bool | None
    htb_quantity: int | None
    optionable: bool | None

    week52_high: float | None
    week52_low: float | None

    stale_threshold_ms: int = DEFAULT_STALE_MS

    # ------------------------------------------------------------- derived

    @property
    def is_realtime(self) -> bool:
        """True only when Schwab says so AND the quote type is NBBO.

        Both are checked because they can disagree: `realtime` is an
        account-level assertion and `quoteType` is per-response. Requiring
        both means a downgrade on either shows up rather than being averaged
        away.
        """
        return bool(self.realtime) and self.quote_type == "NBBO"

    @property
    def is_stale(self) -> bool:
        if self.quote_age_ms is None:
            return True
        return self.quote_age_ms > self.stale_threshold_ms

    @property
    def is_halted(self) -> bool | None:
        if self.security_status is None:
            return None
        return self.security_status.lower() != "normal"

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float | None:
        mid, spread = self.mid, self.spread
        if not mid or spread is None:
            return None
        return (spread / mid) * 10_000.0

    @property
    def volume_vs_avg_full_day(self) -> float | None:
        """Today's cumulative volume as a fraction of an average FULL day.

        NOT RVOL. See the module docstring. This compares a partial session
        against a full-session average, so it is bounded above by roughly the
        fraction of the day elapsed and cannot answer "is this unusual for
        this hour". Useful as a rough magnitude; useless as a gate.
        """
        if not self.avg_10day_volume or self.total_volume is None:
            return None
        return self.total_volume / self.avg_10day_volume

    @property
    def change_from_close_pct(self) -> float | None:
        """Last versus the PRIOR session's official close.

        Schwab supplies `closePrice` as its own field, so this is correct
        across the pre-market window where alpaca_rest must suppress the
        equivalent — its daily bar has not rolled and its prev_close is two
        sessions back. Prefers Schwab's own figure when present.
        """
        if self.net_percent_change is not None:
            return self.net_percent_change
        if not self.close_price or self.last_price is None:
            return None
        return ((self.last_price - self.close_price) / self.close_price) * 100.0


def parse_quote(symbol: str, block: dict,
                *, now_ns: int | None = None,
                stale_threshold_ms: int = DEFAULT_STALE_MS) -> EquityQuote:
    """Flatten one Schwab `/quotes` entry.

    The response nests into `quote`, `fundamental`, `reference`, `regular`
    and `extended`. Missing sub-blocks degrade to None rather than raising —
    Schwab omits `fundamental` on some instrument types, and a missing PE
    ratio must not cost you the price.
    """
    now_ns = now_ns or int(datetime.now(timezone.utc).timestamp() * 1e9)
    q = block.get("quote") or {}
    f = block.get("fundamental") or {}
    r = block.get("reference") or {}
    reg = block.get("regular") or {}

    quote_ns = _ms_to_ns(q.get("quoteTime"))
    trade_ns = _ms_to_ns(q.get("tradeTime"))

    def _i(d: dict, key: str) -> int | None:
        v = d.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    return EquityQuote(
        symbol=symbol,
        fetched_at=datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc),
        realtime=block.get("realtime"),
        quote_type=block.get("quoteType"),
        security_status=q.get("securityStatus"),
        last_price=_f(q, "lastPrice"),
        last_size=_i(q, "lastSize"),
        last_mic=q.get("lastMICId"),
        trade_ts_ns=trade_ns,
        trade_age_ms=_age_ms(trade_ns, now_ns),
        bid=_f(q, "bidPrice"), bid_size=_i(q, "bidSize"),
        bid_mic=q.get("bidMICId"),
        ask=_f(q, "askPrice"), ask_size=_i(q, "askSize"),
        ask_mic=q.get("askMICId"),
        quote_ts_ns=quote_ns,
        quote_age_ms=_age_ms(quote_ns, now_ns),
        open_price=_f(q, "openPrice"),
        high_price=_f(q, "highPrice"),
        low_price=_f(q, "lowPrice"),
        close_price=_f(q, "closePrice"),
        total_volume=_i(q, "totalVolume"),
        net_change=q.get("netChange"),
        net_percent_change=q.get("netPercentChange"),
        post_market_change=q.get("postMarketChange"),
        post_market_percent_change=q.get("postMarketPercentChange"),
        regular_last_price=_f(reg, "regularMarketLastPrice"),
        regular_last_size=_i(reg, "regularMarketLastSize"),
        avg_10day_volume=_f(f, "avg10DaysVolume"),
        avg_1year_volume=_f(f, "avg1YearVolume"),
        pe_ratio=_f(f, "peRatio"),
        eps=_f(f, "eps"),
        shares_outstanding=_f(f, "sharesOutstanding"),
        last_earnings_date=f.get("lastEarningsDate"),
        description=r.get("description"),
        exchange_name=r.get("exchangeName"),
        is_shortable=r.get("isShortable"),
        is_hard_to_borrow=r.get("isHardToBorrow"),
        htb_quantity=_i(r, "htbQuantity"),
        optionable=r.get("optionable"),
        week52_high=_f(q, "52WeekHigh"),
        week52_low=_f(q, "52WeekLow"),
        stale_threshold_ms=stale_threshold_ms,
    )


def fetch_quotes(client, symbols: Sequence[str]) -> dict[str, EquityQuote]:
    """Fetch real-time quotes for one or more symbols.

    Raises RuntimeError on a non-200 with the body included — Schwab returns
    a structured error naming the offending parameter, and swallowing it
    makes debugging blind (Rule 18).
    """
    if not symbols:
        return {}
    resp = client.get_quotes(list(symbols))
    if resp.status_code != 200:
        raise RuntimeError(
            f"Schwab /quotes returned {resp.status_code} for {list(symbols)}: "
            f"{resp.text[:400]}")
    payload = resp.json()
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

    out: dict[str, EquityQuote] = {}
    for sym, block in payload.items():
        if not isinstance(block, dict) or "quote" not in block:
            logger.warning("Schwab /quotes returned no quote block for %r",
                           sym)
            continue
        out[sym] = parse_quote(sym, block, now_ns=now_ns)
        if not out[sym].is_realtime:
            # Loud: a silent downgrade to delayed data is invisible in the
            # numbers themselves, and the whole point of this source is that
            # it is real-time.
            logger.warning(
                "Schwab quote for %s is NOT real-time (realtime=%s, "
                "quoteType=%s). Treat prices as delayed.",
                sym, out[sym].realtime, out[sym].quote_type)
    return out


def fetch_quote(client, symbol: str) -> EquityQuote:
    got = fetch_quotes(client, [symbol])
    if symbol not in got:
        raise RuntimeError(
            f"Schwab /quotes returned no usable block for {symbol!r}. Check "
            f"the symbol is a listed US equity.")
    return got[symbol]
