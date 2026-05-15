"""Tests for data/replay/historical_sentiment.py (M2.2 sub-task #7).

Covers:
  - open_fixture: missing file FileNotFoundError loud + names path +
    points at re-export procedure; corrupt file sqlite3.DatabaseError
    loud; valid file opens read-only AND a write attempt raises
    OperationalError
  - _news_id_for_polygon: identical to _polygon_id_to_int (single
    source of truth)
  - lookup_article_sentiments: empty input -> empty dict; single hit;
    multi-batch hit; chunking past LOOKUP_CHUNK_SIZE; items with no
    polygon_article_id skipped; items absent from fixture absent from
    dict; ticker-mismatch row warned + skipped (defense against hash
    collision / fixture corruption); int -> float conversion at the
    boundary (matches sub-task #6 contract)
  - latest_sentiment: hit within window, miss when too old, miss when
    no rows for ticker, point-in-time exclusion of scored_at > as_of,
    ORDER BY DESC returns the latest, default max_age_seconds=3600
    matches live, custom max_age, ticker upper-case normalization,
    naive as_of raises, negative max_age raises
  - coverage_window: populated returns correct min/max floats; empty
    table raises RuntimeError loud

Tests use real on-disk SQLite via tmp_path for the open_fixture path
(so the RO URI mode is exercised end-to-end) and :memory: connections
for the pure-helper tests (faster, clean isolation).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, '.')

from data.polygon_news import _polygon_id_to_int
from data.replay.historical_news import HistoricalNewsItem
from data.replay.historical_sentiment import (
    DEFAULT_LATEST_MAX_AGE_SECONDS,
    LOOKUP_CHUNK_SIZE,
    HistoricalSentimentRow,
    _news_id_for_polygon,
    coverage_window,
    latest_sentiment,
    lookup_article_sentiments,
    open_fixture,
)


ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Build the sentiment table schema (mirrors data/news_pipeline.py)."""
    conn.execute("""
        CREATE TABLE sentiment (
            news_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            sentiment INTEGER NOT NULL,
            reasoning TEXT,
            headline TEXT,
            scored_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX idx_sentiment_ticker_time "
        "ON sentiment(ticker, scored_at DESC)"
    )


def _build_fixture(
    path: Path, rows: list[tuple[int, str, int, str, str, float]]
) -> None:
    """Write a sentiment fixture file at path with the given rows.

    Row tuple shape: (news_id, ticker, sentiment, reasoning, headline, scored_at).
    """
    conn = sqlite3.connect(path)
    try:
        _create_schema(conn)
        conn.executemany(
            "INSERT INTO sentiment "
            "(news_id, ticker, sentiment, reasoning, headline, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _memory_fixture(
    rows: list[tuple[int, str, int, str, str, float]],
) -> sqlite3.Connection:
    """Build an in-memory sentiment fixture for pure-helper tests."""
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    conn.executemany(
        "INSERT INTO sentiment "
        "(news_id, ticker, sentiment, reasoning, headline, scored_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def _item(
    *,
    article_id: str | None = "uuid-a",
    ticker: str = "AAPL",
    ts: datetime | None = None,
) -> HistoricalNewsItem:
    """Build a HistoricalNewsItem for lookup tests."""
    if ts is None:
        ts = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    return HistoricalNewsItem(
        ts_et=ts,
        ticker=ticker,
        headline="h",
        source="polygon",
        polygon_article_id=article_id,
        article_url=None,
    )


# ===========================================================================
# open_fixture
# ===========================================================================


def test_open_fixture_missing_raises_filenotfound(tmp_path):
    missing = tmp_path / "does-not-exist.sqlite"
    with pytest.raises(FileNotFoundError) as exc_info:
        open_fixture(missing)
    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "Re-export" in msg
    assert "data/replay/fixtures/README.md" in msg
    assert "Rule 26" in msg


def test_open_fixture_corrupt_raises_database_error(tmp_path):
    bad = tmp_path / "not-a-sqlite.sqlite"
    bad.write_bytes(b"this is plain text, not a SQLite database header")
    with pytest.raises(sqlite3.DatabaseError):
        open_fixture(bad)


def test_open_fixture_valid_returns_connection(tmp_path):
    fx = tmp_path / "ok.sqlite"
    _build_fixture(fx, [(1, "AAPL", 5, "r", "h", 1700000000.0)])
    conn = open_fixture(fx)
    try:
        row = conn.execute(
            "SELECT ticker, sentiment FROM sentiment WHERE news_id = 1"
        ).fetchone()
        assert row == ("AAPL", 5)
    finally:
        conn.close()


def test_open_fixture_is_read_only(tmp_path):
    """Write attempts on the RO connection must raise OperationalError."""
    fx = tmp_path / "ro.sqlite"
    _build_fixture(fx, [(1, "AAPL", 5, "r", "h", 1700000000.0)])
    conn = open_fixture(fx)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute(
                "INSERT INTO sentiment "
                "(news_id, ticker, sentiment, scored_at) VALUES (?, ?, ?, ?)",
                (2, "NVDA", 3, 1700000001.0),
            )
    finally:
        conn.close()


# ===========================================================================
# _news_id_for_polygon
# ===========================================================================


def test_news_id_for_polygon_matches_polygon_id_to_int():
    """Single source of truth: the replay loader's hash MUST equal the
    live ingestion hash. Cross-check for a handful of distinct inputs."""
    for article_id, ticker in [
        ("abc-123", "AAPL"),
        ("xyz", "NVDA"),
        ("uuid-with-many-chars-1234567890", "TSLA"),
        ("", "AAPL"),  # empty article_id still hashable (defensive)
    ]:
        assert _news_id_for_polygon(article_id, ticker) == _polygon_id_to_int(
            article_id, ticker
        )


def test_news_id_for_polygon_differs_by_ticker():
    """Same article_id + different ticker => different news_id."""
    a = _news_id_for_polygon("uuid-1", "AAPL")
    b = _news_id_for_polygon("uuid-1", "NVDA")
    assert a != b


def test_news_id_for_polygon_is_negative():
    """Polygon hashes always land in the negative-int space so they
    cannot collide with Alpaca's positive news_ids."""
    n = _news_id_for_polygon("uuid-x", "AAPL")
    assert n < 0


# ===========================================================================
# lookup_article_sentiments
# ===========================================================================


def test_lookup_article_sentiments_empty_input_returns_empty_dict():
    conn = _memory_fixture([])
    out = lookup_article_sentiments(conn, [])
    assert out == {}


def test_lookup_article_sentiments_single_hit():
    article_id = "uuid-aapl-1"
    ticker = "AAPL"
    news_id = _news_id_for_polygon(article_id, ticker)
    conn = _memory_fixture([(news_id, ticker, 7, "r", "h", 1700000000.0)])
    out = lookup_article_sentiments(
        conn, [_item(article_id=article_id, ticker=ticker)]
    )
    assert out == {(article_id, ticker): 7.0}
    # Confirm boundary int -> float conversion preserves the contract.
    assert isinstance(out[(article_id, ticker)], float)


def test_lookup_article_sentiments_multi_hit():
    rows = []
    items = []
    for i, (article_id, ticker, sentiment) in enumerate([
        ("u1", "AAPL", 5),
        ("u2", "NVDA", -3),
        ("u3", "TSLA", 0),
    ]):
        news_id = _news_id_for_polygon(article_id, ticker)
        rows.append((news_id, ticker, sentiment, "r", "h", 1700000000.0 + i))
        items.append(_item(article_id=article_id, ticker=ticker))
    conn = _memory_fixture(rows)
    out = lookup_article_sentiments(conn, items)
    assert out == {
        ("u1", "AAPL"): 5.0,
        ("u2", "NVDA"): -3.0,
        ("u3", "TSLA"): 0.0,
    }


def test_lookup_article_sentiments_skips_items_with_no_article_id():
    """Items with polygon_article_id=None cannot be hashed; they are
    silently dropped from the lookup (consumer surfaces the miss)."""
    article_id = "u1"
    ticker = "AAPL"
    news_id = _news_id_for_polygon(article_id, ticker)
    conn = _memory_fixture([(news_id, ticker, 5, "r", "h", 1700000000.0)])
    items = [
        _item(article_id=article_id, ticker=ticker),
        _item(article_id=None, ticker="NVDA"),  # skipped
    ]
    out = lookup_article_sentiments(conn, items)
    assert out == {("u1", "AAPL"): 5.0}


def test_lookup_article_sentiments_missing_items_absent_from_dict():
    """Items present in input but missing from the fixture do NOT appear
    in the returned dict. (items_to_context_dicts surfaces these as
    WARNING per Rule 18.)"""
    article_id = "u1"
    ticker = "AAPL"
    news_id = _news_id_for_polygon(article_id, ticker)
    conn = _memory_fixture([(news_id, ticker, 5, "r", "h", 1700000000.0)])
    items = [
        _item(article_id=article_id, ticker=ticker),
        _item(article_id="u-missing", ticker="NVDA"),
    ]
    out = lookup_article_sentiments(conn, items)
    assert ("u1", "AAPL") in out
    assert ("u-missing", "NVDA") not in out


def test_lookup_article_sentiments_chunking_above_limit():
    """1500 items exceed LOOKUP_CHUNK_SIZE (900); chunking must return
    every match without truncation."""
    rows = []
    items = []
    n = LOOKUP_CHUNK_SIZE + 600  # 1500
    for i in range(n):
        article_id = f"u-{i:05d}"
        ticker = "AAPL"
        news_id = _news_id_for_polygon(article_id, ticker)
        rows.append((news_id, ticker, i % 21 - 10, "r", "h", 1700000000.0 + i))
        items.append(_item(article_id=article_id, ticker=ticker))
    conn = _memory_fixture(rows)
    out = lookup_article_sentiments(conn, items)
    assert len(out) == n
    # Spot-check that the int->float conversion is exact for a few entries.
    assert out[("u-00000", "AAPL")] == float(0 % 21 - 10)
    assert out[(f"u-{n - 1:05d}", "AAPL")] == float((n - 1) % 21 - 10)


def test_lookup_article_sentiments_ticker_mismatch_skipped_with_warning(caplog):
    """If a fixture news_id maps to a different ticker than what the
    item carries (vanishingly unlikely 63-bit hash collision, or
    fixture corruption), skip the row with a WARNING. Better to
    surface the miss than to feed the LLM a wrong-ticker score."""
    article_id = "u-collision"
    item_ticker = "AAPL"
    news_id = _news_id_for_polygon(article_id, item_ticker)
    # Insert a row with the same news_id but a DIFFERENT ticker.
    conn = _memory_fixture(
        [(news_id, "WRONG", 9, "r", "h", 1700000000.0)]
    )
    with caplog.at_level("WARNING", logger="data.replay.historical_sentiment"):
        out = lookup_article_sentiments(
            conn, [_item(article_id=article_id, ticker=item_ticker)]
        )
    assert out == {}
    assert any(
        "news_id=%d" % news_id in r.message or "ticker=" in r.message
        for r in caplog.records
    )


def test_lookup_article_sentiments_skips_items_with_no_ticker():
    """Defensive: items lacking a ticker (shouldn't happen via the real
    HistoricalNewsItem but the helper accepts any object) are skipped."""
    class _BadItem:
        polygon_article_id = "u1"
        ticker = ""

    conn = _memory_fixture([])
    out = lookup_article_sentiments(conn, [_BadItem()])
    assert out == {}


# ===========================================================================
# latest_sentiment
# ===========================================================================


def _ts(year: int, month: int, day: int, h: int, m: int) -> float:
    """Build a UNIX epoch second from an ET wall-clock spec."""
    return datetime(year, month, day, h, m, tzinfo=ET).timestamp()


def test_latest_sentiment_hit_within_window():
    """A single row in the recency window should be returned."""
    scored = _ts(2026, 4, 15, 10, 0)
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "AAPL", as_of, max_age_seconds=3600) == 7


