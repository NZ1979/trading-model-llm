"""Point-in-time historical sentiment loader for the replay harness.

CRITICAL -- Rule 26 partition:

The original M2 design doc specified that this loader queries
``trader-prod``'s ``sentiment`` table on the VPS read-only. Rule 26
(added 2026-05-13 to ``CLAUDE_PREFLIGHT.md``) forbids any LLM-model
session from touching ``/opt/trader/app/trading.db``, including for
historical reads. The design predates the rule.

Resolution path chosen 2026-05-14 (Option A from the design-decision
discussion, refined 2026-05-15 during M2.2 sub-task #7 to Option C of
the schema-collision discussion): a one-time curated SQLite fixture
exported from trader-prod by a SEPARATE gap-and-go-anchored session,
transferred to Godzilla via a non-SSH path, and read by this loader
at runtime. The fixture is a 1:1 mirror of the live production
``sentiment`` table per Rule 26 (no modifying production to match a
fork-specific schema); the loader re-derives ``news_id`` for
Polygon-sourced items via the same ``_polygon_id_to_int`` hash the
live ingestion pipeline uses, so per-article sentiment lookup works
without the original UUID column being present.

The export procedure is documented at
``data/replay/fixtures/README.md``. The fixture path is configurable
via ``ReplayConfig.sentiment_fixture_path``.

This module is INSIDE Rule 26 ("If realistic data is needed for
LLM-fork dev, use synthesized fixtures or a deliberately-curated local
sample DB"), not an exception to it.

Schema (mirrors ``data/news_pipeline.py::_init_db``)::

    CREATE TABLE sentiment (
        news_id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        sentiment INTEGER NOT NULL,    -- -10..+10, per analysis/sentiment.py
        reasoning TEXT,
        headline TEXT,
        scored_at REAL NOT NULL        -- UNIX epoch seconds (float)
    );
    CREATE INDEX idx_sentiment_ticker_time ON sentiment(ticker, scored_at DESC);

``news_id`` is the positive Alpaca news_id for Alpaca-sourced rows and
the negative ``_polygon_id_to_int(polygon_uuid, ticker)`` hash for
Polygon-sourced rows. The two namespaces never collide.

Status: M2.2 sub-task #7 -- fully implemented.

Rule 18: missing fixture is a loud ``FileNotFoundError``; corrupt
fixture is a loud ``sqlite3.DatabaseError``; empty table on
``coverage_window`` is a loud ``RuntimeError``. Per-item lookup
misses do NOT raise from this module -- they manifest as keys absent
from the returned dict, which ``items_to_context_dicts`` surfaces as
a WARNING (Rule 18 option 2, visible degradation at the consumer).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from data.polygon_news import _polygon_id_to_int

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# SQLite default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds and
# 32766 on 3.32+. Stay well below the conservative limit so the loader
# is portable across whatever sqlite the user's Python is linked
# against. With 900 per chunk a 100-ticker x 100-news-items batch
# (10000 items) costs ~12 queries; still trivial.
LOOKUP_CHUNK_SIZE = 900

# latest_sentiment default. Matches live data/news_pipeline.py:146
# (max_age_sec=3600 -- one hour). The M2.1 stub had 86400 which was
# inconsistent with live; this is the correction.
DEFAULT_LATEST_MAX_AGE_SECONDS = 3600


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalSentimentRow:
    """One row from the sentiment fixture.

    Shape mirrors the live ``data/news_pipeline.py::_init_db`` table
    exactly. The replay loader never re-scores historical headlines
    (re-scoring would couple replay results to Claude model drift, per
    design doc § Sentiment-data caveats), so the score on this row is
    the one the live Haiku pipeline wrote at the time the headline
    was first ingested.
    """

    news_id: int
    ticker: str
    sentiment: int  # -10 (most negative) to +10 (most positive)
    scored_at: float  # UNIX epoch seconds (float)


# ---------------------------------------------------------------------------
# Fixture connection
# ---------------------------------------------------------------------------


def open_fixture(path: Path) -> sqlite3.Connection:
    """Open the curated sentiment fixture read-only.

    Uses SQLite URI mode ``mode=ro`` so a buggy loader cannot corrupt
    the fixture by accident (writes raise ``sqlite3.OperationalError:
    attempt to write a readonly database``).

    Args:
        path: filesystem path to the fixture. Typically
            ``ReplayConfig.sentiment_fixture_path`` resolved at replay
            start. ``Path`` accepted; ``str`` also works via
            ``os.fspath``.

    Returns:
        ``sqlite3.Connection`` opened in RO mode.

    Raises:
        FileNotFoundError: fixture file does not exist. The exception
            message names the path and points at the re-export
            procedure in ``data/replay/fixtures/README.md``. Re-export
            from a gap-and-go-anchored session.
        sqlite3.DatabaseError: file exists but is not a valid SQLite
            database (corrupt, half-transferred, wrong file type).
            Propagates after a quick ``PRAGMA schema_version`` probe
            -- the probe forces sqlite3 to actually parse the header
            so the failure surfaces here rather than on the first
            query.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Sentiment fixture not found at {path}. Re-export from "
            "trader-prod per data/replay/fixtures/README.md "
            "(this must run from a gap-and-go-anchored session, "
            "not from the LLM-model fork -- Rule 26)."
        )
    # The mode=ro URI ensures any accidental INSERT/UPDATE/DELETE on
    # this connection raises rather than mutating the fixture. The
    # uri=True flag is required for sqlite3 to interpret the path as a
    # URI; without it the leading 'file:' becomes part of the literal
    # filename and the open silently succeeds in read-write mode.
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    # Force the header parse now so a corrupt file fails loud here
    # rather than on the first business query. PRAGMA schema_version
    # is cheap and always works on a healthy DB.
    try:
        conn.execute("PRAGMA schema_version").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# news_id derivation
