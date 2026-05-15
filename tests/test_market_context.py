"""Tests for data/replay/market_context.py.

Sub-task #1 coverage (`_load_vix_daily`):

- Success path: returns the DataFrame from fred_vix unchanged.
- Best-effort RuntimeError handling: collapses to None + WARNING log.
- ValueError pass-through: programming errors are not swallowed.
- Unexpected exception pass-through: non-RuntimeError/non-ValueError
  exceptions propagate (we only swallow the documented fred_vix
  failure-mode class).
- Date-argument forwarding: the helper passes start/end exactly as
  given, no implicit padding.
- DataFrame shape: index is tz-aware UTC date, column is `vix_close`.

Sub-task #3 coverage (`load_market_data`):

- Success path: returns MarketContextBundle with all three frames
  populated from mocked fetch_aggs + _load_vix_daily.
- Range handling: SPY 5-min uses replay window as-is; SPY daily and
  VIX daily prepad by SPY_DAILY_PREPAD_CALENDAR_DAYS.
- fetch_aggs called with the documented (multiplier, timespan) pairs
  for each frame: (5, "minute") and (1, "day").
- SPY required (Rule 18): RuntimeError from either SPY fetch
  propagates; replay aborts rather than running with partial context.
- VIX best-effort: RuntimeError from the underlying FRED call
  collapses to vix_daily=None inside the bundle (integration with
  _load_vix_daily's existing best-effort handling).
- Caller-bug input (end_date < start_date) raises ValueError before
  any network call is attempted.
- MarketContextBundle is frozen (dataclass immutability contract).

Underlying network helpers (`fred_vix.get_vix_history`,
`polygon_feed.fetch_aggs`) are monkeypatched. They each have their
own test files covering the real HTTP layer.
"""
from __future__ import annotations

import dataclasses
import logging
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay import market_context
from data.replay.market_context import (
    SPY_DAILY_PREPAD_CALENDAR_DAYS,
    MarketContextBundle,
    _load_vix_daily,
    load_market_data,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vix_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a DataFrame matching fred_vix.get_vix_history's contract."""
    ts = [pd.Timestamp(d, tz="UTC") for d, _ in rows]
    vals = [v for _, v in rows]
    df = pd.DataFrame({"vix_close": vals}, index=pd.Index(ts, name="date"))
    return df.sort_index()


def _patch_get_vix_history(monkeypatch, behavior):
    """Replace fred_vix.get_vix_history with an async fake.

    `behavior` is either:
      - a DataFrame: the fake returns it
      - an Exception instance: the fake raises it
      - a callable(start_date, end_date) -> DataFrame|Exception: the
        fake delegates so tests can capture call args
    """
    captured: list[tuple[date, date]] = []

    async def fake(start_date: date, end_date: date) -> pd.DataFrame:
        captured.append((start_date, end_date))
        result = behavior(start_date, end_date) if callable(behavior) else behavior
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("data.fred_vix.get_vix_history", fake)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history", fake
    )
    return captured


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_load_vix_daily_returns_dataframe_from_fred_vix(monkeypatch):
    expected = _make_vix_df([
        ("2026-04-01", 18.42),
        ("2026-04-02", 19.10),
        ("2026-04-06", 18.95),
    ])
    _patch_get_vix_history(monkeypatch, expected)

    result = await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 6))

    assert result is not None
    pd.testing.assert_frame_equal(result, expected)


async def test_load_vix_daily_preserves_dataframe_shape(monkeypatch):
    """The contract: tz-aware UTC index + vix_close column, sorted asc."""
    expected = _make_vix_df([
        ("2026-04-01", 18.42),
        ("2026-04-02", 19.10),
    ])
    _patch_get_vix_history(monkeypatch, expected)

    result = await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 2))

    assert list(result.columns) == ["vix_close"]
    assert result.index.tz is not None
    assert str(result.index.tz) == "UTC"
    assert list(result.index) == sorted(result.index)


async def test_load_vix_daily_forwards_dates_unchanged(monkeypatch):
    """Helper does not pad/shift dates; caller's job to set the window."""
    captured = _patch_get_vix_history(
        monkeypatch, _make_vix_df([("2026-03-01", 20.0)])
    )

    await _load_vix_daily(date(2026, 3, 1), date(2026, 4, 30))

    assert captured == [(date(2026, 3, 1), date(2026, 4, 30))]


# ---------------------------------------------------------------------------
# Best-effort RuntimeError handling
# ---------------------------------------------------------------------------


async def test_load_vix_daily_returns_none_on_runtime_error(monkeypatch):
    _patch_get_vix_history(
        monkeypatch, RuntimeError("FRED HTTP 400 for ...: invalid key")
    )

    result = await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 6))

    assert result is None


async def test_load_vix_daily_logs_warning_on_runtime_error(
    monkeypatch, caplog
):
    _patch_get_vix_history(
        monkeypatch, RuntimeError("FRED returned 0 observations")
    )

    with caplog.at_level(logging.WARNING, logger="data.replay.market_context"):
        result = await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 6))

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "VIX load failed" in msg
    assert "vix_daily=None" in msg
    assert "FRED returned 0 observations" in msg