def test_latest_sentiment_miss_when_row_too_old():
    """Row outside the recency window should not be returned."""
    scored = _ts(2026, 4, 15, 8, 0)  # 2.5h before as_of
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "AAPL", as_of, max_age_seconds=3600) is None


def test_latest_sentiment_miss_when_no_rows_for_ticker():
    conn = _memory_fixture([
        (101, "AAPL", 7, "r", "h", _ts(2026, 4, 15, 10, 0)),
    ])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "NVDA", as_of) is None


def test_latest_sentiment_excludes_future_scored_at():
    """Point-in-time correctness: a row with scored_at > as_of must NOT
    be returned, even if it's within max_age_seconds."""
    future_scored = _ts(2026, 4, 15, 11, 0)  # AFTER as_of
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", future_scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "AAPL", as_of, max_age_seconds=7200) is None


def test_latest_sentiment_returns_most_recent_when_multiple():
    """ORDER BY scored_at DESC LIMIT 1 must return the latest within
    the recency window, not the earliest."""
    rows = [
        (101, "AAPL", 3, "r", "h", _ts(2026, 4, 15, 9, 50)),
        (102, "AAPL", 8, "r", "h", _ts(2026, 4, 15, 10, 10)),
        (103, "AAPL", -2, "r", "h", _ts(2026, 4, 15, 9, 35)),
    ]
    conn = _memory_fixture(rows)
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "AAPL", as_of, max_age_seconds=3600) == 8


