"""Tests for data/polygon_feed.fetch_aggs (M2.2 sub-task #2).

Covers the generic Polygon aggregates helper that backs the replay
harness's per-ticker bar loaders and SPY market-context loader.

Pattern mirrors tests/test_fred_vix.py:
  - httpx.AsyncClient is monkeypatched at the import site so the real
    httpx Response objects flow through MockTransport.
  - The real AsyncClient class is captured BEFORE the patch to avoid
    factory-recursion when the factory constructs the wrapped client.
  - asyncio.sleep is monkeypatched to a no-op for retry tests so the
    exponential-backoff path doesn't slow the suite.

The retry path uses POLYGON_AGGS_PAGE_LIMIT and _scrub_apikey from
polygon_feed; those are imported here only to assert correct cap
behavior and credential redaction in error messages.
"""
from __future__ import annotations

import sys
from datetime import date
from typing import Callable

import httpx
import pandas as pd
import pytest

sys.path.insert(0, '.')

from data import polygon_feed
from data.polygon_feed import (
    POLYGON_AGGS_PAGE_LIMIT,
    _polygon_bars_to_df,
    _require_polygon_key,
    _scrub_apikey,
    fetch_aggs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_mock(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]):
    """Patch polygon_feed's AsyncClient to use an in-process MockTransport.

    Returns a list of seen requests so tests can assert on the URLs /
    params after the call.
    """
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient  # capture BEFORE patch

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        return real_async_client(transport=httpx.MockTransport(wrapped_handler))

    monkeypatch.setattr("data.polygon_feed.httpx.AsyncClient", factory)
    return seen


def _silence_sleep(monkeypatch):
    """Replace asyncio.sleep in polygon_feed with a no-op for fast retry tests."""
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr("data.polygon_feed.asyncio.sleep", fake_sleep)


def _polygon_bar(t_ms: int, *, o=100.0, h=101.0, l=99.5, c=100.5,
                 v=10_000, vw=100.25, n=42) -> dict:
    """Build one Polygon aggs result row."""
    return {"t": t_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw, "n": n}


def _ok_payload(bars: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"status": "OK", "results": bars})


# ---------------------------------------------------------------------------
# _require_polygon_key
# ---------------------------------------------------------------------------


def test_require_polygon_key_present(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "MYKEY123")
    assert _require_polygon_key() == "MYKEY123"


def test_require_polygon_key_missing(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY not set"):
        _require_polygon_key()


def test_require_polygon_key_empty_string(monkeypatch):
    """Empty string env var is the same as missing per Rule 18."""
    monkeypatch.setenv("POLYGON_API_KEY", "")
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY not set"):
        _require_polygon_key()


# ---------------------------------------------------------------------------
# _polygon_bars_to_df
# ---------------------------------------------------------------------------


def test_polygon_bars_to_df_success():
    bars = [
        _polygon_bar(1_711_966_800_000, c=100.0),  # 2024-04-01 13:00 UTC
        _polygon_bar(1_711_967_100_000, c=101.0),  # 2024-04-01 13:05 UTC
        _polygon_bar(1_711_967_400_000, c=102.0),  # 2024-04-01 13:10 UTC
    ]
    df = _polygon_bars_to_df(bars)
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "vwap", "trade_count"
    ]
    assert len(df) == 3
    assert str(df.index.tz) == "UTC"
    assert list(df.index) == sorted(df.index)
    assert list(df["close"]) == [100.0, 101.0, 102.0]


def test_polygon_bars_to_df_empty_input():
    df = _polygon_bars_to_df([])
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "vwap", "trade_count"
    ]
    assert len(df) == 0


def test_polygon_bars_to_df_missing_optional_columns():
    """Polygon may omit ``vw`` and ``n`` on some bar types; we backfill NA."""
    bars = [{"t": 1_711_966_800_000, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000}]
    df = _polygon_bars_to_df(bars)
    assert df["vwap"].isna().all()
    assert df["trade_count"].isna().all()
    assert float(df["close"].iloc[0]) == 100.5


def test_polygon_bars_to_df_sorts_ascending_regardless_of_input_order():
    bars = [
        _polygon_bar(1_711_967_400_000, c=102.0),
        _polygon_bar(1_711_966_800_000, c=100.0),
        _polygon_bar(1_711_967_100_000, c=101.0),
    ]
    df = _polygon_bars_to_df(bars)
    assert list(df["close"]) == [100.0, 101.0, 102.0]


