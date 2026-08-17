"""Alpaca REST — on-demand equity and option data.

The codebase had no Alpaca REST client. `data/alpaca_market_data.py` is
websocket-only: its constants are `wss://` URLs and its only entry point is
`AlpacaMarketStream`. Every equity number the platform has ever consumed
arrived through a streaming subscription, which is the wrong shape for
"tell me about SNDK right now" — you cannot subscribe, wait, and answer.

This module answers one question per call and holds no state.

Entitlements
------------
Algo Trader Plus ($99/mo, active on all three accounts) covers BOTH:

  - equities: full SIP consolidated tape (`feed=sip`)
  - options:  real-time OPRA (`feed=opra`), including greeks and implied vol

The options half has been paid for since the subscription started and was
never called. `alpaca-py` auto-selects `opra` when entitled; this module is
explicit about it so a silent downgrade to the free `indicative` feed shows
up as a wrong parameter rather than quietly worse data.

WHAT ALPACA DOES NOT GIVE YOU: open interest
--------------------------------------------
There is no open-interest field anywhere in the options market data API.
Alpaca exposes OI on the *trading* API (`/v2/options/contracts`) at T+2, with
no historical endpoint — Alpaca staff confirmed "the open interest data for
day 1 will always lag and be available on day 3."

Open interest therefore comes from Schwab `/chains` via `data/schwab_chains.py`,
persisted by `data/chain_store.py`. The split is:

    OI walls, day-over-day OI change  ->  Schwab   (T+1, delay irrelevant)
    IV, greeks, quotes, underlying    ->  Alpaca   (real-time OPRA)

Do not go looking for OI here. It is not hiding; it does not exist.

Every price carries its age
---------------------------
`docs/FEED_SPEC_V4.md` §1a: in a 20s post-market window, 1 of 38 prints was
last-sale eligible, and the last eligible price was 11.3 seconds old while
raw prints arrived 0.3 seconds ago. A `last` field without an age reads as
current when it is thirty-seven prints behind the market.

That matters MORE for on-demand queries than for a live tape. Ask at 20:00 ET
and the "last price" may be hours old, but it renders identically to a price
from a second ago. Every timestamped field on these dataclasses has a matching
`*_age_ms`, and `is_stale` applies a threshold rather than leaving the caller
to notice.

Credentials
-----------
Alpaca authenticates by HEADER, not query parameter. That sidesteps the
Polygon trap entirely — no URL ever contains a secret, so there is nothing to
scrub out of an exception message or a log line. Nothing here logs, reprs, or
formats a credential regardless.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ALPACA_DATA_BASE = "https://data.alpaca.markets"
ET = ZoneInfo("America/New_York")


def _et_session(ts_ns: int | None) -> str | None:
    """ET calendar date of a nanosecond timestamp, as YYYY-MM-DD.

    The US equity session runs 04:00-20:00 ET, so every print falls inside
    the ET calendar date of its own session. A UTC date would split one
    session across two dates at 20:00 ET.
    """
    if ts_ns is None:
        return None
    return (datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
            .astimezone(ET).strftime("%Y-%m-%d"))

# Odd lots never update last on the consolidated tape. Observed live
# 2026-08-14; see docs/FEED_SPEC_V4.md §1a. Kept in sync with
# data/tick_store.py NOT_LAST_ELIGIBLE deliberately — if that set grows, this
# one must grow with it.
NOT_LAST_ELIGIBLE = frozenset({"I"})

# A price older than this is flagged. Not a hard error: post-market and thin
# names legitimately go minutes between prints, and refusing to answer is
# worse than answering with the age attached.
DEFAULT_STALE_MS = 60_000


def _require_alpaca_keys() -> tuple[str, str]:
    """Read Alpaca credentials from env; raise loud if missing (Rule 18).

    Read at call time rather than import time so tests can monkeypatch the
    environment without re-importing, and so a missing key fails at first use
    with a message naming the fix rather than at module load.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        missing = [n for n, v in
                   (("ALPACA_API_KEY", key), ("ALPACA_API_SECRET", secret))
                   if not v]
        raise RuntimeError(
            f"Alpaca credentials not set in environment: {missing}. "
            f"In PowerShell on Godzilla: $env:ALPACA_API_KEY = '<key>'. "
            f"Never paste the value into a file inside the repo."
        )
    return key, secret


