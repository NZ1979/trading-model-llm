"""Tests for data/replay/historical_news.py + the fetch_news_range helper
in data/polygon_news.py (M2.2 sub-task #6).

Covers:
  - ET -> UTC date-bound conversion (incl. DST boundary)
  - Polygon ISO timestamp parsing (Z suffix, offset, naive)
  - Per-ticker cache path sanitization
  - Coverage subset semantics
  - Polygon article -> HistoricalNewsItem mapping (happy + drop-row paths)
  - Cache row serialization round-trip
  - filter_visible_at (lag boundary, lookback boundary, empty, validation)
  - items_to_context_dicts (None lookup, hit, miss, null id, ordering)
  - fetch_news_range: single page, pagination, next_url apiKey handling,
    404 soft-fail, 200-empty, 4xx loud + scrubbed, 5xx retry-then-loud,
    429 retry-then-loud, ConnectError retry, pagination cap, validation
  - load_historical_news: cache miss -> fetch -> write; cache hit no
    network; coverage-not-superset re-fetches; per-ticker fan-out;
    per-ticker failure aborts the whole batch; ET semantics; empty
    tickers raises; missing API key raises; schema version mismatch
    re-fetches; malformed cache re-fetches; items sorted; cache window
    filter on read

httpx is monkeypatched via MockTransport (same idiom as
``tests/test_ticker_metadata.py`` and ``tests/test_polygon_fetch_aggs.py``).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
import pytest

sys.path.insert(0, '.')

from data import polygon_news
from data.polygon_news import (
    NEWS_PAGINATION_MAX_PAGES,
    fetch_news_range,
)
from data.replay import historical_news
from data.replay.historical_news import (
    CACHE_SCHEMA_VERSION,
    ET,
    UTC,
    HistoricalNewsItem,
    _article_to_item,
    _cache_path_for,
    _cache_row_to_item,
    _coverage_covers,
    _et_date_to_utc_bounds,
    _item_to_cache_row,
    _parse_utc_iso,
    filter_visible_at,
    items_to_context_dicts,
    load_historical_news,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _polygon_key(monkeypatch):
    """All tests assume the env var is present; missing-key path is
    covered explicitly in test_load_missing_polygon_key_raises."""
    monkeypatch.setenv("POLYGON_API_KEY", "TEST_KEY_xyz")


@pytest.fixture
def _silence_sleep(monkeypatch):
    """Make retry backoffs instantaneous for retry-path tests."""
    async def fake(_s):
        return None
    monkeypatch.setattr("data.polygon_news.asyncio.sleep", fake)


def _install_news_mock(
    monkeypatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Patch polygon_news.httpx.AsyncClient to use MockTransport.

    Returns the seen-requests list so tests can assert on URL / params.
    """
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        # Drop any caller-passed transport since MockTransport replaces it.
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(wrapped), **kwargs)

    monkeypatch.setattr("data.polygon_news.httpx.AsyncClient", factory)
    return seen


def _article(
    *,
    aid: str = "art-1",
    ticker: str = "AAPL",
    published_utc: str = "2026-04-15T13:30:00Z",
    title: str = "Apple beats earnings",
    article_url: str | None = "https://example.com/a1",
    extra: dict | None = None,
) -> dict:
    """Build a Polygon News article dict (one tickers's worth)."""
    out: dict = {
        "id": aid,
        "publisher": {"name": "Test Wire"},
        "title": title,
        "author": "tester",
        "published_utc": published_utc,
        "tickers": [ticker],
        "insights": [{"ticker": ticker, "sentiment": "positive"}],
    }
    if article_url is not None:
        out["article_url"] = article_url
    if extra:
        out.update(extra)
    return out


def _news_response(articles: list[dict], next_url: str | None = None) -> dict:
    """Build a Polygon News v2 endpoint response body."""
    out: dict = {"status": "OK", "results": articles}
    if next_url is not None:
        out["next_url"] = next_url
    return out


# ===========================================================================
# Date / timezone helpers
# ===========================================================================


def test_et_date_to_utc_bounds_basic_edt():
    """In May (EDT = UTC-04:00), ET midnight is UTC 04:00."""
    start_utc, end_utc = _et_date_to_utc_bounds(
        date(2026, 5, 14), date(2026, 5, 14)
    )
    assert start_utc == datetime(2026, 5, 14, 4, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 5, 15, 3, 59, 59, tzinfo=UTC)