async def test_load_vix_daily_handles_all_documented_runtime_failures(
    monkeypatch
):
    """fred_vix raises RuntimeError for: missing key, 4xx, 5xx-retries-
    exhausted, transient-network-retries-exhausted, empty observations,
    all-sentinels. All collapse to None here."""
    failure_messages = [
        "FRED_API_KEY not set in environment.",
        "FRED HTTP 400 for ...",
        "FRED VIX fetch failed after 3 retries: ConnectError(...)",
        "FRED returned 0 observations for VIXCLS over 2026-04-01..2026-04-06",
        "FRED returned only sentinel '.' values over 2026-04-04..2026-04-05",
    ]
    for msg in failure_messages:
        _patch_get_vix_history(monkeypatch, RuntimeError(msg))
        result = await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 6))
        assert result is None, f"expected None for failure: {msg!r}"


# ---------------------------------------------------------------------------
# Programming errors and unexpected exceptions: propagate, do not swallow
# ---------------------------------------------------------------------------


async def test_load_vix_daily_propagates_value_error(monkeypatch):
    """end_date < start_date is a caller bug; swallowing it would hide
    the misconfiguration. Rule 18: fail loud, never fake."""
    _patch_get_vix_history(
        monkeypatch, ValueError("end_date 2026-04-01 is before start_date 2026-04-30")
    )

    with pytest.raises(ValueError, match="end_date"):
        await _load_vix_daily(date(2026, 4, 30), date(2026, 4, 1))


async def test_load_vix_daily_propagates_unexpected_exception(monkeypatch):
    """We only swallow RuntimeError (the documented best-effort failure
    class). A different exception type means something unforeseen broke;
    it must surface, not be hidden behind a None."""
    _patch_get_vix_history(
        monkeypatch, TypeError("unexpected NoneType in fred_vix internals")
    )

    with pytest.raises(TypeError, match="unexpected"):
        await _load_vix_daily(date(2026, 4, 1), date(2026, 4, 6))


# ---------------------------------------------------------------------------
# Module-level wiring sanity
# ---------------------------------------------------------------------------


def test_module_exposes_load_market_data_and_bundle():
    """Cross-check the public surface stays as M2.1 declared it."""
    assert hasattr(market_context, "load_market_data")
    assert hasattr(market_context, "MarketContextBundle")
    assert hasattr(market_context, "_load_vix_daily")
    assert hasattr(market_context, "SPY_DAILY_PREPAD_CALENDAR_DAYS")


# ---------------------------------------------------------------------------
# load_market_data (sub-task #3)
# ---------------------------------------------------------------------------