def test_latest_sentiment_default_max_age_matches_live():
    """The default max_age_seconds must equal the live function's
    default (3600s -- one hour). The M2.1 stub had 86400 which was a
    bug; this test guards the correction."""
    assert DEFAULT_LATEST_MAX_AGE_SECONDS == 3600
    scored = _ts(2026, 4, 15, 9, 0)  # 1.5h before as_of
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    # With the default 3600 the row is out of window.
    assert latest_sentiment(conn, "AAPL", as_of) is None


def test_latest_sentiment_custom_max_age_extends_window():
    scored = _ts(2026, 4, 15, 9, 0)
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    # 2 hours (7200s) brings the row inside the window.
    assert latest_sentiment(conn, "AAPL", as_of, max_age_seconds=7200) == 7


def test_latest_sentiment_uppercases_ticker():
    """The live function upper-cases the ticker; mirror it."""
    scored = _ts(2026, 4, 15, 10, 0)
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    assert latest_sentiment(conn, "aapl", as_of, max_age_seconds=3600) == 7


def test_latest_sentiment_rejects_naive_as_of():
    conn = _memory_fixture([])
    with pytest.raises(ValueError, match="tz-aware"):
        latest_sentiment(conn, "AAPL", datetime(2026, 4, 15, 10, 30))


