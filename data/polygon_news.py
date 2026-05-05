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
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 300  # 5 min. Polygon updates ~hourly so faster polling
                         # doesn't get more news; this keeps load light.
HTTP_TIMEOUT_SEC = 30
PER_PAGE_LIMIT = 1000

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
