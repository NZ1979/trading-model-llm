"""Polygon.io / Massive market data client.

NOTE on plan tier: this module is designed for the Stocks Starter plan ($29/mo),
which provides 15-minute delayed data on both REST and WebSocket. In our
architecture Polygon is used ONLY for historical workloads where the delay
doesn't matter:

- Historical pre-market volume baselines (20+ days back) — REST aggregates
- Historical daily bars for the 200-SMA regime filter — REST aggregates
- Backfill of intraday history for indicator warmup

Real-time live bars come from Alpaca's SIP feed (data/alpaca_market_data.py).
The PolygonStreamClient class in this file is included for completeness but
should NOT be used for live signal generation on Stocks Starter — its 15-min
delay would invalidate the gap-and-go time window.

WebSocket
---------
Endpoint: wss://socket.polygon.io/stocks
Auth:     {"action":"auth","params":"<API_KEY>"}
Subscribe: {"action":"subscribe","params":"AM.AAPL,AM.MSFT,..."}

REST
----
Aggregates: GET /v2/aggs/ticker/{symbol}/range/1/minute/{from}/{to}
            ?adjusted=true&sort=asc&limit=50000

Rate limits: Stocks Starter is unlimited per Polygon's pricing page. Backfill
concurrency is capped at 30 in-flight requests as a courtesy buffer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from data.bar_types import MinuteBar

logger = logging.getLogger(__name__)

POLYGON_REST_BASE = "https://api.polygon.io"
POLYGON_WS_URL = "wss://socket.polygon.io/stocks"

ET = ZoneInfo("America/New_York")


def _is_premarket(ts: datetime) -> bool:
    """True if a UTC timestamp falls in pre-market (4:00-9:30 ET)."""
    et = ts.astimezone(ET)
    if et.hour >= 4 and et.hour < 9:
        return True
    if et.hour == 9 and et.minute < 30:
        return True
    return False


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

class PolygonRESTClient:
    """REST client for historical aggregates.

    Async via httpx. Caller must `await client.aclose()` on shutdown, or use
    as an async context manager.
    """

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=POLYGON_REST_BASE,
            timeout=timeout,
            params={"apiKey": api_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolygonRESTClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def _get_with_retry(
        self,
        url: str,
        params: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        """GET with exponential backoff on 429 and 5xx."""
        backoff = 1.0
        for attempt in range(max_retries):
            try:
                resp = await self._client.get(url, params=params or {})
                if resp.status_code == 429:
                    logger.warning("Polygon rate limited, sleeping %.1fs", backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600 and attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError(f"Polygon GET {url} failed after {max_retries} retries")

    async def get_minute_aggregates(
        self,
        symbol: str,
        from_date: str,  # YYYY-MM-DD
        to_date: str,    # YYYY-MM-DD
        adjusted: bool = True,
    ) -> list[dict]:
        """Fetch 1-min OHLCV bars for a symbol over a date range.

        Returns Polygon's raw bar dicts:
          {"t": <epoch_ms>, "o", "h", "l", "c", "v", "vw", "n"}
        Includes pre-market and after-hours bars. Up to 50000 bars per call;
        for 1-min bars over 20 trading days that's well under the limit.
        """
        url = f"/v2/aggs/ticker/{symbol}/range/1/minute/{from_date}/{to_date}"
        params = {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": 50000,
        }
        data = await self._get_with_retry(url, params)
        return data.get("results", []) or []

    async def get_daily_aggregates(
        self,
        symbol: str,
        from_date: str,  # YYYY-MM-DD
        to_date: str,    # YYYY-MM-DD
        adjusted: bool = True,
    ) -> list[dict]:
        """Fetch 1-day OHLCV bars for a symbol over a date range.

        Returns Polygon's raw bar dicts (same shape as minute, but t is
        epoch_ms of the trading day at midnight UTC):
          {"t": <epoch_ms>, "o", "h", "l", "c", "v", "vw", "n"}
        ~300 rows per year. Used for SMA200 + ATR daily context.
        """
        url = f"/v2/aggs/ticker/{symbol}/range/1/day/{from_date}/{to_date}"
        params = {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": 50000,
        }
        data = await self._get_with_retry(url, params)
        return data.get("results", []) or []

    async def get_premarket_volume_history(
        self,
        symbol: str,
        days: int = 20,
    ) -> list[int]:
        """Return per-day pre-market volume sums for the last `days` trading days.

        Used as the denominator for RVOL. The mean of this list is what
        today's pre-market volume gets compared against.

        Returns up to `days` integers, sorted oldest -> newest. Excludes today.
        """
        # Calendar days needed: weekends + holidays = ~1.5x trading days, plus buffer
        end = (datetime.now(timezone.utc) - timedelta(days=1)).date()  # exclude today
        start = end - timedelta(days=int(days * 1.6) + 5)

        bars = await self.get_minute_aggregates(
            symbol=symbol,
            from_date=start.isoformat(),
            to_date=end.isoformat(),
        )

        daily_pm_vol: dict[str, int] = defaultdict(int)
        for bar in bars:
            ts = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc)
            if _is_premarket(ts):
                day_key = ts.astimezone(ET).date().isoformat()
                daily_pm_vol[day_key] += int(bar.get("v", 0))

        # Take the last `days` entries
        sorted_pairs = sorted(daily_pm_vol.items())[-days:]
        return [v for _, v in sorted_pairs]


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------

class PolygonStreamClient:
    """Real-time minute aggregates via WebSocket with auto-reconnect.

    Subscribes to AM.<symbol> for each watchlist ticker. Caller's on_bar
    callback receives one MinuteBar per symbol per minute.
    """

    def __init__(
        self,
        api_key: str,
        symbols: set[str],
        on_bar: Callable[[MinuteBar], Awaitable[None]],
    ) -> None:
        self._api_key = api_key
        self._symbols = {s.upper() for s in symbols}
        self._on_bar = on_bar
        self._running = False

    async def _authenticate(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Polygon sends a "connected" message right after the handshake
        first = json.loads(await ws.recv())
        logger.debug("Polygon WS first message: %s", first)

        await ws.send(json.dumps({"action": "auth", "params": self._api_key}))
        auth_resp = json.loads(await ws.recv())
        # auth_resp is a list with one dict; status should be "auth_success"
        items = auth_resp if isinstance(auth_resp, list) else [auth_resp]
        ok = any(
            isinstance(m, dict) and m.get("status") == "auth_success"
            for m in items
        )
        if not ok:
            raise RuntimeError(f"Polygon auth failed: {auth_resp}")
        logger.info("Polygon WS authenticated")

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Polygon accepts comma-separated channels in one message; for 500
        # symbols this is well under their per-message size limit.
        params = ",".join(f"AM.{s}" for s in self._symbols)
        await ws.send(json.dumps({"action": "subscribe", "params": params}))
        ack = await ws.recv()
        logger.info("Polygon WS subscribed (%d symbols): %s",
                    len(self._symbols), str(ack)[:200])

    async def _process_message(self, raw: str | bytes) -> None:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON Polygon message: %r", raw[:200])
            return

        for msg in messages:
            if msg.get("ev") != "AM":
                continue  # ignore status messages and other event types
            try:
                # Polygon AM message: ev=AM, sym, s=start_ms, e=end_ms,
                # o, h, l, c, v, vw. We use 's' (start) to match Alpaca convention.
                bar = MinuteBar(
                    symbol=msg["sym"],
                    timestamp=datetime.fromtimestamp(msg["s"] / 1000, tz=timezone.utc),
                    open=float(msg["o"]),
                    high=float(msg["h"]),
                    low=float(msg["l"]),
                    close=float(msg["c"]),
                    volume=int(msg["v"]),
                    vwap=float(msg["vw"]) if msg.get("vw") else None,
                )
            except (KeyError, ValueError, TypeError):
                logger.exception("Malformed AM message: %s", msg)
                continue
            try:
                await self._on_bar(bar)
            except Exception:
                logger.exception("on_bar callback failed for %s", bar.symbol)

    async def run(self) -> None:
        """Run the WebSocket loop with exponential-backoff reconnect."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    POLYGON_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**24,  # 16MB; Polygon batches can be large
                ) as ws:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    backoff = 1.0
                    async for raw in ws:
                        await self._process_message(raw)
            except ConnectionClosed as e:
                logger.warning("Polygon WS closed (%s), reconnecting in %.1fs", e, backoff)
            except Exception:
                logger.exception("Polygon WS error, reconnecting in %.1fs", backoff)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Convenience: backfill pre-market baselines for a watchlist
