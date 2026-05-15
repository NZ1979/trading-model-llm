"""Tests for data/replay/ticker_metadata.py (M2.2 sub-task #5).

Covers:
  - SIC → GICS sector mapping (representative ranges + boundary cases)
  - Market-cap bucketization at the 5 thresholds
  - Cache I/O (read missing, read malformed, atomic write)
  - get_ticker_metadata: cache miss → fetch + write → hit on second call
  - as_of invalidation: same ticker, different as_of → re-fetch ADV only
  - 7-day TTL on slow fields: stale fetched_at → re-fetch sector + bucket
  - 404 on Polygon Reference → soft-fail to Unknown/unknown, ADV unchanged
  - Empty Polygon daily bars → ADV=0, sector/bucket unchanged
  - Polygon Reference 5xx → loud RuntimeError after retries
  - warm_metadata_cache: idempotent, batch-writes, concurrency-limited

httpx is monkeypatched via MockTransport (same pattern as
``tests/test_polygon_fetch_aggs.py``). fetch_aggs is monkeypatched at
the import site so tests don't depend on the live Polygon Aggs path.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay import ticker_metadata
from data.replay.ticker_metadata import (
    SLOW_FIELD_TTL_DAYS,
    TickerMetadata,
    _is_slow_fields_fresh,
    _market_cap_to_bucket,
    _read_cache_file,
    _sic_to_sector,
    _write_cache_file,
    get_ticker_metadata,
    warm_metadata_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_reference_mock(
    monkeypatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Patch ticker_metadata's httpx.AsyncClient to use MockTransport.

    Returns the seen-requests list so tests can assert on URL / params.
    """
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        return real_async_client(transport=httpx.MockTransport(wrapped))

    monkeypatch.setattr("data.replay.ticker_metadata.httpx.AsyncClient", factory)
    return seen


def _patch_fetch_aggs(monkeypatch, behavior: Callable):
    """Replace fetch_aggs (imported into ticker_metadata) with an async fake.

    ``behavior(ticker, multiplier, timespan, start_date, end_date)`` returns
    a DataFrame or an Exception (which gets raised).
    """
    captured: list[tuple] = []

    async def fake(ticker, multiplier, timespan, start_date, end_date, **kw):
        captured.append((ticker, multiplier, timespan, start_date, end_date))
        out = behavior(ticker, multiplier, timespan, start_date, end_date)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr("data.replay.ticker_metadata.fetch_aggs", fake)
    return captured


def _silence_sleep(monkeypatch):
    """Make retry waits instantaneous for the retry path tests."""
    async def fake(_s):
        return None
    monkeypatch.setattr("data.replay.ticker_metadata.asyncio.sleep", fake)


def _daily_bars_df(volumes: list[int]) -> pd.DataFrame:
    """Build a daily-bars DataFrame matching polygon_feed._polygon_bars_to_df()."""
    n = len(volumes)
    idx = pd.date_range("2026-03-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": volumes,
            "vwap": [100.25] * n,
            "trade_count": [1] * n,
        },
        index=pd.Index(idx, name="ts"),
    )


def _ref_payload(*, sic_code: str | None, market_cap: float | None) -> dict:
    """Build a Polygon Reference Tickers v3 response body."""
    results = {
        "ticker": "TEST",
        "name": "Test Corp",
        "market": "stocks",
        "active": True,
    }
    if sic_code is not None:
        results["sic_code"] = sic_code
    if market_cap is not None:
        results["market_cap"] = market_cap
    return {"status": "OK", "results": results}


@pytest.fixture(autouse=True)
def _polygon_key(monkeypatch):
    """All tests assume the env var is present; the missing-key path is
    covered by tests/test_polygon_fetch_aggs.py."""
    monkeypatch.setenv("POLYGON_API_KEY", "TEST_KEY_xyz")


