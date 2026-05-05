"""Finnhub HTTP client + scheduled fetchers.

Wave 1A scope (this file as initially deployed): Earnings Calendar only.
Earnings is wired as a hard veto on gap-and-go signals — trading gap-and-go
on a stock with earnings before/during/after the bar is binary-bet exposure
that the strategy isn't designed for. The 8:30 ET backfill window pulls a
14-day forward window of earnings events into the `catalysts` table;
`_evaluate_and_execute` calls `is_earnings_day(ticker, today)` to gate.

Future waves (1B onward) will add methods to FinnhubClient for News
Sentiment, Major Press Releases, Company News, Recommendation Trends,
Insider Transactions, Social Sentiment, Basic Financials, Investment Themes,
FDA Calendar — all on the Fundamental-1 plan, all verified working
2026-05-03 via scripts/test_finnhub_endpoints.py.

Rate limits enforced (Fundamental-1, US):
  - 300 requests/minute (plan tier cap)
  - 30 requests/second (Finnhub global hard cap)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
HTTP_TIMEOUT_SEC = 15

# Rate limit constants
PLAN_PER_MIN = 300       # Fundamental-1 cap
GLOBAL_PER_SEC = 30      # Finnhub's global hard cap


class FinnhubClient:
    """Async Finnhub HTTP client with built-in rate limiting.

    Usage:
        client = FinnhubClient(api_key)
        await client.__aenter__()
        try:
            data = await client.get("/calendar/earnings",
                                    {"from": "2026-05-04", "to": "2026-05-18"})
        finally:
            await client.__aexit__(None, None, None)

    Or as an async context manager:
        async with FinnhubClient(api_key) as c:
            data = await c.get(...)
    """

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("FinnhubClient requires a non-empty api_key")
        self._key = api_key
        self._session: aiohttp.ClientSession | None = None
        # Sliding-window buckets: monotonic timestamps of recent requests
        self._minute_window: deque[float] = deque()
        self._second_window: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "FinnhubClient":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()
        self._session = None

    async def _wait_for_rate_capacity(self) -> None:
        """Block until both rate limits have capacity for one more request.

        Sliding-window enforcement: keep monotonic timestamps of recent
        requests, drop expired entries before each check, sleep if at the cap.
        Holds the lock across the sleep — simple but means concurrent callers
        serialize on heavy load. Acceptable for this project's call volume
        (a few dozen requests per day during the 8:30 ET refresh).
        """
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._minute_window and now - self._minute_window[0] >= 60:
                    self._minute_window.popleft()
                while self._second_window and now - self._second_window[0] >= 1:
                    self._second_window.popleft()
                wait_sec = 0.0
                if len(self._minute_window) >= PLAN_PER_MIN:
                    wait_sec = max(wait_sec, 60 - (now - self._minute_window[0]))
                if len(self._second_window) >= GLOBAL_PER_SEC:
                    wait_sec = max(wait_sec, 1 - (now - self._second_window[0]))
                if wait_sec <= 0:
                    self._minute_window.append(now)
                    self._second_window.append(now)
                    return
                await asyncio.sleep(wait_sec + 0.01)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET <BASE_URL><path>?<params> with auth, returning parsed JSON.

        Returns None on HTTP error or network error (logged loudly per Rule 18).
        Retries are not implemented; the caller decides whether to retry.
        """
        if self._session is None:
            raise RuntimeError(
                "FinnhubClient not initialized; call __aenter__ first or use "
                "as async context manager"
            )
        await self._wait_for_rate_capacity()
        p = dict(params or {})
        p["token"] = self._key
        url = f"{BASE_URL}{path}?{urlencode(p)}"
        try:
            async with self._session.get(url, timeout=HTTP_TIMEOUT_SEC) as resp:
                body_text = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "Finnhub %s failed: HTTP %d body=%s",
                        path, resp.status, body_text[:300],
                    )
                    return None
                try:
                    return json.loads(body_text)
                except json.JSONDecodeError:
                    logger.error(
                        "Finnhub %s returned non-JSON: %s",
                        path, body_text[:300],
                    )
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("Finnhub %s network error: %s", path, e)
            return None

    # -------- Wave 1A: earnings calendar --------

    async def get_earnings_calendar(
        self,
        date_from: str,
        date_to: str,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get earnings calendar for date_from..date_to (YYYY-MM-DD).

        If `symbol` is provided, scopes to that one ticker. Otherwise returns
        all earnings in the date window (typically faster + cheaper than
        503 separate per-symbol calls).

        Returns the list of earnings events (possibly empty); returns [] on
        error. Each event has keys like:
            symbol, date, hour ('bmo'/'amc'/'dmh'), epsActual, epsEstimate,
            revenueActual, revenueEstimate, quarter, year
        """
        params: dict[str, Any] = {"from": date_from, "to": date_to}
        if symbol:
            params["symbol"] = symbol
        data = await self.get("/calendar/earnings", params)
        if not isinstance(data, dict):
            return []
        return data.get("earningsCalendar") or []


# ---------------------------------------------------------------------------
# Schema + persistence helpers (kept module-level so they can be imported
# without holding a FinnhubClient instance)
# ---------------------------------------------------------------------------

def _init_catalysts_table(db_path: str | Path) -> None:
    """Idempotent schema for the `catalysts` table.

    Used as a unified store for any kind of date-bound event that informs
    a trade decision: earnings (Wave 1A), FDA meetings, upgrades/downgrades
    if/when those become available, etc. Each row is uniquely identified by
    (ticker, event_date, source, event_type) so re-fetches are idempotent.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalysts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ticker TEXT NOT NULL,
                event_date TEXT NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                UNIQUE(ticker, event_date, source, event_type)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_catalysts_ticker_date "
            "ON catalysts(ticker, event_date)"
        )