def test_et_date_to_utc_bounds_basic_est():
    """In January (EST = UTC-05:00), ET midnight is UTC 05:00."""
    start_utc, end_utc = _et_date_to_utc_bounds(
        date(2026, 1, 14), date(2026, 1, 14)
    )
    assert start_utc == datetime(2026, 1, 14, 5, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 1, 15, 4, 59, 59, tzinfo=UTC)


def test_et_date_to_utc_bounds_dst_transition_window():
    """Window spanning the spring-forward day still produces correct UTC."""
    # 2026 spring-forward is 2026-03-08. Window covers two ET dates,
    # one EST and one EDT.
    start_utc, end_utc = _et_date_to_utc_bounds(
        date(2026, 3, 8), date(2026, 3, 8)
    )
    # Start in EST: ET 00:00 -> UTC 05:00.
    assert start_utc == datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)
    # End in EDT: ET 23:59:59 -> UTC 03:59:59 of the next day.
    assert end_utc == datetime(2026, 3, 9, 3, 59, 59, tzinfo=UTC)


def test_et_date_to_utc_bounds_rejects_inverted_range():
    with pytest.raises(ValueError, match="before start_date"):
        _et_date_to_utc_bounds(date(2026, 5, 15), date(2026, 5, 14))


def test_parse_utc_iso_z_suffix():
    dt = _parse_utc_iso("2026-04-15T13:30:00Z")
    assert dt == datetime(2026, 4, 15, 13, 30, 0, tzinfo=UTC)


def test_parse_utc_iso_offset_suffix():
    dt = _parse_utc_iso("2026-04-15T09:30:00-04:00")
    assert dt.astimezone(UTC) == datetime(2026, 4, 15, 13, 30, 0, tzinfo=UTC)


def test_parse_utc_iso_naive_assumes_utc():
    dt = _parse_utc_iso("2026-04-15T13:30:00")
    assert dt == datetime(2026, 4, 15, 13, 30, 0, tzinfo=UTC)


def test_parse_utc_iso_garbage_raises():
    with pytest.raises(ValueError):
        _parse_utc_iso("not-a-timestamp")


# ===========================================================================
# Per-ticker cache path
# ===========================================================================


def test_cache_path_for_simple_ticker(tmp_path):
    p = _cache_path_for(tmp_path, "AAPL")
    assert p == tmp_path / "AAPL.json"


def test_cache_path_for_sanitizes_dot_share_class(tmp_path):
    """BRK.A -> BRK_A.json so the filesystem path is portable."""
    p = _cache_path_for(tmp_path, "BRK.A")
    assert p == tmp_path / "BRK_A.json"


def test_cache_path_for_sanitizes_slashes(tmp_path):
    p = _cache_path_for(tmp_path, "FOO/BAR")
    assert p.name == "FOO_BAR.json"
    p2 = _cache_path_for(tmp_path, "FOO\\BAR")
    assert p2.name == "FOO_BAR.json"


# ===========================================================================
# Coverage check
# ===========================================================================


def _cov(start: str, end: str) -> dict:
    return {
        "coverage": {"start_utc": start, "end_utc": end},
    }


def test_coverage_covers_exact_match():
    start = datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 5, 1, 3, 59, 59, tzinfo=UTC)
    assert _coverage_covers(
        _cov("2026-04-01T04:00:00Z", "2026-05-01T03:59:59Z"), start, end
    )


def test_coverage_covers_superset():
    start = datetime(2026, 4, 10, 4, 0, tzinfo=UTC)
    end = datetime(2026, 4, 20, 4, 0, tzinfo=UTC)
    assert _coverage_covers(
        _cov("2026-04-01T04:00:00Z", "2026-05-01T03:59:59Z"), start, end
    )


def test_coverage_covers_subset_returns_false():
    """Cached window is smaller than the requested window -> miss."""
    start = datetime(2026, 3, 15, 4, 0, tzinfo=UTC)
    end = datetime(2026, 5, 15, 4, 0, tzinfo=UTC)
    assert not _coverage_covers(
        _cov("2026-04-01T04:00:00Z", "2026-05-01T03:59:59Z"), start, end
    )


def test_coverage_covers_overlap_left_returns_false():
    start = datetime(2026, 3, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 4, 15, 4, 0, tzinfo=UTC)
    assert not _coverage_covers(
        _cov("2026-04-01T04:00:00Z", "2026-05-01T03:59:59Z"), start, end
    )


def test_coverage_covers_missing_coverage_returns_false():
    start = datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 5, 1, 4, 0, tzinfo=UTC)
    assert not _coverage_covers({}, start, end)
    assert not _coverage_covers({"coverage": {}}, start, end)