# ---------------------------------------------------------------------------

async def backfill_premarket_baselines(
    api_key: str,
    symbols: set[str],
    days: int = 20,
    concurrency: int = 30,
) -> dict[str, list[int]]:
    """Concurrently fetch pre-market volume history for many symbols.

    Returns dict mapping symbol -> list of daily pre-market volume sums.
    Symbols that fail (delisted, errored) are omitted from the result.

    Concurrency cap of 30 keeps us comfortably under any per-second limits
    even on the Stocks Starter plan; tune up if needed once you confirm
    your plan's actual rate limit.
    """
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, list[int]] = {}

    async with PolygonRESTClient(api_key) as client:
        async def fetch_one(sym: str) -> None:
            async with sem:
                try:
                    vols = await client.get_premarket_volume_history(sym, days=days)
                    if vols:
                        out[sym] = vols
                except Exception as e:
                    logger.warning("Baseline fetch failed for %s: %s", sym, e)

        await asyncio.gather(*(fetch_one(s) for s in symbols))

    logger.info("Pre-market baselines fetched for %d/%d symbols",
                len(out), len(symbols))
    return out


# ---------------------------------------------------------------------------
# Daily aggregates (added in Phase-6.1 backfill fix)
# ---------------------------------------------------------------------------

async def backfill_daily_bars(
    api_key: str,
    symbols: set[str],
    lookback_days: int = 300,
    concurrency: int = 30,
) -> dict[str, "pd.DataFrame"]:
    """Concurrently fetch daily OHLCV history for many symbols.

    Returns dict mapping symbol -> daily DataFrame indexed by UTC midnight.
    Symbols that fail (delisted, errored, no data) are omitted from result.

    Why this exists: the original sequential loop in main.py fetched MINUTE
    bars over 450 calendar days per ticker (~30k rows each, ~1s per request)
    and resampled to daily. With 503 tickers that's 8+ minutes of pure I/O
    even on a fast connection, and Python silently dropped most of them on
    the first run. This helper hits Polygon's daily-bar endpoint (~300 rows
    per ticker, ~50ms per request) with concurrency=30, completing all 503
    in under 30 seconds.
    """
    import pandas as pd  # local import to avoid hard dep at module load

    sem = asyncio.Semaphore(concurrency)
    out: dict[str, pd.DataFrame] = {}

    end = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    # 1.5x buffer for non-trading days + small safety margin
    start = end - timedelta(days=int(lookback_days * 1.5) + 10)

    async with PolygonRESTClient(api_key) as client:
        async def fetch_one(sym: str) -> None:
            async with sem:
                try:
                    bars = await client.get_daily_aggregates(
                        symbol=sym,
                        from_date=start.isoformat(),
                        to_date=end.isoformat(),
                    )
                    if not bars:
                        return
                    df = pd.DataFrame(bars)
                    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
                    df = df.set_index("ts").rename(columns={
                        "o": "open", "h": "high", "l": "low",
                        "c": "close", "v": "volume",
                    })[["open", "high", "low", "close", "volume"]]
                    df = df.tail(lookback_days)
                    if len(df) >= 50:  # need enough bars for SMA200/ATR
                        out[sym] = df
                except Exception as e:
                    logger.warning("Daily backfill failed for %s: %s", sym, e)

        await asyncio.gather(*(fetch_one(s) for s in symbols))

    logger.info("Daily bars fetched for %d/%d symbols", len(out), len(symbols))
    return out