# ---------------------------------------------------------------------------
# Sector mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sic, expected",
    [
        ("2834", "Health Care"),                  # Pharma (Pfizer)
        ("3571", "Information Technology"),       # Computer equipment (Apple)
        ("3674", "Information Technology"),       # Semiconductors (NVDA)
        ("7372", "Information Technology"),       # Computer services
        ("2911", "Energy"),                       # Petroleum (XOM)
        ("4911", "Utilities"),                    # Electric utility
        ("6021", "Financials"),                   # National banks (JPM)
        ("6500", "Real Estate"),                  # REIT range
        ("5812", "Consumer Discretionary"),       # Restaurants
        ("2086", "Consumer Staples"),             # Beverages
        ("4813", "Communication Services"),       # Telecom
        ("3711", "Consumer Discretionary"),       # Motor vehicles
    ],
)
def test_sic_to_sector_known_ranges(sic, expected):
    assert _sic_to_sector(sic) == expected


@pytest.mark.parametrize(
    "value",
    [None, "", "abc", "99999"],  # last is out of mapped ranges
)
def test_sic_to_sector_unknown_inputs(value):
    assert _sic_to_sector(value) == "Unknown"


def test_sic_to_sector_accepts_int():
    """Polygon returns string, but the helper accepts int defensively."""
    assert _sic_to_sector(3571) == "Information Technology"


# ---------------------------------------------------------------------------
# Market-cap bucketization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cap, bucket",
    [
        # Below each boundary should land in the lower bucket; AT/above
        # the boundary lands in the higher bucket.
        (3_000_000_000_000, "mega"),      # AAPL-class, well above $200B
        (200_000_000_000, "mega"),        # exactly at the mega boundary
        (199_999_999_999, "large"),       # one dollar below mega
        (50_000_000_000, "large"),        # mid-large
        (10_000_000_000, "large"),        # exactly at the large boundary
        (9_999_999_999, "mid"),           # one dollar below large
        (5_000_000_000, "mid"),           # mid-mid
        (2_000_000_000, "mid"),           # exactly at the mid boundary
        (1_999_999_999, "small"),         # one dollar below mid
        (500_000_000, "small"),           # mid-small
        (300_000_000, "small"),           # exactly at the small boundary
        (299_999_999, "micro"),           # one dollar below small
        (50_000_000, "micro"),            # micro
        (1, "micro"),                     # positive but tiny
    ],
)
def test_market_cap_to_bucket_thresholds(cap, bucket):
    assert _market_cap_to_bucket(cap) == bucket


@pytest.mark.parametrize("value", [None, 0, -1, "not-a-number"])
def test_market_cap_to_bucket_unknown_inputs(value):
    assert _market_cap_to_bucket(value) == "unknown"


# ---------------------------------------------------------------------------
# Cache file I/O
# ---------------------------------------------------------------------------


def test_read_cache_file_missing_returns_empty(tmp_path):
    assert _read_cache_file(tmp_path / "nope.json") == {}


def test_read_cache_file_malformed_returns_empty(tmp_path, caplog):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING, logger="data.replay.ticker_metadata"):
        result = _read_cache_file(p)
    assert result == {}
    assert any("read failed" in r.message for r in caplog.records)


def test_read_cache_file_non_dict_root_returns_empty(tmp_path, caplog):
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING, logger="data.replay.ticker_metadata"):
        result = _read_cache_file(p)
    assert result == {}
    assert any("not a dict" in r.message for r in caplog.records)


def test_write_cache_file_creates_parent(tmp_path):
    target = tmp_path / "fresh_subdir" / "cache.json"
    _write_cache_file(target, {"AAPL": {"sector": "X"}})
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"AAPL": {"sector": "X"}}