def test_latest_sentiment_rejects_negative_max_age():
    conn = _memory_fixture([])
    as_of = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    with pytest.raises(ValueError, match="max_age_seconds"):
        latest_sentiment(conn, "AAPL", as_of, max_age_seconds=-1)


def test_latest_sentiment_zero_max_age_only_matches_exact_scored_at():
    """Boundary: max_age_seconds=0 means scored_at must equal as_of
    exactly (the SQL uses >= as_of - 0 and <= as_of)."""
    as_of_dt = datetime(2026, 4, 15, 10, 30, tzinfo=ET)
    scored = as_of_dt.timestamp()
    conn = _memory_fixture([(101, "AAPL", 7, "r", "h", scored)])
    assert latest_sentiment(conn, "AAPL", as_of_dt, max_age_seconds=0) == 7


# ===========================================================================
# coverage_window
# ===========================================================================


def test_coverage_window_populated_returns_min_and_max():
    rows = [
        (101, "AAPL", 3, "r", "h", 1700000000.0),
        (102, "NVDA", -2, "r", "h", 1700050000.0),
        (103, "TSLA", 5, "r", "h", 1700025000.0),
    ]
    conn = _memory_fixture(rows)
    lo, hi = coverage_window(conn)
    assert lo == 1700000000.0
    assert hi == 1700050000.0
    assert isinstance(lo, float)
    assert isinstance(hi, float)


def test_coverage_window_single_row_min_equals_max():
    conn = _memory_fixture([(101, "AAPL", 3, "r", "h", 1700000000.0)])
    lo, hi = coverage_window(conn)
    assert lo == hi == 1700000000.0


def test_coverage_window_empty_table_raises_runtime_error():
    conn = _memory_fixture([])
    with pytest.raises(RuntimeError, match="empty"):
        coverage_window(conn)


# ===========================================================================
# Dataclass round-trip (defensive sanity)
# ===========================================================================


def test_historical_sentiment_row_construction():
    row = HistoricalSentimentRow(
        news_id=101, ticker="AAPL", sentiment=7, scored_at=1700000000.0
    )
    assert row.news_id == 101
    assert row.ticker == "AAPL"
    assert row.sentiment == 7
    assert row.scored_at == 1700000000.0