# ---------------------------------------------------------------------------


def _news_id_for_polygon(polygon_article_id: str, ticker: str) -> int:
    """Re-hash a Polygon article UUID + ticker to the negative-space news_id
    the live pipeline used at ingestion time.

    Direct wrapper over ``data.polygon_news._polygon_id_to_int`` --
    we route through one function so a future change to the hash
    formula updates both the ingestion side and the replay lookup
    side in lockstep. Treat this as the single source of truth for
    the (UUID, ticker) -> news_id mapping.
    """
    return _polygon_id_to_int(polygon_article_id, ticker)


# ---------------------------------------------------------------------------
# Batch article-level lookup
# ---------------------------------------------------------------------------


def lookup_article_sentiments(
    conn: sqlite3.Connection,
    items: list,
) -> dict[tuple[str, str], float]:
    """Look up sentiment scores for a batch of HistoricalNewsItems.

    Resolves each item's ``polygon_article_id`` + ``ticker`` to the
    live ``news_id`` via ``_news_id_for_polygon`` (the same hash the
    live pipeline uses at ingestion), batches into SQLite ``IN``
    queries, and returns a dict suitable for
    ``data.replay.historical_news.items_to_context_dicts``.

    The dict's value type is ``float`` to match the contract that
    ``items_to_context_dicts`` was shipped with in sub-task #6, even
    though the underlying ``sentiment`` column is ``INTEGER``. The
    integer-to-float conversion is exact for the -10..+10 range.

    Args:
        conn: connection from ``open_fixture``.
        items: list of ``HistoricalNewsItem`` (typed as ``list`` to
            avoid a circular import; the fields touched are
            ``polygon_article_id`` and ``ticker``).

    Returns:
        ``dict[(polygon_article_id, ticker), sentiment_as_float]``.
        Items with no matching row in the fixture (article missed by
        the live ingestion pipeline, or pre-coverage window) are
        simply absent from the dict -- they surface as a WARNING
        through ``items_to_context_dicts``' Rule 18 option-2 path.
        Items with ``polygon_article_id is None`` are skipped here
        too (they cannot be hashed); the same downstream WARNING
        applies.

    Raises:
        sqlite3.DatabaseError: the connection's underlying file got
            corrupted between ``open_fixture`` and this call.
            Propagated; this is a data-integrity failure, not a
            data-gap.
    """
    # Build the (article_id, ticker) -> news_id projection. Items
    # without an article_id (e.g. a Polygon article with no id field)
    # cannot be looked up and are skipped silently here; the consumer
    # surfaces the miss.
    id_to_key: dict[int, tuple[str, str]] = {}
    for it in items:
        article_id = getattr(it, "polygon_article_id", None)
        ticker = getattr(it, "ticker", None)
        if not article_id or not ticker:
            continue
        news_id = _news_id_for_polygon(article_id, ticker)
        # Multiple items hashing to the same news_id (extremely
        # unlikely 63-bit collision, but possible) would have the
        # later one win in id_to_key. That's correct: the SELECT
        # returns one row per news_id, so the dict has at most one
        # entry per article anyway.
        id_to_key[news_id] = (article_id, ticker)

    if not id_to_key:
        return {}

    out: dict[tuple[str, str], float] = {}
    all_ids = list(id_to_key.keys())
    for i in range(0, len(all_ids), LOOKUP_CHUNK_SIZE):
        chunk = all_ids[i : i + LOOKUP_CHUNK_SIZE]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT news_id, ticker, sentiment FROM sentiment "
            f"WHERE news_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for news_id, db_ticker, sentiment in rows:
            key = id_to_key.get(int(news_id))
            if key is None:
                # Shouldn't happen (we asked for exactly these
                # news_ids) but defends against driver quirks.
                continue
            expected_ticker = key[1]
            if db_ticker != expected_ticker:
                # Hash collision (vanishingly unlikely) or fixture
                # corruption (a news_id mapped to a different
                # ticker). Either way, don't claim the score belongs
                # to this article -- skip with a WARNING. Better to
                # surface a sentiment miss downstream than feed the
                # LLM a wrong-ticker score.
                logger.warning(
                    "historical_sentiment: news_id=%d in fixture has "
                    "ticker=%r but item requested ticker=%r; skipping",
                    news_id, db_ticker, expected_ticker,
                )
                continue
            out[key] = float(sentiment)
    return out