def test_coverage_covers_malformed_returns_false():
    start = datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
    end = datetime(2026, 5, 1, 4, 0, tzinfo=UTC)
    assert not _coverage_covers(
        _cov("not-iso", "not-iso"), start, end
    )


# ===========================================================================
# Article -> HistoricalNewsItem
# ===========================================================================


def test_article_to_item_happy_path():
    art = _article(
        aid="abc-123",
        ticker="AAPL",
        published_utc="2026-04-15T13:30:00Z",
        title="Apple beats",
        article_url="https://example.com/x",
    )
    item = _article_to_item(art, "AAPL")
    assert item is not None
    assert item.ticker == "AAPL"
    assert item.headline == "Apple beats"
    assert item.source == "polygon"
    assert item.polygon_article_id == "abc-123"
    assert item.article_url == "https://example.com/x"
    # ts_et should be tz-aware ET
    assert item.ts_et.tzinfo is ET
    assert item.ts_et.astimezone(UTC) == datetime(2026, 4, 15, 13, 30, tzinfo=UTC)


def test_article_to_item_truncates_long_headline():
    art = _article(title="x" * 1000)
    item = _article_to_item(art, "AAPL")
    assert item is not None
    assert len(item.headline) == 500


def test_article_to_item_missing_title_returns_none(caplog):
    art = _article(title="")
    with caplog.at_level("WARNING", logger="data.replay.historical_news"):
        item = _article_to_item(art, "AAPL")
    assert item is None
    assert any("missing published_utc or title" in r.message for r in caplog.records)


def test_article_to_item_missing_published_returns_none(caplog):
    art = _article(published_utc="")
    with caplog.at_level("WARNING", logger="data.replay.historical_news"):
        item = _article_to_item(art, "AAPL")
    assert item is None
    assert any("missing published_utc or title" in r.message for r in caplog.records)


def test_article_to_item_unparseable_timestamp_returns_none(caplog):
    art = _article(published_utc="not-iso")
    with caplog.at_level("WARNING", logger="data.replay.historical_news"):
        item = _article_to_item(art, "AAPL")
    assert item is None
    assert any("unparseable published_utc" in r.message for r in caplog.records)


def test_article_to_item_missing_id_is_kept_as_none():
    """An article with no id is still usable; sentiment lookup will miss."""
    art = _article(aid="")
    item = _article_to_item(art, "AAPL")
    assert item is not None
    assert item.polygon_article_id is None


def test_article_to_item_missing_url_is_none():
    art = _article(article_url=None)
    item = _article_to_item(art, "AAPL")
    assert item is not None
    assert item.article_url is None


# ===========================================================================
# Cache row round-trip
# ===========================================================================


def test_cache_row_round_trip():
    original = HistoricalNewsItem(
        ts_et=datetime(2026, 4, 15, 9, 30, tzinfo=ET),
        ticker="AAPL",
        headline="hello",
        source="polygon",
        polygon_article_id="abc",
        article_url="https://x",
    )
    row = _item_to_cache_row(original)
    restored = _cache_row_to_item(row)
    assert restored is not None
    assert restored.ticker == original.ticker
    assert restored.headline == original.headline
    assert restored.source == original.source
    assert restored.polygon_article_id == original.polygon_article_id
    assert restored.article_url == original.article_url
    assert restored.ts_et.astimezone(UTC) == original.ts_et.astimezone(UTC)


def test_cache_row_to_item_malformed_returns_none():
    assert _cache_row_to_item({}) is None
    assert _cache_row_to_item({"ticker": "X"}) is None
    assert _cache_row_to_item({"ts_utc": "bad", "ticker": "X", "headline": "h"}) is None


# ===========================================================================
# filter_visible_at
# ===========================================================================


def _it(ts_et: datetime, ticker: str = "AAPL") -> HistoricalNewsItem:
    return HistoricalNewsItem(
        ts_et=ts_et, ticker=ticker, headline="h", source="polygon",
        polygon_article_id="x", article_url=None,
    )


def test_filter_visible_at_exactly_at_lag_boundary_is_visible():
    """ts + lag == as_of is visible (boundary inclusive)."""
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    ts = as_of - timedelta(seconds=30)
    items = [_it(ts)]
    out = filter_visible_at(items, as_of_et=as_of, lookback_hours=2, lag_seconds=30)
    assert len(out) == 1