# ---------------------------------------------------------------------------
# fetch_aggs: success paths
# ---------------------------------------------------------------------------


async def test_fetch_aggs_success_returns_dataframe(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    bars = [
        _polygon_bar(1_711_966_800_000, c=100.0),
        _polygon_bar(1_711_967_100_000, c=101.0),
    ]
    _install_mock(monkeypatch, lambda req: _ok_payload(bars))

    df = await fetch_aggs(
        "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
    )

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "vwap", "trade_count"
    ]
    assert str(df.index.tz) == "UTC"


async def test_fetch_aggs_builds_correct_url_and_params(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    seen = _install_mock(
        monkeypatch,
        lambda req: _ok_payload([_polygon_bar(1_711_966_800_000)]),
    )

    await fetch_aggs(
        "AAPL", 1, "minute", date(2026, 4, 1), date(2026, 4, 30)
    )

    assert len(seen) == 1
    req = seen[0]
    # URL path components
    assert "/v2/aggs/ticker/AAPL/range/1/minute/2026-04-01/2026-04-30" in str(req.url)
    # Query params
    assert req.url.params["apiKey"] == "test-key"
    assert req.url.params["adjusted"] == "true"
    assert req.url.params["sort"] == "asc"
    assert int(req.url.params["limit"]) == POLYGON_AGGS_PAGE_LIMIT


async def test_fetch_aggs_api_key_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "env-key")
    seen = _install_mock(
        monkeypatch,
        lambda req: _ok_payload([_polygon_bar(1_711_966_800_000)]),
    )

    await fetch_aggs(
        "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 2),
        api_key="arg-key",
    )

    assert seen[0].url.params["apiKey"] == "arg-key"


async def test_fetch_aggs_adjusted_false_propagates(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    seen = _install_mock(
        monkeypatch,
        lambda req: _ok_payload([_polygon_bar(1_711_966_800_000)]),
    )
    await fetch_aggs(
        "SPY", 1, "day", date(2026, 4, 1), date(2026, 4, 2), adjusted=False
    )
    assert seen[0].url.params["adjusted"] == "false"


async def test_fetch_aggs_accepts_all_valid_timespans(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _install_mock(
        monkeypatch,
        lambda req: _ok_payload([_polygon_bar(1_711_966_800_000)]),
    )
    for ts in ["minute", "hour", "day", "week", "month", "quarter", "year"]:
        df = await fetch_aggs(
            "SPY", 1, ts, date(2026, 4, 1), date(2026, 4, 2)
        )
        assert len(df) == 1, f"timespan {ts!r} did not return rows"


# ---------------------------------------------------------------------------
# fetch_aggs: caller-bug input validation (ValueError, no swallowing)
# ---------------------------------------------------------------------------


async def test_fetch_aggs_bad_date_range_raises_value_error():
    with pytest.raises(ValueError, match="end_date"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 30), date(2026, 4, 1)
        )


async def test_fetch_aggs_bad_multiplier_raises_value_error():
    with pytest.raises(ValueError, match="multiplier"):
        await fetch_aggs(
            "SPY", 0, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )


async def test_fetch_aggs_bad_timespan_raises_value_error():
    with pytest.raises(ValueError, match="timespan"):
        await fetch_aggs(
            "SPY", 5, "fortnight", date(2026, 4, 1), date(2026, 4, 30)
        )


# ---------------------------------------------------------------------------
# fetch_aggs: env-var and HTTP error handling (RuntimeError, fail loud)
# ---------------------------------------------------------------------------


async def test_fetch_aggs_missing_key_raises(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLYGON_API_KEY not set"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )


async def test_fetch_aggs_400_raises_with_scrubbed_url(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "MYSECRETKEY")
    _install_mock(monkeypatch, lambda req: httpx.Response(
        400, text="bad request: invalid ticker"
    ))

    with pytest.raises(RuntimeError) as exc:
        await fetch_aggs(
            "BOGUS", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )

    msg = str(exc.value)
    assert "MYSECRETKEY" not in msg, "API key leaked into error message"
    assert "HTTP 400" in msg
    assert "apiKey=<redacted>" in msg


async def test_fetch_aggs_404_does_not_retry(monkeypatch):
    """4xx other than 429 must NOT retry — that would amplify a bad request."""
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _silence_sleep(monkeypatch)
    seen = _install_mock(monkeypatch, lambda req: httpx.Response(
        404, text="not found"
    ))

    with pytest.raises(RuntimeError, match="HTTP 404"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )

    assert len(seen) == 1


async def test_fetch_aggs_429_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _silence_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="rate limited")
        return _ok_payload([_polygon_bar(1_711_966_800_000)])

    _install_mock(monkeypatch, handler)

    df = await fetch_aggs(
        "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
    )

    assert calls["n"] == 3
    assert len(df) == 1


async def test_fetch_aggs_429_persists_raises_after_retries(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _silence_sleep(monkeypatch)
    seen = _install_mock(monkeypatch, lambda req: httpx.Response(
        429, text="still rate limited"
    ))

    with pytest.raises(RuntimeError, match="after 3 retries"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30),
            max_retries=3,
        )

    assert len(seen) == 3


async def test_fetch_aggs_5xx_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _silence_sleep(monkeypatch)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="service unavailable")
        return _ok_payload([_polygon_bar(1_711_966_800_000)])

    _install_mock(monkeypatch, handler)

    df = await fetch_aggs(
        "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
    )
    assert calls["n"] == 2
    assert len(df) == 1


