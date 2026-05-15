"""Tests for data/replay/market_context.py.

Covers the M2.2 sub-task #1 VIX helper (`_load_vix_daily`):

- Success path: returns the DataFrame from fred_vix unchanged.
- Best-effort RuntimeError handling: collapses to None + WARNING log.
- ValueError pass-through: programming errors are not swallowed.
- Unexpected exception pass-through: non-RuntimeError/non-ValueError
  exceptions propagate (we only swallow the documented fred_vix
  failure-mode class).
- Date-argument forwarding: the helper passes start/end exactly as
  given, no implicit padding.
- DataFrame shape: index is tz-aware UTC date, column is `vix_close`.

The underlying `fred_vix.get_vix_history` is monkeypatched. It has its
own test file (`tests/test_fred_vix.py`) covering the HTTP layer.

The `load_market_data` stub assertion still lives in
`tests/test_replay_scaffolding.py` and stays green here — sub-task #1
only adds the VIX helper; the `NotImplementedError` for
`load_market_data` is replaced atomically in sub-task #3 when both
SPY and VIX halves combine.
"""
from __future__ import annotations

import logging
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay import market_context
from data.replay.market_context import _load_vix_daily


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
    """Cross-check the public surface stays as M2.1 declared it.

    `load_market_data` itself still raises NotImplementedError until
    sub-task #3 — that assertion lives in test_replay_scaffolding.py."""
    assert hasattr(market_context, "load_market_data")
    assert hasattr(market_context, "MarketContextBundle")
    assert hasattr(market_context, "_load_vix_daily")