def test_filter_visible_at_one_second_before_lag_is_not_visible():
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    ts = as_of - timedelta(seconds=29)
    out = filter_visible_at([_it(ts)], as_of_et=as_of, lookback_hours=2, lag_seconds=30)
    assert out == []


def test_filter_visible_at_well_after_lag_is_visible():
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    ts = as_of - timedelta(minutes=10)
    out = filter_visible_at([_it(ts)], as_of_et=as_of, lookback_hours=2, lag_seconds=30)
    assert len(out) == 1


def test_filter_visible_at_outside_lookback_is_not_visible():
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    ts = as_of - timedelta(hours=3)
    out = filter_visible_at([_it(ts)], as_of_et=as_of, lookback_hours=2, lag_seconds=30)
    assert out == []


def test_filter_visible_at_exactly_at_lookback_boundary_is_visible():
    """ts == as_of - lookback is visible (boundary inclusive)."""
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    ts = as_of - timedelta(hours=2)
    out = filter_visible_at([_it(ts)], as_of_et=as_of, lookback_hours=2, lag_seconds=0)
    assert len(out) == 1


def test_filter_visible_at_empty_input_returns_empty():
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    assert filter_visible_at([], as_of_et=as_of, lookback_hours=2, lag_seconds=30) == []


def test_filter_visible_at_does_not_mutate_input():
    as_of = datetime(2026, 4, 15, 10, 0, 0, tzinfo=ET)
    items = [
        _it(as_of - timedelta(minutes=10)),
        _it(as_of - timedelta(hours=3)),  # outside lookback
    ]
    snapshot = list(items)
    filter_visible_at(items, as_of_et=as_of, lookback_hours=2, lag_seconds=30)
    assert items == snapshot


def test_filter_visible_at_rejects_naive_as_of():
    with pytest.raises(ValueError, match="tz-aware"):
        filter_visible_at([], as_of_et=datetime(2026, 4, 15, 10), lookback_hours=2, lag_seconds=30)


def test_filter_visible_at_rejects_negative_lookback():
    as_of = datetime(2026, 4, 15, 10, 0, tzinfo=ET)
    with pytest.raises(ValueError, match="lookback_hours"):
        filter_visible_at([], as_of_et=as_of, lookback_hours=-1, lag_seconds=30)


def test_filter_visible_at_rejects_negative_lag():
    as_of = datetime(2026, 4, 15, 10, 0, tzinfo=ET)
    with pytest.raises(ValueError, match="lag_seconds"):
        filter_visible_at([], as_of_et=as_of, lookback_hours=2, lag_seconds=-1)


# ===========================================================================
# items_to_context_dicts
# ===========================================================================


def test_items_to_context_dicts_none_lookup_all_zero():
    items = [
        _it(datetime(2026, 4, 15, 10, 0, tzinfo=ET)),
        _it(datetime(2026, 4, 15, 10, 5, tzinfo=ET)),
    ]
    out = items_to_context_dicts(items, sentiment_lookup=None)
    assert isinstance(out, tuple)
    assert len(out) == 2
    assert all(d["sentiment_score"] == 0.0 for d in out)
    assert all("source" in d and d["source"] == "polygon" for d in out)


def test_items_to_context_dicts_lookup_hit():
    item = HistoricalNewsItem(
        ts_et=datetime(2026, 4, 15, 10, 0, tzinfo=ET),
        ticker="AAPL", headline="h", source="polygon",
        polygon_article_id="abc", article_url=None,
    )
    lookup = {("abc", "AAPL"): 4.0}
    out = items_to_context_dicts([item], sentiment_lookup=lookup)
    assert out[0]["sentiment_score"] == 4.0


def test_items_to_context_dicts_lookup_miss_logs_warning(caplog):
    item = HistoricalNewsItem(
        ts_et=datetime(2026, 4, 15, 10, 0, tzinfo=ET),
        ticker="AAPL", headline="h", source="polygon",
        polygon_article_id="abc", article_url=None,
    )
    with caplog.at_level("WARNING", logger="data.replay.historical_news"):
        out = items_to_context_dicts([item], sentiment_lookup={})
    assert out[0]["sentiment_score"] == 0.0
    assert any("no sentiment row" in r.message for r in caplog.records)