def test_write_cache_file_atomic_no_leftover_tmp(tmp_path):
    target = tmp_path / "cache.json"
    _write_cache_file(target, {"AAPL": {}})
    assert target.exists()
    assert not target.with_suffix(target.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------


def test_is_slow_fields_fresh_within_ttl():
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = (now - timedelta(days=SLOW_FIELD_TTL_DAYS - 1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _is_slow_fields_fresh({"fetched_at": fetched}, now=now) is True


def test_is_slow_fields_fresh_at_exact_boundary():
    """Exactly 7 days old → still fresh (inclusive bound)."""
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = (now - timedelta(days=SLOW_FIELD_TTL_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert _is_slow_fields_fresh({"fetched_at": fetched}, now=now) is True


def test_is_slow_fields_fresh_one_second_stale():
    """One second over the TTL → stale."""
    now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    fetched = (
        now - timedelta(days=SLOW_FIELD_TTL_DAYS, seconds=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _is_slow_fields_fresh({"fetched_at": fetched}, now=now) is False


def test_is_slow_fields_fresh_missing_or_bad_input():
    assert _is_slow_fields_fresh({}) is False
    assert _is_slow_fields_fresh({"fetched_at": "garbage"}) is False
    assert _is_slow_fields_fresh({"fetched_at": None}) is False


# ---------------------------------------------------------------------------
# get_ticker_metadata — happy path + caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticker_metadata_cache_miss_fetches_and_writes(
    tmp_path, monkeypatch,
):
    """First call: hits Polygon Reference + fetch_aggs; writes a cache row."""
    cache = tmp_path / "tm.json"
    ref_calls = _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    aggs_calls = _patch_fetch_aggs(
        monkeypatch,
        lambda *a: _daily_bars_df([100_000] * 32),
    )

    md = await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)

    assert md == TickerMetadata(
        ticker="AAPL",
        sector="Information Technology",
        market_cap_bucket="mega",
        avg_daily_volume=100_000,
    )
    # One Polygon Reference call, one fetch_aggs call.
    assert len(ref_calls) == 1
    assert "/v3/reference/tickers/AAPL" in str(ref_calls[0].url)
    assert "apiKey=TEST_KEY_xyz" in str(ref_calls[0].url)
    assert len(aggs_calls) == 1

    # Cache row written.
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert "AAPL" in written
    assert written["AAPL"]["sector"] == "Information Technology"
    assert written["AAPL"]["market_cap_bucket"] == "mega"
    assert written["AAPL"]["avg_daily_volume"] == 100_000
    assert written["AAPL"]["as_of"] == "2026-04-01"


@pytest.mark.asyncio
async def test_get_ticker_metadata_cache_hit_skips_network(
    tmp_path, monkeypatch,
):
    """Second call with same ticker + as_of: no Polygon traffic, no fetch_aggs."""
    cache = tmp_path / "tm.json"

    # Seed a fresh row.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache.write_text(json.dumps({
        "AAPL": {
            "sector": "Information Technology",
            "market_cap_bucket": "mega",
            "avg_daily_volume": 50_000_000,
            "as_of": "2026-04-01",
            "fetched_at": now_iso,
        }
    }), encoding="utf-8")

    ref_calls = _install_reference_mock(
        monkeypatch,
        lambda req: pytest.fail("Polygon Reference should not be called"),
    )
    aggs_calls = _patch_fetch_aggs(
        monkeypatch,
        lambda *a: pytest.fail("fetch_aggs should not be called"),
    )

    md = await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)

    assert md == TickerMetadata(
        ticker="AAPL",
        sector="Information Technology",
        market_cap_bucket="mega",
        avg_daily_volume=50_000_000,
    )
    assert ref_calls == []
    assert aggs_calls == []


@pytest.mark.asyncio
async def test_get_ticker_metadata_as_of_change_refetches_only_adv(
    tmp_path, monkeypatch,
):
    """Same ticker, different as_of, slow fields still fresh:
    re-fetch ADV but skip Polygon Reference."""
    cache = tmp_path / "tm.json"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache.write_text(json.dumps({
        "NVDA": {
            "sector": "Information Technology",
            "market_cap_bucket": "mega",
            "avg_daily_volume": 25_000_000,
            "as_of": "2026-04-01",
            "fetched_at": now_iso,
        }
    }), encoding="utf-8")

    ref_calls = _install_reference_mock(
        monkeypatch,
        lambda req: pytest.fail("Polygon Reference must NOT be re-called"),
    )
    aggs_calls = _patch_fetch_aggs(
        monkeypatch,
        lambda *a: _daily_bars_df([40_000_000] * 32),
    )

    md = await get_ticker_metadata(
        "NVDA", date(2026, 5, 1), cache_path=cache,
    )

    assert md.avg_daily_volume == 40_000_000
    assert md.sector == "Information Technology"  # reused from cache
    assert md.market_cap_bucket == "mega"          # reused from cache
    assert ref_calls == []                          # NOT re-fetched
    assert len(aggs_calls) == 1                     # ADV re-fetched

    # Cache row updated with new as_of but same fetched_at.
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["NVDA"]["as_of"] == "2026-05-01"
    assert written["NVDA"]["fetched_at"] == now_iso


@pytest.mark.asyncio
async def test_get_ticker_metadata_stale_slow_fields_refetches_reference(
    tmp_path, monkeypatch,
):
    """fetched_at > 7 days ago → re-fetch sector + bucket (and ADV)."""
    cache = tmp_path / "tm.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cache.write_text(json.dumps({
        "AAPL": {
            "sector": "Stale Sector",
            "market_cap_bucket": "stale",
            "avg_daily_volume": 999,
            "as_of": "2026-04-01",
            "fetched_at": stale,
        }
    }), encoding="utf-8")

    ref_calls = _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    aggs_calls = _patch_fetch_aggs(
        monkeypatch,
        # As_of stayed the same → ADV cache hit; this lambda should NOT
        # be called.
        lambda *a: pytest.fail("ADV should not re-fetch when as_of matches"),
    )

    md = await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)

    assert md.sector == "Information Technology"  # re-fetched, not "Stale Sector"
    assert md.market_cap_bucket == "mega"
    assert md.avg_daily_volume == 999  # ADV reused since as_of matches
    assert len(ref_calls) == 1
    assert aggs_calls == []