# ---------------------------------------------------------------------------
# Latest-sentiment-for-ticker (point-in-time mirror of live function)
# ---------------------------------------------------------------------------


def latest_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_et: datetime,
    max_age_seconds: int = DEFAULT_LATEST_MAX_AGE_SECONDS,
) -> int | None:
    """Return the most recent sentiment score for ``ticker`` at ``as_of_et``.

    Mirrors ``data/news_pipeline.py::latest_sentiment`` -- same query
    shape, point-in-time-corrected. Used by the replay
    ``context_builder`` where the live system calls the live
    function.

    Args:
        conn: connection from ``open_fixture``.
        ticker: equity symbol. Upper-cased here to match the live
            function's behavior.
        as_of_et: replay tick timestamp; tz-aware (typically
            ``America/New_York``, but any tz works since we convert
            to epoch seconds). Rows with ``scored_at > as_of`` are
            excluded -- point-in-time correctness, no peeking at
            future sentiment.
        max_age_seconds: rows older than ``as_of - max_age_seconds``
            are excluded. Default 3600s (one hour) matches the live
            function. Callers needing a longer window pass it
            explicitly.

    Returns:
        Integer sentiment in [-10, +10], or ``None`` if no matching
        row exists. ``None`` is the right "no signal" sentinel and
        matches the live function's contract.

    Raises:
        ValueError: ``as_of_et`` is naive; ``max_age_seconds < 0``.
            Caller bugs, fail loud.
    """
    if as_of_et.tzinfo is None:
        raise ValueError(
            "latest_sentiment requires tz-aware as_of_et; got naive"
        )
    if max_age_seconds < 0:
        raise ValueError(
            f"max_age_seconds must be >= 0, got {max_age_seconds}"
        )
    as_of_epoch = as_of_et.timestamp()
    cutoff_epoch = as_of_epoch - max_age_seconds
    row = conn.execute(
        "SELECT sentiment FROM sentiment "
        "WHERE ticker = ? AND scored_at <= ? AND scored_at >= ? "
        "ORDER BY scored_at DESC LIMIT 1",
        (ticker.upper(), as_of_epoch, cutoff_epoch),
    ).fetchone()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Coverage window
# ---------------------------------------------------------------------------


def coverage_window(conn: sqlite3.Connection) -> tuple[float, float]:
    """Return ``(min_scored_at, max_scored_at)`` of the fixture's coverage.

    Used at replay start to verify the requested
    ``[start_date, end_date]`` window is fully covered by the
    fixture. The replay loop's pre-flight aborts loudly if the
    range extends past ``max_scored_at`` -- never silently degrades
    to empty-sentiment for the uncovered tail (Rule 18).

    Returns:
        Tuple of two floats, each a UNIX epoch-second timestamp.

    Raises:
        RuntimeError: the fixture is empty. An empty sentiment
            fixture is unusable; better to fail loud at replay
            start than to score every news item as 0.0 silently.
            Re-export from trader-prod per
            ``data/replay/fixtures/README.md``.
    """
    row = conn.execute(
        "SELECT MIN(scored_at), MAX(scored_at), COUNT(*) FROM sentiment"
    ).fetchone()
    if row is None or row[2] == 0 or row[0] is None or row[1] is None:
        raise RuntimeError(
            "Sentiment fixture is empty; coverage_window has no rows "
            "to report. Re-export from trader-prod per "
            "data/replay/fixtures/README.md."
        )
    return float(row[0]), float(row[1])
