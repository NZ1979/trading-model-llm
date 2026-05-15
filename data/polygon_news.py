"""Polygon News -> sentiment pipeline (supplementary feed).

Runs alongside NewsSentimentPipeline (Alpaca/Benzinga + Claude Haiku scoring).
Both pipelines write to the same `sentiment` SQLite table; latest_sentiment()
returns the most recent across both, regardless of source.

Why supplementary?
------------------
  - Polygon News updates at roughly an hourly cadence, so it can't replace
    the real-time Alpaca/Benzinga stream. But it picks up coverage gaps:
    the 2026-05-01 RCA found that headlines for ORCL and PM never reached
    our sentiment table despite being big movers, while Polygon's coverage
    of those names is broad.
  - Polygon's news endpoint includes pre-scored sentiment per ticker
    ("positive" / "negative" / "neutral") with sentiment_reasoning text.
    Articles ingested through this pipeline skip the Claude Haiku scoring
    step entirely, which saves API spend.

Sentiment mapping
-----------------
Polygon's categorical sentiment is mapped to a conservative integer scale:
  "positive"  -> +4
  "negative"  -> -4
  "neutral"   ->  0

+4 clears the gap_and_go +/-3 sentiment threshold (allowing gap-and-go signals
to fire on Polygon-scored news), but NOT the pullback +/-5 threshold (so
pullback entries still require Haiku-grade scoring of Benzinga news). This
is intentional: Polygon's three-class sentiment is less granular than
Haiku's -10..+10 scale, so we treat it as moderately confident, not
strongly confident.

Dedup with Alpaca news
----------------------
Alpaca news_ids are positive integers. Polygon article IDs are UUID-like
strings. We hash to a negative 63-bit integer to avoid collision with
Alpaca IDs in the same `sentiment.news_id` PRIMARY KEY column.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
import httpx

from data.polygon_feed import POLYGON_REST_BASE, _scrub_apikey

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 300  # 5 min. Polygon updates ~hourly so faster polling
                         # doesn't get more news; this keeps load light.
HTTP_TIMEOUT_SEC = 30
PER_PAGE_LIMIT = 1000

# Pagination safety cap for fetch_news_range. Polygon caps each page at
# PER_PAGE_LIMIT (1000) articles; 20 pages = 20,000 articles. For a
# single ticker over the M2 30-day replay window this is comfortably above
# any realistic news volume. Exceeding the cap means either Polygon
# misordered results, our date math is wrong, or we're scanning a window
# pathologically larger than intended -- all conditions that should fail
# loud per Rule 18 rather than truncate silently.
NEWS_PAGINATION_MAX_PAGES = 20

SENTIMENT_MAP = {
    "positive": 4,
    "negative": -4,
    "neutral": 0,
}


def _polygon_id_to_int(polygon_id: str, ticker: str = "") -> int:
    """Convert Polygon's UUID-like string ID + ticker to a negative 63-bit int.

    Why include the ticker in the hash?
        Polygon articles often contain multiple insights (one per ticker
        mentioned). The sentiment table's news_id column is PRIMARY KEY,
        so we need a unique news_id per (article, ticker) pair — otherwise
        only the first ticker's sentiment would persist.

    Why negative?
        Alpaca news_ids occupy the positive int space; we use negative space
        to avoid collisions in the shared sentiment.news_id PRIMARY KEY column.

    Blake2b 8-byte digest; collision probability across millions of articles
    is statistically negligible at our scale.
    """
    key = f"{polygon_id}|{ticker}".encode("utf-8")
    h = hashlib.blake2b(key, digest_size=8).digest()
    n = int.from_bytes(h, byteorder="big", signed=False)
    return -(n & 0x7FFFFFFFFFFFFFFF)


class PolygonNewsPipeline:
    """Polls Polygon's news endpoint and persists per-ticker sentiment.

    Lifecycle:
        pipeline = PolygonNewsPipeline(polygon_key, watchlist, db_path)
        await pipeline.start()  # runs forever; cancel the task to stop
    """

    def __init__(
        self,
        polygon_key: str,
        watchlist: set[str],
        db_path: str | Path = "trading.db",
    ) -> None:
        self._key = polygon_key
        self._watchlist = {s.upper() for s in watchlist}
        self._db_path = Path(db_path)
        # Cursor: only fetch articles published after this UTC timestamp.
        # Initialized on first poll to "1 hour ago" so we backfill any
        # very-recent news on boot without flooding history.
        self._cursor_published_utc: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _fetch_page(self, since_utc: str | None) -> list[dict[str, Any]]:
        """Fetch one page of news from Polygon's news endpoint.

        Returns the parsed `results` list. Empty list on HTTP error or
        unexpected response shape (logs the issue but does not raise — the
        outer poll loop continues).
        """
        assert self._session is not None
        params: dict[str, Any] = {
            "limit": PER_PAGE_LIMIT,
            "order": "asc",
            "sort": "published_utc",
            "apiKey": self._key,
        }
        if since_utc:
            params["published_utc.gte"] = since_utc
        url = f"https://api.polygon.io/v2/reference/news?{urlencode(params)}"
        try:
            async with self._session.get(url, timeout=HTTP_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(
                        "Polygon news fetch failed: HTTP %d: %s",
                        resp.status, body[:300],
                    )
                    return []
                data = await resp.json()
                return data.get("results", []) or []
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("Polygon news network error: %s", e)
            return []

    def _process_articles(self, articles: list[dict[str, Any]]) -> list[tuple]:
        """Extract per-ticker sentiment rows from articles.

        Each article may have multiple insights (one per ticker mentioned).
        We emit one row per (article, watchlist-ticker) pair. Articles
        without insights, with non-mappable sentiment, or for tickers not
        in our watchlist are silently skipped.

        Returns list of tuples ready for executemany INSERT OR IGNORE:
            (news_id_int, ticker, sentiment_int, reasoning, headline, scored_at)
        """
        now = time.time()
        rows: list[tuple] = []
        for art in articles:
            polygon_id = art.get("id", "")
            if not polygon_id:
                continue
            headline = (art.get("title") or "")[:500]  # cap length defensively
            insights = art.get("insights") or []
            for ins in insights:
                ticker = (ins.get("ticker") or "").upper()
                if ticker not in self._watchlist:
                    continue
                sent_str = (ins.get("sentiment") or "").lower()
                if sent_str not in SENTIMENT_MAP:
                    continue
                sentiment = SENTIMENT_MAP[sent_str]
                reasoning = (ins.get("sentiment_reasoning") or "")[:1000]
                # news_id depends on (article, ticker) so multi-ticker articles
                # don't collide on sentiment.news_id PRIMARY KEY.
                news_id = _polygon_id_to_int(polygon_id, ticker)
                rows.append((news_id, ticker, sentiment, reasoning, headline, now))
        return rows

    def _persist(self, rows: list[tuple]) -> int:
        """Insert rows into sentiment table. Returns count of newly added rows.

        Uses INSERT OR IGNORE so duplicate (news_id, ticker) pairs from
        repeated polls are silently dropped. The poll loop is therefore
        idempotent — replaying the same window does not double-count news.
        """
        if not rows:
            return 0
        with sqlite3.connect(self._db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO sentiment "
                "(news_id, ticker, sentiment, reasoning, headline, scored_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            after = conn.execute("SELECT COUNT(*) FROM sentiment").fetchone()[0]
        return after - before

    async def _poll_loop(self) -> None:
        """Run forever: poll Polygon, persist new sentiment, sleep, repeat."""
        # On first boot, look back 1 hour. Subsequent polls advance the
        # cursor to the latest published_utc seen.
        self._cursor_published_utc = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)
        )
        while True:
            try:
                articles = await self._fetch_page(self._cursor_published_utc)
                rows = self._process_articles(articles)
                added = self._persist(rows)
                if articles:
                    # Advance cursor to the latest article's timestamp so
                    # the next poll only fetches strictly newer items.
                    last_ts = max(
                        (a.get("published_utc", "") for a in articles),
                        default=self._cursor_published_utc,
                    )
                    if last_ts:
                        self._cursor_published_utc = last_ts
                logger.info(
                    "Polygon news poll: %d articles, %d sentiment rows added "
                    "(cursor=%s)",
                    len(articles), added, self._cursor_published_utc,
                )
            except Exception:
                logger.exception("Polygon news poll error (continuing)")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def start(self) -> None:
        """Run the poll loop forever. Call from asyncio.create_task()."""
        async with aiohttp.ClientSession() as session:
            self._session = session
            await self._poll_loop()


# ---------------------------------------------------------------------------
# fetch_news_range -- generic Polygon News range helper (M2.2 sub-task #6)
# ---------------------------------------------------------------------------
# Why httpx here when PolygonNewsPipeline (above) uses aiohttp?
#
# The pipeline is the legacy live-streaming path; this helper backs the
# M2 replay harness (data/replay/historical_news.py). The replay stack
# standardised on httpx for fetch_aggs, Polygon Reference, FRED, and
# market_context, with a shared _scrub_apikey helper and a shared
# MockTransport-based test idiom. Sticking to that stack means one
# error-handling style for the M2 replay surface and one mocking pattern
# in tests. The aiohttp pipeline above is untouched.


async def fetch_news_range(
    ticker: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    api_key: str,
    timeout_s: float = 30.0,
    max_retries: int = 3,
    max_pages: int = NEWS_PAGINATION_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Fetch all Polygon News articles for ``ticker`` over ``[start_utc, end_utc]``.

    Follows Polygon's ``next_url`` pagination to completion (capped at
    ``max_pages`` to fail loud rather than scan an unbounded server-side
    cursor). One ticker per call; the caller fans out across the
    watchlist with bounded concurrency.

    Args:
        ticker: equity symbol; case-sensitive on Polygon's side.
        start_utc: inclusive lower bound for ``published_utc.gte``. Must
            be tz-aware UTC.
        end_utc: inclusive upper bound for ``published_utc.lte``. Must
            be tz-aware UTC and >= ``start_utc``.
        api_key: Polygon API key. Always required (no env-var fallback;
            keeps the helper pure and lets the loader own credential
            resolution).
        timeout_s: per-request timeout. 30s comfortably covers a 1000-
            article page even on a slow link.
        max_retries: retries on 429 and 5xx with exponential backoff
            (1s, 2s, 4s). 4xx other than 429/404 does not retry --
            raises immediately. 404 returns an empty list (unknown
            ticker / no coverage; matches the Polygon Reference 404
            convention in ticker_metadata).
        max_pages: pagination safety cap. Exceeding it raises loud.

    Returns:
        List of Polygon article dicts (the raw ``results`` payload
        elements: ``id``, ``published_utc``, ``title``, ``article_url``,
        ``publisher``, ``tickers``, ``insights``, etc.). Empty list on
        404 or a valid 200 with no matching articles.

    Raises:
        ValueError: ``end_utc < start_utc``; either bound is naive
            (caller bug -- forces explicit timezone reasoning).
        RuntimeError: 4xx other than 429/404 (bad key, malformed
            request, plan-tier mismatch); 5xx persisting after retries;
            429 persisting after retries; transient network error
            persisting after retries; pagination cap exceeded.

    Rule 22: URL strings in raised messages pass through
    ``_scrub_apikey`` (including Polygon's ``next_url`` which itself
    contains the apiKey query param). ``from None`` on the re-raise
    suppresses the httpx exception chain so its default leaky message
    cannot propagate via ``__cause__``.
    """
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError(
            "fetch_news_range requires tz-aware UTC datetimes; "
            f"got start_utc={start_utc!r}, end_utc={end_utc!r}"
        )
    if end_utc < start_utc:
        raise ValueError(
            f"end_utc {end_utc.isoformat()} is before "
            f"start_utc {start_utc.isoformat()}"
        )
    if not api_key:
        raise RuntimeError(
            "fetch_news_range requires a non-empty api_key"
        )

    initial_url = f"{POLYGON_REST_BASE}/v2/reference/news"
    initial_params: dict[str, Any] = {
        "ticker": ticker,
        "published_utc.gte": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_utc.lte": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": PER_PAGE_LIMIT,
        "order": "asc",
        "sort": "published_utc",
        "apiKey": api_key,
    }

    articles: list[dict[str, Any]] = []
    next_url: str | None = None
    page_count = 0

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        while True:
            if page_count >= max_pages:
                # Loud failure: better to abort than scan an unbounded
                # cursor (Rule 18 option 3 > option 4).
                raise RuntimeError(
                    f"Polygon News pagination exceeded {max_pages} pages "
                    f"for {ticker} {start_utc.isoformat()}..{end_utc.isoformat()}; "
                    "refusing to scan further -- check range or raise max_pages"
                )

            if next_url is None:
                url_for_request = initial_url
                params_for_request: dict[str, Any] | None = initial_params
            else:
                # Polygon's next_url already contains query params (incl.
                # apiKey). Pass it through verbatim; httpx will not
                # re-attach base_url because we use the absolute URL.
                url_for_request = next_url
                params_for_request = None

            payload = await _news_page_with_retry(
                client=client,
                url=url_for_request,
                params=params_for_request,
                ticker=ticker,
                max_retries=max_retries,
            )

            if payload is None:
                # 404 on the very first page -> unknown ticker. Soft-fail
                # to empty list. (Subsequent next_url pages cannot 404 in
                # practice; if Polygon ever does that we still soft-fail
                # the same way -- the alternative would be a partial-list
                # ambiguity we don't want.)
                return articles

            page_results = payload.get("results") or []
            if not isinstance(page_results, list):
                raise RuntimeError(
                    f"Polygon News returned non-list results for {ticker}: "
                    f"{type(page_results).__name__}"
                )
            articles.extend(page_results)
            page_count += 1

            next_url_raw = payload.get("next_url")
            if not next_url_raw:
                break
            # Polygon's next_url omits the apiKey on some plans;
            # re-append from our key so the next GET authenticates.
            # Idempotent if apiKey is already present (Polygon returns
            # it on Starter+ plans).
            if "apiKey=" not in next_url_raw:
                joiner = "&" if "?" in next_url_raw else "?"
                next_url = f"{next_url_raw}{joiner}apiKey={api_key}"
            else:
                next_url = next_url_raw

    return articles