async def refresh_earnings_calendar(
    client: FinnhubClient,
    watchlist: set[str],
    db_path: str | Path,
    days_forward: int = 14,
) -> int:
    """Fetch upcoming earnings for the watchlist, persist to catalysts table.

    Strategy: query Finnhub once for the entire forward window without symbol
    filter (single API call), then filter in-memory to watchlist. This is far
    cheaper than 503 separate per-symbol calls.

    Returns the count of newly-added rows. Idempotent: re-running the same
    window inserts 0 new rows on the second call (UNIQUE constraint).
    """
    _init_catalysts_table(db_path)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    end_date = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() + days_forward * 86400)
    )
    events = await client.get_earnings_calendar(today, end_date)
    if not events:
        # Could be a real "no earnings this window" or an API hiccup — log
        # loudly per Rule 18; the daily routine will retry tomorrow regardless.
        logger.warning(
            "Finnhub earnings calendar refresh returned 0 events (%s..%s)",
            today, end_date,
        )
        return 0

    watchlist_upper = {s.upper() for s in watchlist}
    rows = []
    now = time.time()
    in_watchlist = 0
    for ev in events:
        ticker = (ev.get("symbol") or "").upper()
        if ticker not in watchlist_upper:
            continue
        in_watchlist += 1
        event_date = ev.get("date") or ""
        if not event_date:
            continue
        rows.append((
            now, ticker, event_date,
            "finnhub", "earnings",
            json.dumps(ev),
        ))

    if not rows:
        logger.info(
            "Finnhub earnings refresh: %d events fetched, 0 in watchlist (%s..%s)",
            len(events), today, end_date,
        )
        return 0

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM catalysts").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO catalysts "
            "(ts, ticker, event_date, source, event_type, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        after = conn.execute("SELECT COUNT(*) FROM catalysts").fetchone()[0]
    added = after - before
    logger.info(
        "Finnhub earnings refresh: %d events fetched, %d in watchlist, "
        "%d new rows persisted (%s..%s)",
        len(events), in_watchlist, added, today, end_date,
    )
    return added


def is_earnings_day(
    db_path: str | Path,
    ticker: str,
    date_str: str,
) -> bool:
    """Return True if `ticker` has an earnings event on `date_str` (YYYY-MM-DD).

    Returns False if the catalysts table doesn't exist yet (e.g., refresh
    hasn't run today) or no row matches. The False default is intentional:
    a missing catalysts table should NOT block trading (fail-soft on the
    enrichment data; fail-loud only on the trading-critical path).
    """
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM catalysts "
                "WHERE ticker = ? AND event_date = ? "
                "  AND source = 'finnhub' AND event_type = 'earnings' LIMIT 1",
                (ticker.upper(), date_str),
            ).fetchone()
        except sqlite3.OperationalError:
            # Table doesn't exist yet
            return False
    return row is not None