async def test_fetch_aggs_5xx_persists_raises_after_retries(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _silence_sleep(monkeypatch)
    seen = _install_mock(monkeypatch, lambda req: httpx.Response(
        500, text="server error"
    ))

    with pytest.raises(RuntimeError, match="after 3 retries"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30),
            max_retries=3,
        )

    assert len(seen) == 3


# ---------------------------------------------------------------------------
# fetch_aggs: data-quality failure modes
# ---------------------------------------------------------------------------


async def test_fetch_aggs_empty_results_raises(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _install_mock(monkeypatch, lambda req: _ok_payload([]))

    with pytest.raises(RuntimeError, match="0 bars"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )


async def test_fetch_aggs_null_results_raises(monkeypatch):
    """Polygon sometimes returns ``"results": null`` on no-data ranges."""
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    _install_mock(monkeypatch, lambda req: httpx.Response(
        200, json={"status": "OK", "results": None}
    ))

    with pytest.raises(RuntimeError, match="0 bars"):
        await fetch_aggs(
            "SPY", 5, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )


async def test_fetch_aggs_exactly_cap_raises_truncation(monkeypatch):
    """Exactly POLYGON_AGGS_PAGE_LIMIT rows means truncation is likely."""
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    bars = [
        _polygon_bar(1_711_966_800_000 + i * 60_000)
        for i in range(POLYGON_AGGS_PAGE_LIMIT)
    ]
    _install_mock(monkeypatch, lambda req: _ok_payload(bars))

    with pytest.raises(RuntimeError, match="truncated"):
        await fetch_aggs(
            "SPY", 1, "minute", date(2026, 4, 1), date(2026, 4, 30)
        )


async def test_fetch_aggs_one_below_cap_returns_normally(monkeypatch):
    """Truncation guard is exact-equality — len == cap raises, len == cap-1 ok."""
    monkeypatch.setenv("POLYGON_API_KEY", "k")
    bars = [
        _polygon_bar(1_711_966_800_000 + i * 60_000)
        for i in range(POLYGON_AGGS_PAGE_LIMIT - 1)
    ]
    _install_mock(monkeypatch, lambda req: _ok_payload(bars))

    df = await fetch_aggs(
        "SPY", 1, "minute", date(2026, 4, 1), date(2026, 4, 30)
    )
    assert len(df) == POLYGON_AGGS_PAGE_LIMIT - 1


# ---------------------------------------------------------------------------
# Module-level wiring sanity
# ---------------------------------------------------------------------------


def test_module_exposes_public_helpers():
    assert hasattr(polygon_feed, "fetch_aggs")
    assert hasattr(polygon_feed, "_polygon_bars_to_df")
    assert hasattr(polygon_feed, "_require_polygon_key")
    assert hasattr(polygon_feed, "POLYGON_AGGS_PAGE_LIMIT")
    assert polygon_feed.POLYGON_AGGS_PAGE_LIMIT == 50_000