# ---------------------------------------------------------------------------
# get_ticker_metadata — soft-fail paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticker_metadata_polygon_404_soft_fails_to_unknown(
    tmp_path, monkeypatch, caplog,
):
    """Polygon Reference 404 → Unknown/unknown sector+bucket, ADV still tried.

    Per Rule 18 option (2): visible degradation, not silent. WARNING
    log emitted; downstream policy.py handles 'unknown' cleanly.
    """
    import logging

    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(404, json={"status": "NOT_FOUND"}),
    )
    _patch_fetch_aggs(
        monkeypatch,
        lambda *a: _daily_bars_df([1_000] * 32),
    )

    with caplog.at_level(logging.WARNING, logger="data.replay.ticker_metadata"):
        md = await get_ticker_metadata(
            "ZZZZ", date(2026, 4, 1), cache_path=cache,
        )

    assert md == TickerMetadata(
        ticker="ZZZZ",
        sector="Unknown",
        market_cap_bucket="unknown",
        avg_daily_volume=1_000,
    )
    assert any(
        "Polygon Reference returned no row" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_get_ticker_metadata_empty_bars_soft_fails_adv_to_zero(
    tmp_path, monkeypatch, caplog,
):
    """fetch_aggs raises 'Polygon returned 0 bars' → ADV=0 with WARNING.

    Sector and bucket still populated from Polygon Reference (independent path).
    """
    import logging

    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    _patch_fetch_aggs(
        monkeypatch,
        lambda *a: RuntimeError(
            "Polygon returned 0 bars for HALT 1/day 2026-02-15..2026-03-31"
        ),
    )

    with caplog.at_level(logging.WARNING, logger="data.replay.ticker_metadata"):
        md = await get_ticker_metadata(
            "HALT", date(2026, 4, 1), cache_path=cache,
        )

    assert md == TickerMetadata(
        ticker="HALT",
        sector="Information Technology",
        market_cap_bucket="mega",
        avg_daily_volume=0,
    )
    assert any(
        "0 daily bars" in r.message and "ADV=0" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_get_ticker_metadata_other_runtime_error_propagates_loud(
    tmp_path, monkeypatch,
):
    """fetch_aggs RuntimeError that ISN'T the empty-results case must propagate.

    Rule 18: 5xx, retries exhausted, bad key, truncation are operational
    bugs — they should never become a silent ADV=0.
    """
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    _patch_fetch_aggs(
        monkeypatch,
        lambda *a: RuntimeError("Polygon fetch_aggs failed after 3 retries"),
    )

    with pytest.raises(RuntimeError, match="failed after 3 retries"):
        await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)


@pytest.mark.asyncio
async def test_get_ticker_metadata_reference_5xx_raises(
    tmp_path, monkeypatch,
):
    """Polygon Reference 5xx persisting after retries → loud RuntimeError."""
    cache = tmp_path / "tm.json"
    _silence_sleep(monkeypatch)
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(500, text="upstream is sad"),
    )
    _patch_fetch_aggs(
        monkeypatch,
        lambda *a: pytest.fail("ADV must not be fetched when reference fails"),
    )

    with pytest.raises(RuntimeError, match="Polygon Reference fetch failed"):
        await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)