def test_items_to_context_dicts_null_article_id_logs_warning(caplog):
    item = HistoricalNewsItem(
        ts_et=datetime(2026, 4, 15, 10, 0, tzinfo=ET),
        ticker="AAPL", headline="h", source="polygon",
        polygon_article_id=None, article_url=None,
    )
    with caplog.at_level("WARNING", logger="data.replay.historical_news"):
        out = items_to_context_dicts(
            [item], sentiment_lookup={("abc", "AAPL"): 1.0}
        )
    assert out[0]["sentiment_score"] == 0.0
    assert any("no polygon_article_id" in r.message for r in caplog.records)


def test_items_to_context_dicts_preserves_order():
    items = [
        _it(datetime(2026, 4, 15, 10, 0, tzinfo=ET), ticker="AAA"),
        _it(datetime(2026, 4, 15, 10, 5, tzinfo=ET), ticker="BBB"),
        _it(datetime(2026, 4, 15, 10, 10, tzinfo=ET), ticker="CCC"),
    ]
    out = items_to_context_dicts(items, sentiment_lookup=None)
    headlines = [d["headline"] for d in out]
    # all "h" but order is by index; assert via the ts string
    tickers_by_ts = [d["ts"] for d in out]
    assert tickers_by_ts == [
        "2026-04-15T10:00:00-04:00",
        "2026-04-15T10:05:00-04:00",
        "2026-04-15T10:10:00-04:00",
    ]


def test_items_to_context_dicts_empty_input_returns_empty_tuple():
    out = items_to_context_dicts([], sentiment_lookup=None)
    assert out == ()


# ===========================================================================
# fetch_news_range
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_news_range_single_page_happy(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_news_response([_article(aid="a1")])
        )
    seen = _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, 4, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, 3, tzinfo=UTC),
        api_key="k1",
    )
    assert len(out) == 1
    assert out[0]["id"] == "a1"
    assert len(seen) == 1
    qs = dict(seen[0].url.params.multi_items())
    assert qs["ticker"] == "AAPL"
    assert qs["published_utc.gte"] == "2026-04-01T04:00:00Z"
    assert qs["published_utc.lte"] == "2026-05-01T03:00:00Z"
    assert qs["apiKey"] == "k1"


@pytest.mark.asyncio
async def test_fetch_news_range_multi_page_follows_next_url(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_news_response(
                [_article(aid="a1")],
                next_url="https://api.polygon.io/v2/reference/news?cursor=p2&apiKey=k1",
            ))
        if calls["n"] == 2:
            return httpx.Response(200, json=_news_response(
                [_article(aid="a2"), _article(aid="a3")],
                next_url=None,
            ))
        raise AssertionError("unexpected extra page")

    _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
    )
    assert [a["id"] for a in out] == ["a1", "a2", "a3"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fetch_news_range_next_url_missing_apikey_is_reappended(monkeypatch):
    seen_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        if len(seen_urls) == 1:
            return httpx.Response(200, json=_news_response(
                [_article(aid="a1")],
                next_url="https://api.polygon.io/v2/reference/news?cursor=p2",
            ))
        return httpx.Response(200, json=_news_response([_article(aid="a2")]))

    _install_news_mock(monkeypatch, handler)
    await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
    )
    # The second request URL must contain apiKey=k1, re-appended by us.
    assert "apiKey=k1" in seen_urls[1]


@pytest.mark.asyncio
async def test_fetch_news_range_404_returns_empty(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "ERROR"})
    _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="UNKNOWN",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
    )
    assert out == []


@pytest.mark.asyncio
async def test_fetch_news_range_200_empty_results_returns_empty(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_news_response([]))
    _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
    )
    assert out == []


@pytest.mark.asyncio
async def test_fetch_news_range_4xx_other_raises_scrubbed(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            text='{"error": "bad apiKey=LIVE_KEY_DO_NOT_LEAK"}',
        )
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc_info:
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="LIVE_KEY_DO_NOT_LEAK",
        )
    msg = str(exc_info.value)
    assert "403" in msg
    # Critical: the live key must NOT appear anywhere in the raised message.
    assert "LIVE_KEY_DO_NOT_LEAK" not in msg
    assert "apiKey=<redacted>" in msg
    # And the original httpx chain must be suppressed (Rule 22).
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_fetch_news_range_5xx_persist_raises(monkeypatch, _silence_sleep):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream")
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="failed after"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
            max_retries=3,
        )


@pytest.mark.asyncio
async def test_fetch_news_range_5xx_then_200_returns(monkeypatch, _silence_sleep):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=_news_response([_article(aid="a1")]))

    _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
        max_retries=3,
    )
    assert len(out) == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fetch_news_range_429_persist_raises(monkeypatch, _silence_sleep):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="failed after"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
            max_retries=3,
        )