async def _news_page_with_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None,
    ticker: str,
    max_retries: int,
) -> dict[str, Any] | None:
    """Fetch one page of Polygon News with retry. Returns parsed JSON or None on 404.

    Helper for fetch_news_range. Encapsulates the retry / scrub / re-raise
    block so the main pagination loop stays readable. Returns ``None``
    only for HTTP 404 (unknown ticker on the first page); all other
    failure modes raise RuntimeError per Rule 18.
    """
    backoff = 1.0
    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                last_err = RuntimeError(
                    f"Polygon News HTTP 429 (rate limited) for "
                    f"{_scrub_apikey(str(resp.url))}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                break
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Polygon News returned non-object payload for "
                    f"{ticker}: {type(data).__name__}"
                )
            return data
        except httpx.HTTPStatusError as e:
            if 500 <= e.response.status_code < 600:
                last_err = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                break
            # 4xx other than 429/404: don't retry; raise loud + scrubbed.
            safe_url = _scrub_apikey(str(e.response.url))
            body = _scrub_apikey(e.response.text[:200])
            raise RuntimeError(
                f"Polygon News HTTP {e.response.status_code} for {safe_url}: "
                f"{body}"
            ) from None
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
        ) as e:
            last_err = e
            if attempt < max_retries - 1:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            break

    raise RuntimeError(
        f"Polygon News fetch failed after {max_retries} retries for "
        f"{ticker}: {_scrub_apikey(str(last_err))}"
    ) from None