@pytest.mark.asyncio
async def test_get_ticker_metadata_reference_4xx_raises_with_scrubbed_url(
    tmp_path, monkeypatch,
):
    """Polygon Reference 401/403 → loud RuntimeError; URL has apiKey scrubbed (Rule 22)."""
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(401, text="bad api key"),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)

    msg = str(exc_info.value)
    assert "401" in msg
    assert "TEST_KEY_xyz" not in msg
    assert "apiKey=<redacted>" in msg


@pytest.mark.asyncio
async def test_get_ticker_metadata_reference_5xx_retries_then_succeeds(
    tmp_path, monkeypatch,
):
    """First call 503, second call 200 → succeeds (exercises retry path)."""
    cache = tmp_path / "tm.json"
    _silence_sleep(monkeypatch)

    call_count = {"n": 0}

    def handler(req):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, text="try again")
        return httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        )

    _install_reference_mock(monkeypatch, handler)
    _patch_fetch_aggs(monkeypatch, lambda *a: _daily_bars_df([1_000] * 32))

    md = await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)
    assert md.sector == "Information Technology"
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# ADV computation specifics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adv_uses_trailing_30_trading_days(tmp_path, monkeypatch):
    """When fetch_aggs returns 32 bars, ADV uses the last 30."""
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )

    # 2 bars at vol=1, 30 bars at vol=100 → tail(30) is all 100s; mean=100.
    volumes = [1, 1] + [100] * 30
    _patch_fetch_aggs(monkeypatch, lambda *a: _daily_bars_df(volumes))

    md = await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)
    assert md.avg_daily_volume == 100


@pytest.mark.asyncio
async def test_adv_calls_fetch_aggs_with_correct_window(tmp_path, monkeypatch):
    """fetch_aggs called with [as_of - 45d, as_of - 1d] inclusive."""
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    aggs = _patch_fetch_aggs(monkeypatch, lambda *a: _daily_bars_df([5] * 32))

    await get_ticker_metadata("AAPL", date(2026, 4, 1), cache_path=cache)

    assert len(aggs) == 1
    (ticker, mult, timespan, start, end) = aggs[0]
    assert ticker == "AAPL"
    assert (mult, timespan) == (1, "day")
    assert start == date(2026, 4, 1) - timedelta(days=45)
    assert end == date(2026, 4, 1) - timedelta(days=1)