def _make_polygon_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """Build a DataFrame matching ``polygon_feed._polygon_bars_to_df`` output."""
    ts = [pd.Timestamp(d, tz="UTC") for d, _ in rows]
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
    """Replace ``polygon_feed.fetch_aggs`` with an async fake.

    ``behavior`` is callable(ticker, multiplier, timespan, start, end) ->
    DataFrame or Exception. Returns the captured-call list so tests can
    assert on the exact args fetch_aggs was called with.
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


# Standard mock bodies used across tests.

def _ok_5min(start=date(2026, 4, 1), end=date(2026, 4, 30)) -> pd.DataFrame:
    return _make_polygon_df([
        (f"{start.isoformat()}T13:30:00", 500.0),
        (f"{start.isoformat()}T13:35:00", 500.5),
        (f"{end.isoformat()}T19:55:00", 510.0),
    ])


def _ok_daily(start=date(2025, 1, 1), end=date(2026, 4, 30)) -> pd.DataFrame:
    return _make_polygon_df([
        (start.isoformat(), 480.0),
        (end.isoformat(), 510.0),
    ])


def _ok_vix() -> pd.DataFrame:
    return _make_vix_df([
        ("2026-03-01", 18.0),
        ("2026-04-30", 19.5),
    ])


# Success path -------------------------------------------------------------


async def test_load_market_data_returns_bundle_with_three_frames(monkeypatch):
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    bundle = await load_market_data(date(2026, 4, 1), date(2026, 4, 30))

    assert isinstance(bundle, MarketContextBundle)
    assert len(bundle.spy_5min) == 3
    assert len(bundle.spy_daily) == 2
    assert bundle.vix_daily is not None
    assert len(bundle.vix_daily) == 2


# Range handling -----------------------------------------------------------


async def test_load_market_data_uses_replay_window_for_5min_unchanged(monkeypatch):
    """SPY 5-min fetch_aggs call must receive the exact replay window."""
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    captured = _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    start = date(2026, 4, 1)
    end = date(2026, 4, 30)
    await load_market_data(start, end)

    minute_calls = [c for c in captured if c[2] == "minute"]
    assert len(minute_calls) == 1
    _, mult, _, mcall_start, mcall_end = minute_calls[0]
    assert mult == 5
    assert mcall_start == start
    assert mcall_end == end


async def test_load_market_data_prepads_daily_by_documented_calendar_days(monkeypatch):
    """SPY daily fetch must extend by SPY_DAILY_PREPAD_CALENDAR_DAYS."""
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    captured = _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    start = date(2026, 4, 1)
    end = date(2026, 4, 30)
    await load_market_data(start, end)

    day_calls = [c for c in captured if c[2] == "day"]
    assert len(day_calls) == 1
    _, mult, _, dcall_start, dcall_end = day_calls[0]
    assert mult == 1
    assert dcall_start == start - timedelta(days=SPY_DAILY_PREPAD_CALENDAR_DAYS)
    assert dcall_end == end


async def test_load_market_data_vix_uses_same_prepad_as_daily_spy(monkeypatch):
    """VIX warmup window must match SPY daily so the regime classifier
    sees aligned histories on both sides."""
    captured_vix: list[tuple[date, date]] = []

    async def fake_vix(start_date, end_date):
        captured_vix.append((start_date, end_date))
        return _ok_vix()

    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history", fake_vix
    )

    start = date(2026, 4, 1)
    end = date(2026, 4, 30)
    await load_market_data(start, end)

    assert len(captured_vix) == 1
    assert captured_vix[0] == (
        start - timedelta(days=SPY_DAILY_PREPAD_CALENDAR_DAYS),
        end,
    )


async def test_load_market_data_spy_only_one_5min_and_one_daily_call(monkeypatch):
    """No spurious extra fetch_aggs calls. Two SPY fetches total."""
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    captured = _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    await load_market_data(date(2026, 4, 1), date(2026, 4, 30))

    tickers_called = {c[0] for c in captured}
    granularities_called = {(c[1], c[2]) for c in captured}
    assert tickers_called == {"SPY"}
    assert granularities_called == {(5, "minute"), (1, "day")}
    assert len(captured) == 2


# SPY required (Rule 18) ---------------------------------------------------


async def test_load_market_data_propagates_spy_5min_runtime_error(monkeypatch):
    """A 5-min SPY fetch failure aborts the load; replay cannot proceed
    without intraday SPY context."""
    err = RuntimeError("Polygon HTTP 400 for SPY 5-min")

    def behavior(t, m, ts, s, e):
        if ts == "minute":
            return err
        return _ok_daily(s, e)

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    with pytest.raises(RuntimeError, match="5-min"):
        await load_market_data(date(2026, 4, 1), date(2026, 4, 30))


async def test_load_market_data_propagates_spy_daily_runtime_error(monkeypatch):
    """A daily SPY fetch failure aborts the load. The regime classifier
    needs SMA200-class warmup; without it, replay produces invalid
    market_regime_label."""
    err = RuntimeError("Polygon returned 0 bars for SPY 1/day")

    def behavior(t, m, ts, s, e):
        if ts == "day":
            return err
        return _ok_5min()

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    with pytest.raises(RuntimeError, match="0 bars"):
        await load_market_data(date(2026, 4, 1), date(2026, 4, 30))


# VIX best-effort (integration with _load_vix_daily) -----------------------


async def test_load_market_data_vix_failure_yields_none_not_raise(monkeypatch):
    """Any FRED-side RuntimeError must collapse to vix_daily=None and
    let the load complete successfully."""
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(RuntimeError("FRED_API_KEY not set in environment.")),
    )

    bundle = await load_market_data(date(2026, 4, 1), date(2026, 4, 30))

    assert bundle.vix_daily is None
    assert len(bundle.spy_5min) > 0
    assert len(bundle.spy_daily) > 0


# Caller-bug input (Rule 18 fail loud) -------------------------------------


async def test_load_market_data_bad_date_range_raises_value_error(monkeypatch):
    """end_date < start_date is a caller bug; raise before any I/O."""
    captured = _patch_fetch_aggs(
        monkeypatch,
        lambda *a: pytest.fail("fetch_aggs should not be called"),
    )

    with pytest.raises(ValueError, match="end_date"):
        await load_market_data(date(2026, 4, 30), date(2026, 4, 1))

    assert captured == [], "fetch_aggs was called despite bad input"


# Bundle immutability ------------------------------------------------------


async def test_load_market_data_returns_frozen_bundle(monkeypatch):
    def behavior(t, m, ts, s, e):
        return _ok_5min() if ts == "minute" else _ok_daily(s, e)

    _patch_fetch_aggs(monkeypatch, behavior)
    monkeypatch.setattr(
        "data.replay.market_context.fred_vix.get_vix_history",
        _make_async(_ok_vix()),
    )

    bundle = await load_market_data(date(2026, 4, 1), date(2026, 4, 30))

    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.spy_5min = pd.DataFrame()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helpers used by the sub-task #3 tests
# ---------------------------------------------------------------------------


def _make_async(result):
    """Wrap a value (DataFrame or Exception) in an async coroutine."""
    async def coro(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result
    return coro
