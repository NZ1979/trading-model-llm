"""News -> sentiment pipeline.

Glues news_feed.AlpacaNewsFeed to analysis.sentiment.SentimentScorer with a
batching queue. The WebSocket callback is non-blocking (just enqueues), and a
separate task drains the queue every FLUSH_INTERVAL seconds and sends a batch
to Claude.

Why batch instead of scoring each headline immediately?
-------------------------------------------------------
1. Cost: one API call with 15 headlines costs roughly the same as one call
   with 1 headline (system prompt dominates), so batching is ~15x cheaper.
2. Latency: 60s flush is fine for intraday equity signals. We're not HFT;
   the signal engine reads the latest sentiment per ticker on its own loop.
3. Rate limits: Haiku 4.5 has request-per-minute caps. Batching keeps us
   well under them even on busy news days.

Pipeline writes results to SQLite. The signal engine reads the most recent
sentiment per ticker via storage.db.latest_sentiment().
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from analysis.sentiment import SentimentScorer
from data.news_feed import AlpacaNewsFeed

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 60
MAX_BATCH_SIZE = 20  # cap per Claude call to keep latency predictable
MAX_QUEUE_SIZE = 500  # backpressure: drop oldest if news floods


class NewsSentimentPipeline:
    """Runs the news feed and sentiment scorer concurrently.

    Lifecycle:
        pipeline = NewsSentimentPipeline(...)
        await pipeline.start()  # runs forever; cancel the task to stop
    """

    def __init__(
        self,
        alpaca_key: str,
        alpaca_secret: str,
        anthropic_key: str,
        watchlist: set[str],
        db_path: str | Path = "trading.db",
    ) -> None:
        self._db_path = Path(db_path)
        self._scorer = SentimentScorer(api_key=anthropic_key)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(MAX_QUEUE_SIZE)
        self._feed = AlpacaNewsFeed(
            api_key=alpaca_key,
            api_secret=alpaca_secret,
            watchlist=watchlist,
            on_news=self._enqueue,
        )
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sentiment (
                    news_id INTEGER PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    sentiment INTEGER NOT NULL,
                    reasoning TEXT,
                    headline TEXT,
                    scored_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sentiment_ticker_time "
                "ON sentiment(ticker, scored_at DESC)"
            )

    async def _enqueue(self, news: dict[str, Any]) -> None:
        """WebSocket callback. Must not block."""
        try:
            self._queue.put_nowait(news)
        except asyncio.QueueFull:
            # Drop the oldest so we always score the freshest news
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(news)
                logger.warning("News queue full; dropped oldest")
            except asyncio.QueueEmpty:
                pass

    async def _flush_loop(self) -> None:
        """Every FLUSH_INTERVAL seconds, batch-score whatever is queued."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SEC)

            batch: list[dict[str, Any]] = []
            while not self._queue.empty() and len(batch) < MAX_BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            if not batch:
                continue

            logger.info("Flushing %d headlines to Claude", len(batch))
            # Run the (synchronous) Anthropic SDK call in a thread so we
            # don't block the event loop.
            results = await asyncio.to_thread(self._scorer.score_batch, batch)

            if not results:
                logger.warning("Sentiment batch returned empty; skipping persist")
                continue

            self._persist(results)

    def _persist(self, results: list) -> None:
        now = time.time()
        rows = [
            (r.news_id, r.ticker, r.sentiment, r.reasoning, r.headline, now)
            for r in results
        ]
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO sentiment "
                "(news_id, ticker, sentiment, reasoning, headline, scored_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    async def start(self) -> None:
        """Run feed and flush loop concurrently. Returns only on cancel."""
        await asyncio.gather(
            self._feed.run(),
            self._flush_loop(),
        )


def latest_sentiment(db_path: str | Path, ticker: str, max_age_sec: int = 3600) -> int | None:
    """Return the most recent sentiment for `ticker` within max_age_sec.

    Returns None if no recent score exists. The signal engine calls this
    on its own loop; it does not subscribe to pipeline events directly.
    """
    cutoff = time.time() - max_age_sec
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sentiment FROM sentiment "
            "WHERE ticker = ? AND scored_at >= ? "
            "ORDER BY scored_at DESC LIMIT 1",
            (ticker.upper(), cutoff),
        ).fetchone()
    return row[0] if row else None