def _ns(ts: str | None) -> int | None:
    """RFC-3339 with nanoseconds -> integer nanoseconds since epoch.

    Nanosecond integers, not datetime: Python truncates datetime to
    microseconds, which collapses prints nanoseconds apart and corrupts
    ordering. Same rule as data/tick_types.py.
    """
    if not ts:
        return None
    body, _, frac = ts.partition(".")
    if body.endswith("Z"):
        body = body[:-1]
    base = datetime.fromisoformat(body).replace(tzinfo=timezone.utc)
    whole = int(base.timestamp()) * 1_000_000_000
    if not frac:
        return whole
    digits = "".join(c for c in frac if c.isdigit())[:9].ljust(9, "0")
    return whole + int(digits)


def _age_ms(ts_ns: int | None, now_ns: int) -> float | None:
    if ts_ns is None:
        return None
    return (now_ns - ts_ns) / 1_000_000.0


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar at an arbitrary timeframe.

    Deliberately NOT data/bar_types.MinuteBar: that type is minute-anchored
    and carries the streaming path's contract. This supports 1Min through
    1Day from the same call. Diverging explicitly rather than silently
    duplicating (FEED_SPEC_V4 §5.3).
    """

    symbol: str
    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int | None
    vwap: float | None

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1e9, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    """Everything answerable about one equity from a single call.

    `last_age_ms` and `quote_age_ms` are not decoration. A last price with no
    age is the single most misleading field this module could return.
    """

    symbol: str
    fetched_at: datetime

    last_price: float | None
    last_size: int | None
    last_ts_ns: int | None
    last_age_ms: float | None
    last_conditions: tuple[str, ...]
    last_exchange: str | None

    bid: float | None
    bid_size: int | None
    ask: float | None
    ask_size: int | None
    quote_ts_ns: int | None
    quote_age_ms: float | None

    day_open: float | None
    day_high: float | None
    day_low: float | None
    day_close: float | None
    day_volume: int | None
    day_vwap: float | None
    day_ts_ns: int | None

    prev_close: float | None
    prev_volume: int | None
    prev_day_ts_ns: int | None

    stale_threshold_ms: int = DEFAULT_STALE_MS

    # ------------------------------------------------------------ derived

    @property
    def last_is_odd_lot(self) -> bool:
        """The last print was not last-sale eligible.

        87-97% of post-market prints carry condition `I`. If the latest trade
        is an odd lot, the consolidated 'last' the rest of the market sees is
        an OLDER print than this one.
        """
        return any(c in NOT_LAST_ELIGIBLE for c in self.last_conditions)

    @property
    def is_stale(self) -> bool:
        if self.last_age_ms is None:
            return True
        return self.last_age_ms > self.stale_threshold_ms

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
        """Spread in basis points of mid. None when mid is zero or absent.

        Absolute spread is not comparable across names; 0.05 is tight on a
        $1,600 stock and enormous on a $3 one.
        """
        mid, spread = self.mid, self.spread
        if not mid or spread is None:
            return None
        return (spread / mid) * 10_000.0

    @property
    def last_session(self) -> str | None:
        """ET session date of the most recent print."""
        return _et_session(self.last_ts_ns)

    @property
    def day_bar_session(self) -> str | None:
        """ET session date the OHLCV block describes."""
        return _et_session(self.day_ts_ns)

    @property
    def day_bar_matches_last(self) -> bool:
        """True when the daily bar covers the same session as `last_price`.

        THE PRE-MARKET TRAP. Alpaca's `dailyBar` does not roll to the new
        session the instant pre-market opens, so between 04:00 ET and the
        roll you get a LIVE last price beside the PREVIOUS session's OHLCV
        and the one before that as `prevDailyBar`. Every percentage derived
        from both then spans the wrong interval.

        Measured on SNDK, 2026-08-17 06:18 ET: last 1724.30 (1.8s old) with
        day_open 1646.93, day_volume 21,087,731 and prev_close 1528.11 — all
        of which belong to Friday and Thursday. `change_pct` computed across
        them read +12.84%, which is a Monday price against a Thursday close
        spanning the whole of Friday's +8.4% session. The true pre-market
        move was +4.06%, against a Friday close the snapshot does not carry.

        Note this is NOT "is the daily bar today's". On a weekend the last
        print and the daily bar are both Friday's, so the percentages are
        correct and describe Friday. What breaks them is the two coming from
        DIFFERENT sessions, which is exactly what this compares.
        """
        a, b = self.last_session, self.day_bar_session
        if a is None or b is None:
            return False
        return a == b

    @property
    def gap_pct(self) -> float | None:
        """Session open versus prior close, in percent.

        None when the daily bar covers a different session than the last
        print — the figure would be a previous session's gap presented as
        the current one. Returns None rather than 0.0 on a missing prior
        close too: 'no gap' and 'gap unknown' are different answers, and a
        gate keyed on 0.0 would treat them identically.
        """
        if not self.day_bar_matches_last:
            return None
        if not self.prev_close or self.day_open is None:
            return None
        return ((self.day_open - self.prev_close) / self.prev_close) * 100.0

    @property
    def change_pct(self) -> float | None:
        """Last price versus prior close, in percent.

        None when the daily bar is from a different session than the last
        print, because `prev_close` is then two sessions behind `last_price`
        and the result silently spans an extra session.
        """
        if not self.day_bar_matches_last:
            return None
        if not self.prev_close or self.last_price is None:
            return None
        return ((self.last_price - self.prev_close) / self.prev_close) * 100.0


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One option contract's real-time quote, greeks and implied vol.

    NO open interest — see the module docstring. `open_interest` is absent
    rather than None so that a caller reaching for it gets an AttributeError
    at the call site instead of silently treating None as zero.
    """

    symbol: str
    underlying: str
    expiration: str
    strike: float
    put_call: str

    bid: float | None
    bid_size: int | None
    ask: float | None
    ask_size: int | None
    quote_ts_ns: int | None
    quote_age_ms: float | None

    last_price: float | None
    last_size: int | None
    last_ts_ns: int | None
    last_age_ms: float | None

    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def is_call(self) -> bool:
        return self.put_call == "CALL"