# ---------------------------------------------------------------------------
# warm_metadata_cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_metadata_cache_batch_writes_once(tmp_path, monkeypatch):
    """N tickers → one cache file write at the end with N rows."""
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        ),
    )
    _patch_fetch_aggs(monkeypatch, lambda *a: _daily_bars_df([1_000] * 32))

    await warm_metadata_cache(
        ("AAPL", "NVDA", "MSFT"),
        date(2026, 4, 1),
        cache_path=cache,
    )

    written = json.loads(cache.read_text(encoding="utf-8"))
    assert set(written.keys()) == {"AAPL", "NVDA", "MSFT"}
    for ticker in ("AAPL", "NVDA", "MSFT"):
        assert written[ticker]["sector"] == "Information Technology"
        assert written[ticker]["avg_daily_volume"] == 1_000


@pytest.mark.asyncio
async def test_warm_metadata_cache_skips_fresh_rows(tmp_path, monkeypatch):
    """Tickers whose slow fields AND as_of are fresh skip both fetches."""
    cache = tmp_path / "tm.json"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache.write_text(json.dumps({
        "AAPL": {
            "sector": "Information Technology",
            "market_cap_bucket": "mega",
            "avg_daily_volume": 50_000_000,
            "as_of": "2026-04-01",
            "fetched_at": now_iso,
        }
    }), encoding="utf-8")

    ref_calls = _install_reference_mock(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json=_ref_payload(sic_code="3674", market_cap=2_000_000_000_000),
        ),
    )
    aggs_calls = _patch_fetch_aggs(
        monkeypatch,
        lambda *a: _daily_bars_df([42] * 32),
    )

    await warm_metadata_cache(
        ("AAPL", "NVDA"), date(2026, 4, 1), cache_path=cache,
    )

    # AAPL is fresh → skipped; only NVDA hits the network.
    assert len(ref_calls) == 1
    assert "/v3/reference/tickers/NVDA" in str(ref_calls[0].url)
    assert len(aggs_calls) == 1
    assert aggs_calls[0][0] == "NVDA"

    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["AAPL"]["avg_daily_volume"] == 50_000_000  # untouched
    assert written["NVDA"]["avg_daily_volume"] == 42          # newly written


@pytest.mark.asyncio
async def test_warm_metadata_cache_empty_tickers_is_noop(tmp_path, monkeypatch):
    """Empty tuple → no I/O, no cache file created."""
    cache = tmp_path / "tm.json"
    _install_reference_mock(
        monkeypatch,
        lambda req: pytest.fail("no requests expected"),
    )
    _patch_fetch_aggs(
        monkeypatch,
        lambda *a: pytest.fail("no fetch_aggs expected"),
    )

    await warm_metadata_cache((), date(2026, 4, 1), cache_path=cache)
    assert not cache.exists()


@pytest.mark.asyncio
async def test_warm_metadata_cache_continues_past_per_ticker_failure(
    tmp_path, monkeypatch, caplog,
):
    """One ticker's Polygon failure must not abort the whole warmup."""
    import logging

    cache = tmp_path / "tm.json"
    _silence_sleep(monkeypatch)

    def handler(req):
        # NVDA → 5xx (will exhaust retries and raise inside fetch_one),
        # everything else → 200.
        if "/NVDA" in str(req.url):
            return httpx.Response(500, text="oops")
        return httpx.Response(
            200,
            json=_ref_payload(sic_code="3571", market_cap=3_000_000_000_000),
        )

    _install_reference_mock(monkeypatch, handler)
    _patch_fetch_aggs(monkeypatch, lambda *a: _daily_bars_df([10] * 32))

    with caplog.at_level(logging.WARNING, logger="data.replay.ticker_metadata"):
        await warm_metadata_cache(
            ("AAPL", "NVDA", "MSFT"),
            date(2026, 4, 1),
            cache_path=cache,
        )

    written = json.loads(cache.read_text(encoding="utf-8"))
    # AAPL and MSFT made it through; NVDA was skipped.
    assert set(written.keys()) == {"AAPL", "MSFT"}
    assert any("NVDA failed" in r.message for r in caplog.records)