@pytest.mark.asyncio
async def test_fetch_news_range_429_then_200_returns(monkeypatch, _silence_sleep):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_news_response([]))

    _install_news_mock(monkeypatch, handler)
    out = await fetch_news_range(
        ticker="AAPL",
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 5, 1, tzinfo=UTC),
        api_key="k1",
        max_retries=3,
    )
    assert out == []
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_fetch_news_range_naive_datetime_raises():
    with pytest.raises(ValueError, match="tz-aware"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
        )
    with pytest.raises(ValueError, match="tz-aware"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1),
            api_key="k1",
        )


@pytest.mark.asyncio
async def test_fetch_news_range_inverted_range_raises():
    with pytest.raises(ValueError, match="before"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 5, 1, tzinfo=UTC),
            end_utc=datetime(2026, 4, 1, tzinfo=UTC),
            api_key="k1",
        )


@pytest.mark.asyncio
async def test_fetch_news_range_empty_api_key_raises():
    with pytest.raises(RuntimeError, match="api_key"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="",
        )


@pytest.mark.asyncio
async def test_fetch_news_range_pagination_cap_raises(monkeypatch):
    """Polygon mis-paginates; we abort rather than scan forever."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_news_response(
            [_article()],
            next_url="https://api.polygon.io/v2/reference/news?cursor=infinite&apiKey=k1",
        ))
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="pagination exceeded"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
            max_pages=3,
        )


@pytest.mark.asyncio
async def test_fetch_news_range_non_dict_payload_raises(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="non-object payload"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
        )


@pytest.mark.asyncio
async def test_fetch_news_range_non_list_results_raises(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "results": {"not": "list"}})
    _install_news_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="non-list results"):
        await fetch_news_range(
            ticker="AAPL",
            start_utc=datetime(2026, 4, 1, tzinfo=UTC),
            end_utc=datetime(2026, 5, 1, tzinfo=UTC),
            api_key="k1",
        )


# ===========================================================================
# load_historical_news
# ===========================================================================


def _patch_loader(monkeypatch, behavior: Callable):
    """Replace fetch_news_range (imported into historical_news) with a fake.

    behavior(ticker, start_utc, end_utc, *, api_key) returns either a list
    of article dicts or an Exception to raise.
    """
    captured: list[tuple] = []

    async def fake(ticker, start_utc, end_utc, *, api_key, **kw):
        captured.append((ticker, start_utc, end_utc, api_key))
        out = behavior(ticker, start_utc, end_utc, api_key=api_key)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(
        "data.replay.historical_news.fetch_news_range", fake
    )
    return captured


@pytest.mark.asyncio
async def test_load_historical_news_cache_miss_fetches_and_writes(monkeypatch, tmp_path):
    def behavior(ticker, *a, **kw):
        return [
            _article(
                aid=f"{ticker}-1",
                ticker=ticker,
                published_utc="2026-04-15T13:30:00Z",
                title=f"{ticker} news",
            ),
        ]
    captured = _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL",),
        date(2026, 4, 1),
        date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert "AAPL" in out
    assert len(out["AAPL"]) == 1
    assert out["AAPL"][0].polygon_article_id == "AAPL-1"
    # Cache file written.
    assert (tmp_path / "AAPL.json").exists()
    cached = json.loads((tmp_path / "AAPL.json").read_text())
    assert cached["schema_version"] == CACHE_SCHEMA_VERSION
    assert cached["ticker"] == "AAPL"
    assert len(cached["items"]) == 1
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_load_historical_news_cache_hit_no_network(monkeypatch, tmp_path):
    # Prime the cache with an explicit superset coverage.
    cache_file = tmp_path / "AAPL.json"
    cache_file.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "ticker": "AAPL",
        "coverage": {
            "start_utc": "2026-04-01T04:00:00Z",
            "end_utc": "2026-05-01T03:59:59Z",
        },
        "fetched_at": "2026-04-30T00:00:00Z",
        "items": [
            {
                "ts_utc": "2026-04-15T13:30:00Z",
                "ticker": "AAPL",
                "headline": "cached news",
                "source": "polygon",
                "polygon_article_id": "cached-1",
                "article_url": None,
            },
        ],
    }))

    def behavior(*a, **kw):
        raise AssertionError("cache hit should not hit network")
    _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL",),
        date(2026, 4, 1),
        date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert len(out["AAPL"]) == 1
    assert out["AAPL"][0].headline == "cached news"


@pytest.mark.asyncio
async def test_load_historical_news_cache_window_filter_on_read(monkeypatch, tmp_path):
    """Cache file may include items outside the current requested window;
    the returned list is filtered to the requested window."""
    cache_file = tmp_path / "AAPL.json"
    cache_file.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "ticker": "AAPL",
        "coverage": {
            # Wide coverage: 3 months.
            "start_utc": "2026-01-01T05:00:00Z",
            "end_utc": "2026-06-01T03:59:59Z",
        },
        "fetched_at": "2026-06-01T00:00:00Z",
        "items": [
            {"ts_utc": "2026-02-15T13:30:00Z", "ticker": "AAPL",
             "headline": "feb", "source": "polygon",
             "polygon_article_id": "feb", "article_url": None},
            {"ts_utc": "2026-04-15T13:30:00Z", "ticker": "AAPL",
             "headline": "apr", "source": "polygon",
             "polygon_article_id": "apr", "article_url": None},
            {"ts_utc": "2026-05-15T13:30:00Z", "ticker": "AAPL",
             "headline": "may", "source": "polygon",
             "polygon_article_id": "may", "article_url": None},
        ],
    }))

    def behavior(*a, **kw):
        raise AssertionError("cache hit should not hit network")
    _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL",),
        date(2026, 4, 1),
        date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    headlines = [it.headline for it in out["AAPL"]]
    assert headlines == ["apr"]


@pytest.mark.asyncio
async def test_load_historical_news_coverage_not_superset_refetches(monkeypatch, tmp_path):
    """Cached window doesn't fully contain the requested window -> miss."""
    cache_file = tmp_path / "AAPL.json"
    cache_file.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION,
        "ticker": "AAPL",
        "coverage": {
            "start_utc": "2026-04-10T04:00:00Z",
            "end_utc": "2026-04-20T04:00:00Z",
        },
        "fetched_at": "2026-04-20T00:00:00Z",
        "items": [
            {"ts_utc": "2026-04-15T13:30:00Z", "ticker": "AAPL",
             "headline": "stale", "source": "polygon",
             "polygon_article_id": "stale", "article_url": None},
        ],
    }))

    fetched: list[str] = []

    def behavior(ticker, *a, **kw):
        fetched.append(ticker)
        return [_article(aid="fresh-1", ticker=ticker,
                         published_utc="2026-04-15T13:30:00Z",
                         title="fresh news")]
    _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL",),
        date(2026, 4, 1),  # outside the cached window's start
        date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert fetched == ["AAPL"]
    assert out["AAPL"][0].headline == "fresh news"
    # Cache file overwritten with fresh window.
    cached = json.loads(cache_file.read_text())
    assert cached["items"][0]["headline"] == "fresh news"