def parse_occ_symbol(symbol: str) -> tuple[str, str, str, float]:
    """OCC symbol -> (underlying, expiration YYYY-MM-DD, CALL|PUT, strike).

    Format is ROOT + YYMMDD + C|P + strike*1000 zero-padded to 8 digits, e.g.
    ``SNDK260919C01600000``. Alpaca returns these without the padding spaces
    Schwab uses, so this deliberately does not accept Schwab's format —
    silently parsing both would hide a vendor mix-up.
    """
    body = symbol.strip()
    i = 0
    while i < len(body) and not body[i].isdigit():
        i += 1
    root, rest = body[:i], body[i:]
    if len(rest) < 15:
        raise ValueError(f"Not an OCC option symbol: {symbol!r}")
    yy, mm, dd = rest[0:2], rest[2:4], rest[4:6]
    cp = "CALL" if rest[6].upper() == "C" else "PUT"
    strike = int(rest[7:15]) / 1000.0
    return root, f"20{yy}-{mm}-{dd}", cp, strike


class AlpacaRESTClient:
    """On-demand REST client for Alpaca market data.

    Async via httpx, mirroring data/polygon_feed.PolygonRESTClient so there is
    one HTTP pattern in this codebase rather than two. Use as an async context
    manager, or call `aclose()`.

        async with AlpacaRESTClient.from_env() as client:
            snap = await client.snapshot("SNDK")
            print(snap.last_price, snap.last_age_ms)
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        feed: str = "sip",
        options_feed: str = "opra",
        timeout: float = 30.0,
        stale_threshold_ms: int = DEFAULT_STALE_MS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._feed = feed
        self._options_feed = options_feed
        self._stale_threshold_ms = stale_threshold_ms
        # `transport` exists so tests can inject httpx.MockTransport rather
        # than monkeypatching internals or adding a mocking dependency. It is
        # never set in production paths.
        # Header auth: no secret ever enters a URL, so no exception message or
        # log line can leak one by embedding the request target.
        self._client = httpx.AsyncClient(
            base_url=ALPACA_DATA_BASE,
            timeout=timeout,
            transport=transport,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
                "accept": "application/json",
            },
        )

    @classmethod
    def from_env(cls, **kwargs) -> "AlpacaRESTClient":
        key, secret = _require_alpaca_keys()
        return cls(key, secret, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AlpacaRESTClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    # ------------------------------------------------------------- transport

    async def _get(
        self,
        path: str,
        params: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        """GET with exponential backoff on 429 and 5xx.

        A 403 is surfaced immediately with its body rather than retried: on
        Alpaca it almost always means the requested `feed` is not entitled,
        and retrying an entitlement failure three times just delays the
        answer.
        """
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = await self._client.get(path, params=params or {})
                if resp.status_code == 429:
                    logger.warning(
                        "Alpaca rate limited on %s, sleeping %.1fs", path,
                        backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if resp.status_code == 403:
                    raise RuntimeError(
                        f"Alpaca returned 403 for {path}. This usually means "
                        f"the requested feed is not entitled on these keys "
                        f"(feed={self._feed!r}, options_feed="
                        f"{self._options_feed!r}). Body: {resp.text[:300]}"
                    )
                if 500 <= resp.status_code < 600:
                    last_exc = RuntimeError(
                        f"Alpaca {resp.status_code} on {path}: "
                        f"{resp.text[:300]}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    raise last_exc
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Alpaca {resp.status_code} on {path}: "
                        f"{resp.text[:300]}")
                return resp.json()
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError(
            f"Alpaca GET {path} failed after {max_retries} retries: "
            f"{last_exc}")

    # -------------------------------------------------------------- equities

    def _build_snapshot(self, symbol: str, block: dict, now_ns: int
                        ) -> EquitySnapshot:
        trade = block.get("latestTrade") or {}
        quote = block.get("latestQuote") or {}
        day = block.get("dailyBar") or {}
        prev = block.get("prevDailyBar") or {}

        last_ns = _ns(trade.get("t"))
        quote_ns = _ns(quote.get("t"))

        return EquitySnapshot(
            symbol=symbol,
            fetched_at=datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc),
            last_price=trade.get("p"),
            last_size=trade.get("s"),
            last_ts_ns=last_ns,
            last_age_ms=_age_ms(last_ns, now_ns),
            last_conditions=tuple(trade.get("c") or ()),
            last_exchange=trade.get("x"),
            bid=quote.get("bp"),
            bid_size=quote.get("bs"),
            ask=quote.get("ap"),
            ask_size=quote.get("as"),
            quote_ts_ns=quote_ns,
            quote_age_ms=_age_ms(quote_ns, now_ns),
            day_open=day.get("o"),
            day_high=day.get("h"),
            day_low=day.get("l"),
            day_close=day.get("c"),
            day_volume=day.get("v"),
            day_vwap=day.get("vw"),
            day_ts_ns=_ns(day.get("t")),
            prev_close=prev.get("c"),
            prev_volume=prev.get("v"),
            prev_day_ts_ns=_ns(prev.get("t")),
            stale_threshold_ms=self._stale_threshold_ms,
        )

    async def snapshot(self, symbol: str) -> EquitySnapshot:
        """Latest trade, quote, today's bar and yesterday's, in one call."""
        got = await self.snapshots([symbol])
        if symbol not in got:
            raise RuntimeError(
                f"Alpaca returned no snapshot for {symbol!r}. Check the "
                f"symbol is a US equity and is currently listed.")
        return got[symbol]

    async def snapshots(self, symbols: Sequence[str]
                        ) -> dict[str, EquitySnapshot]:
        """Snapshots for many symbols in one request.

        One call for N symbols rather than N calls: the per-request overhead
        dominates at this size, and it keeps a watchlist query inside one
        rate-limit slot.
        """
        if not symbols:
            return {}
        payload = await self._get(
            "/v2/stocks/snapshots",
            {"symbols": ",".join(symbols), "feed": self._feed},
        )
        blocks = payload.get("snapshots", payload)
        now_ns = _now_ns()
        return {
            sym: self._build_snapshot(sym, block, now_ns)
            for sym, block in blocks.items()
            if isinstance(block, dict)
        }

    async def bars(
        self,
        symbol: str,
        *,
        timeframe: str = "1Min",
        limit: int = 100,
        start: str | None = None,
        end: str | None = None,
    ) -> list[Bar]:
        """OHLCV bars. `timeframe` is Alpaca's own vocabulary: 1Min, 5Min,
        15Min, 1Hour, 1Day.
        """
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": timeframe,
            "limit": limit,
            "feed": self._feed,
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        payload = await self._get("/v2/stocks/bars", params)
        rows = (payload.get("bars") or {}).get(symbol) or []
        return [
            Bar(
                symbol=symbol,
                ts_ns=_ns(r.get("t")) or 0,
                open=r.get("o"), high=r.get("h"),
                low=r.get("l"), close=r.get("c"),
                volume=r.get("v") or 0,
                trade_count=r.get("n"),
                vwap=r.get("vw"),
            )
            for r in rows
        ]

    # --------------------------------------------------------------- options

    def _build_option(self, symbol: str, block: dict, now_ns: int
                      ) -> OptionQuote:
        quote = block.get("latestQuote") or {}
        trade = block.get("latestTrade") or {}
        greeks = block.get("greeks") or {}

        underlying, expiration, put_call, strike = parse_occ_symbol(symbol)
        quote_ns = _ns(quote.get("t"))
        last_ns = _ns(trade.get("t"))

        return OptionQuote(
            symbol=symbol,
            underlying=underlying,
            expiration=expiration,
            strike=strike,
            put_call=put_call,
            bid=quote.get("bp"), bid_size=quote.get("bs"),
            ask=quote.get("ap"), ask_size=quote.get("as"),
            quote_ts_ns=quote_ns,
            quote_age_ms=_age_ms(quote_ns, now_ns),
            last_price=trade.get("p"), last_size=trade.get("s"),
            last_ts_ns=last_ns,
            last_age_ms=_age_ms(last_ns, now_ns),
            implied_volatility=block.get("impliedVolatility"),
            delta=greeks.get("delta"), gamma=greeks.get("gamma"),
            theta=greeks.get("theta"), vega=greeks.get("vega"),
            rho=greeks.get("rho"),
        )

    async def option_chain(
        self,
        underlying: str,
        *,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        contract_type: str | None = None,
        limit: int = 1000,
        max_pages: int = 10,
    ) -> list[OptionQuote]:
        """Real-time option chain with greeks and implied vol.

        `max_pages` is a deliberate cap with a LOUD warning rather than a
        silent truncation: a full chain on a liquid name is thousands of
        contracts, and quietly returning the first page would read as "that
        is the whole chain" (Rule 18).

        Narrow with `expiration_lte` and the strike bounds rather than raising
        the cap. Skew and walls live near the money and near the front
        expiries; pulling every LEAP to compute a 25-delta risk reversal is
        wasted bandwidth.
        """
        params: dict[str, Any] = {"feed": self._options_feed, "limit": limit}
        if expiration_gte:
            params["expiration_date_gte"] = expiration_gte
        if expiration_lte:
            params["expiration_date_lte"] = expiration_lte
        if strike_gte is not None:
            params["strike_price_gte"] = strike_gte
        if strike_lte is not None:
            params["strike_price_lte"] = strike_lte
        if contract_type:
            params["type"] = contract_type

        out: list[OptionQuote] = []
        page_token: str | None = None
        pages = 0
        while pages < max_pages:
            if page_token:
                params["page_token"] = page_token
            payload = await self._get(
                f"/v1beta1/options/snapshots/{underlying}", params)
            now_ns = _now_ns()
            for sym, block in (payload.get("snapshots") or {}).items():
                if not isinstance(block, dict):
                    continue
                try:
                    out.append(self._build_option(sym, block, now_ns))
                except ValueError:
                    logger.warning("Unparseable option symbol %r, skipped",
                                   sym)
            pages += 1
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        else:
            logger.error(
                "Option chain for %s hit the %d-page cap and is TRUNCATED. "
                "Results are incomplete. Narrow with expiration_lte or "
                "strike bounds rather than raising the cap.",
                underlying, max_pages)

        return out
