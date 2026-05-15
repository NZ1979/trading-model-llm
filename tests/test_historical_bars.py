"""Tests for data/replay/historical_bars.py (M2.2 sub-task #4).

Covers the per-ticker bar loaders that wrap
``data.polygon_feed.fetch_aggs``:

- 1-min loader: tz_converts UTC -> America/New_York so the index
  matches the replay loop's eval-tick wall-clock semantics.
- daily loader: re-anchors Polygon's UTC-midnight trading-day index
  to ET-midnight of the same date (semantic re-anchor, not a tz
  conversion — see the docstring in historical_bars.py).
- Both functions: ValueError and RuntimeError from fetch_aggs
  propagate unchanged. No silent fallbacks per Rule 18.

The underlying ``polygon_feed.fetch_aggs`` is monkeypatched. The HTTP
layer is covered by ``tests/test_polygon_fetch_aggs.py``.

The 1-min re-anchor is exercised in both DST and EST to catch any
offset arithmetic bug that would only surface across a DST boundary.
The daily re-anchor is tested against the specific failure mode it
exists to prevent: naive tz_convert(ET) would drop the bar onto the
PRIOR calendar date (~19:00 or 20:00 ET previous day).
"""
from __future__ import annotations

import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay import historical_bars
from data.replay.historical_bars import (
    load_historical_bars_1min,
    load_historical_bars_daily,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _polygon_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a DataFrame matching ``polygon_feed._polygon_bars_to_df`` output."""
    ts = [pd.Timestamp(s, tz="UTC") for s, _ in rows]
    closes = [c for _, c in rows]
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [10_000] * len(rows),
            "vwap": closes,
            "trade_count": [1] * len(rows),
        },
        index=pd.Index(ts, name="ts"),
    ).sort_index()


def _patch_fetch_aggs(monkeypatch, behavior):
    """Replace polygon_feed.fetch_aggs with an async fake.

    ``behavior`` is callable(ticker, multiplier, timespan, start, end)
    -> DataFrame or Exception. Returns the list of captured calls.
    """
    captured: list[tuple] = []

    async def fake(
        ticker, multiplier, timespan, start_date, end_date, **kwargs
    ):
        captured.append((ticker, multiplier, timespan, start_date, end_date))
        result = behavior(ticker, multiplier, timespan, start_date, end_date)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("data.polygon_feed.fetch_aggs", fake)
    return captured


# ---------------------------------------------------------------------------
# load_historical_bars_1min
# ---------------------------------------------------------------------------


async def test_load_1min_calls_fetch_aggs_with_correct_args(monkeypatch):
    df_in = _polygon_df([("2026-04-15T13:30:00", 150.0)])
    captured = _patch_fetch_aggs(monkeypatch, lambda t, m, ts, s, e: df_in)

    await load_historical_bars_1min(
        "AAPL", date(2026, 4, 1), date(2026, 4, 30)
    )

    assert captured == [("AAPL", 1, "minute", date(2026, 4, 1), date(2026, 4, 30))]


async def test_load_1min_returns_et_indexed_dataframe(monkeypatch):
    df_in = _polygon_df([
        ("2026-04-15T13:30:00", 150.0),  # 09:30 EDT (UTC-4)
        ("2026-04-15T13:31:00", 150.1),
    ])
    _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    df = await load_historical_bars_1min(
        "AAPL", date(2026, 4, 15), date(2026, 4, 15)
    )

    assert str(df.index.tz) == "America/New_York"
    # 13:30 UTC = 09:30 EDT (April is in DST).
    assert df.index[0].hour == 9
    assert df.index[0].minute == 30


async def test_load_1min_handles_dst_correctly(monkeypatch):
    """13:30 UTC in April (EDT, UTC-4) -> 09:30 ET; same UTC in
    January (EST, UTC-5) -> 08:30 ET. Confirms tz_convert, not a
    fixed offset."""
    df_apr = _polygon_df([("2026-04-15T13:30:00", 150.0)])
    df_jan = _polygon_df([("2026-01-15T13:30:00", 150.0)])

    def behavior(t, m, ts, s, e):
        if s.month == 4:
            return df_apr
        return df_jan

    _patch_fetch_aggs(monkeypatch, behavior)

    apr = await load_historical_bars_1min(
        "AAPL", date(2026, 4, 15), date(2026, 4, 15)
    )
    jan = await load_historical_bars_1min(
        "AAPL", date(2026, 1, 15), date(2026, 1, 15)
    )

    # 13:30 UTC = 09:30 EDT in April
    assert apr.index[0].hour == 9
    assert apr.index[0].minute == 30
    # 13:30 UTC = 08:30 EST in January
    assert jan.index[0].hour == 8
    assert jan.index[0].minute == 30


async def test_load_1min_preserves_ohlcv_data(monkeypatch):
    df_in = _polygon_df([
        ("2026-04-15T13:30:00", 150.0),
        ("2026-04-15T13:31:00", 151.0),
        ("2026-04-15T13:32:00", 149.5),
    ])
    _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    df = await load_historical_bars_1min(
        "AAPL", date(2026, 4, 15), date(2026, 4, 15)
    )

    assert list(df["close"]) == [150.0, 151.0, 149.5]
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "vwap", "trade_count"
    ]


async def test_load_1min_propagates_value_error(monkeypatch):
    err = ValueError("end_date 2026-04-01 is before start_date 2026-04-30")
    _patch_fetch_aggs(monkeypatch, lambda *a: err)

    with pytest.raises(ValueError, match="end_date"):
        await load_historical_bars_1min(
            "AAPL", date(2026, 4, 30), date(2026, 4, 1)
        )


async def test_load_1min_propagates_runtime_error(monkeypatch):
    err = RuntimeError("Polygon HTTP 404 for AAPL")
    _patch_fetch_aggs(monkeypatch, lambda *a: err)

    with pytest.raises(RuntimeError, match="404"):
        await load_historical_bars_1min(
            "BOGUS", date(2026, 4, 1), date(2026, 4, 30)
        )


# ---------------------------------------------------------------------------
# load_historical_bars_daily
# ---------------------------------------------------------------------------


async def test_load_daily_calls_fetch_aggs_with_correct_args(monkeypatch):
    df_in = _polygon_df([("2026-04-15T00:00:00", 150.0)])
    captured = _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    await load_historical_bars_daily(
        "AAPL", date(2026, 4, 1), date(2026, 4, 30)
    )

    assert captured == [("AAPL", 1, "day", date(2026, 4, 1), date(2026, 4, 30))]


async def test_load_daily_returns_et_midnight_indexed(monkeypatch):
    df_in = _polygon_df([
        ("2026-04-15T00:00:00", 150.0),  # Polygon's t for 2026-04-15
        ("2026-04-16T00:00:00", 151.0),
    ])
    _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    df = await load_historical_bars_daily(
        "AAPL", date(2026, 4, 15), date(2026, 4, 16)
    )

    assert str(df.index.tz) == "America/New_York"
    # The re-anchor must land on 2026-04-15 00:00 ET, NOT
    # 2026-04-14 20:00 ET (which is what naive tz_convert would
    # produce from 2026-04-15 00:00 UTC).
    assert df.index[0].year == 2026
    assert df.index[0].month == 4
    assert df.index[0].day == 15
    assert df.index[0].hour == 0
    assert df.index[0].minute == 0
    assert df.index[1].day == 16


async def test_load_daily_reanchor_survives_dst_boundary(monkeypatch):
    """Trading days that straddle the spring-forward Sunday (2026-03-08)
    must still land on their own calendar date. The re-anchor is a
    semantic interpretation, not a UTC-to-ET conversion, so DST
    transitions cannot off-by-one it."""
    df_in = _polygon_df([
        ("2026-03-06T00:00:00", 100.0),  # Fri before DST starts
        ("2026-03-09T00:00:00", 101.0),  # Mon after DST starts
    ])
    _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    df = await load_historical_bars_daily(
        "AAPL", date(2026, 3, 1), date(2026, 3, 15)
    )

    assert df.index[0].day == 6 and df.index[0].month == 3
    assert df.index[1].day == 9 and df.index[1].month == 3


async def test_load_daily_preserves_ohlcv_data(monkeypatch):
    df_in = _polygon_df([
        ("2026-04-15T00:00:00", 150.0),
        ("2026-04-16T00:00:00", 151.0),
    ])
    _patch_fetch_aggs(monkeypatch, lambda *a: df_in)

    df = await load_historical_bars_daily(
        "AAPL", date(2026, 4, 15), date(2026, 4, 16)
    )

    assert list(df["close"]) == [150.0, 151.0]
    assert list(df.columns) == [
        "open", "high", "low", "close", "volume", "vwap", "trade_count"
    ]


async def test_load_daily_propagates_value_error(monkeypatch):
    err = ValueError("end_date 2026-04-01 is before start_date 2026-04-30")
    _patch_fetch_aggs(monkeypatch, lambda *a: err)

    with pytest.raises(ValueError, match="end_date"):
        await load_historical_bars_daily(
            "AAPL", date(2026, 4, 30), date(2026, 4, 1)
        )


async def test_load_daily_propagates_runtime_error(monkeypatch):
    err = RuntimeError("Polygon returned 0 bars for AAPL 1/day")
    _patch_fetch_aggs(monkeypatch, lambda *a: err)

    with pytest.raises(RuntimeError, match="0 bars"):
        await load_historical_bars_daily(
            "AAPL", date(2026, 4, 1), date(2026, 4, 30)
        )


# ---------------------------------------------------------------------------
# Module sanity
# ---------------------------------------------------------------------------


def test_module_exposes_both_loaders():
    assert hasattr(historical_bars, "load_historical_bars_1min")
    assert hasattr(historical_bars, "load_historical_bars_daily")