@pytest.mark.asyncio
async def test_load_historical_news_multi_ticker_fan_out(monkeypatch, tmp_path):
    def behavior(ticker, *a, **kw):
        return [_article(aid=f"{ticker}-x", ticker=ticker, title=f"{ticker} news")]
    captured = _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL", "NVDA", "TSLA"),
        date(2026, 4, 1),
        date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert set(out.keys()) == {"AAPL", "NVDA", "TSLA"}
    assert all(len(out[t]) == 1 for t in ("AAPL", "NVDA", "TSLA"))
    seen_tickers = sorted(c[0] for c in captured)
    assert seen_tickers == ["AAPL", "NVDA", "TSLA"]


@pytest.mark.asyncio
async def test_load_historical_news_empty_tickers_raises(tmp_path):
    with pytest.raises(ValueError, match="at least one ticker"):
        await load_historical_news(
            (), date(2026, 4, 1), date(2026, 4, 30), cache_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_load_historical_news_inverted_range_raises(tmp_path):
    with pytest.raises(ValueError, match="before start_date"):
        await load_historical_news(
            ("AAPL",),
            date(2026, 5, 1), date(2026, 4, 1),
            cache_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_load_historical_news_per_ticker_failure_aborts_batch(monkeypatch, tmp_path):
    """One ticker's RuntimeError must abort the whole gather, never silently
    drop that ticker's news from LLMContext."""
    def behavior(ticker, *a, **kw):
        if ticker == "NVDA":
            return RuntimeError("Polygon News HTTP 503 for apiKey=<redacted>")
        return [_article(aid=f"{ticker}-1", ticker=ticker)]
    _patch_loader(monkeypatch, behavior)

    with pytest.raises(RuntimeError, match="503"):
        await load_historical_news(
            ("AAPL", "NVDA", "TSLA"),
            date(2026, 4, 1), date(2026, 4, 30),
            cache_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_load_historical_news_missing_polygon_key_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY"):
        await load_historical_news(
            ("AAPL",), date(2026, 4, 1), date(2026, 4, 30),
            cache_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_load_historical_news_schema_version_mismatch_refetches(monkeypatch, tmp_path):
    cache_file = tmp_path / "AAPL.json"
    cache_file.write_text(json.dumps({
        "schema_version": CACHE_SCHEMA_VERSION + 99,  # future / unknown
        "ticker": "AAPL",
        "coverage": {
            "start_utc": "2026-04-01T04:00:00Z",
            "end_utc": "2026-05-01T03:59:59Z",
        },
        "items": [],
    }))

    fetched: list[str] = []

    def behavior(ticker, *a, **kw):
        fetched.append(ticker)
        return [_article(aid="fresh", ticker=ticker)]
    _patch_loader(monkeypatch, behavior)

    await load_historical_news(
        ("AAPL",), date(2026, 4, 1), date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert fetched == ["AAPL"]


@pytest.mark.asyncio
async def test_load_historical_news_malformed_cache_refetches(monkeypatch, tmp_path):
    cache_file = tmp_path / "AAPL.json"
    cache_file.write_text("{ this is : not valid json")
    fetched: list[str] = []

    def behavior(ticker, *a, **kw):
        fetched.append(ticker)
        return [_article(aid="fresh", ticker=ticker)]
    _patch_loader(monkeypatch, behavior)

    await load_historical_news(
        ("AAPL",), date(2026, 4, 1), date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    assert fetched == ["AAPL"]


@pytest.mark.asyncio
async def test_load_historical_news_items_sorted_by_ts(monkeypatch, tmp_path):
    def behavior(ticker, *a, **kw):
        # Return out-of-order articles.
        return [
            _article(aid="b", ticker=ticker, published_utc="2026-04-20T13:30:00Z", title="b"),
            _article(aid="a", ticker=ticker, published_utc="2026-04-10T13:30:00Z", title="a"),
            _article(aid="c", ticker=ticker, published_utc="2026-04-25T13:30:00Z", title="c"),
        ]
    _patch_loader(monkeypatch, behavior)

    out = await load_historical_news(
        ("AAPL",), date(2026, 4, 1), date(2026, 4, 30),
        cache_dir=tmp_path,
    )
    headlines = [it.headline for it in out["AAPL"]]
    assert headlines == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_load_historical_news_et_date_semantics(monkeypatch, tmp_path):
    """A date range expressed in ET converts to the right UTC bounds at
    the fetch_news_range boundary."""
    captured: list[tuple] = []

    def behavior(ticker, start_utc, end_utc, *, api_key, **kw):
        captured.append((start_utc, end_utc))
        return []
    _patch_loader(monkeypatch, behavior)

    # May 14 ET (EDT) -> May 14 04:00 UTC to May 15 03:59:59 UTC.
    await load_historical_news(
        ("AAPL",), date(2026, 5, 14), date(2026, 5, 14),
        cache_dir=tmp_path,
    )
    assert captured[0][0] == datetime(2026, 5, 14, 4, 0, 0, tzinfo=UTC)
    assert captured[0][1] == datetime(2026, 5, 15, 3, 59, 59, tzinfo=UTC)


@pytest.mark.asyncio
async def test_load_historical_news_concurrency_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="concurrency"):
        await load_historical_news(
            ("AAPL",), date(2026, 4, 1), date(2026, 4, 30),
            cache_dir=tmp_path, concurrency=0,
        )


@pytest.mark.asyncio
async def test_load_historical_news_concurrency_caps_in_flight(monkeypatch, tmp_path):
    """Bounded concurrency: with 6 tickers + concurrency=2, the max
    in-flight count never exceeds 2."""
    import asyncio as _asyncio

    in_flight = {"now": 0, "peak": 0}

    async def fake(ticker, start_utc, end_utc, *, api_key, **kw):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await _asyncio.sleep(0)  # yield so concurrency can race
        in_flight["now"] -= 1
        return [_article(aid=f"{ticker}-1", ticker=ticker)]

    monkeypatch.setattr(
        "data.replay.historical_news.fetch_news_range", fake
    )

    await load_historical_news(
        ("A", "B", "C", "D", "E", "F"),
        date(2026, 4, 1), date(2026, 4, 30),
        cache_dir=tmp_path, concurrency=2,
    )
    assert in_flight["peak"] <= 2
