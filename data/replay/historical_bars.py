"""Point-in-time historical bar loader for the replay harness.

Wraps Polygon REST (aggs and daily endpoints) to fetch 1-minute and
daily OHLCV bars over a date range. The replay loop is responsible for
gating to a per-tick "available at this timestamp" window — this module
does the I/O, not the point-in-time slicing.

Status: M2.1 scaffolding stub. Implementation lands in M2.2.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Inputs.
"""
from __future__ import annotations

from datetime import date

import pandas as pd


def load_historical_bars_1min(
    ticker: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Load 1-minute OHLCV bars for one ticker over a date range.

    Returns a DataFrame indexed by tz-aware America/New_York timestamps,
    with columns ``open``, ``high``, ``low``, ``close``, ``volume``,
    ``vwap`` (Polygon-provided), ``trade_count``. Sorted ascending.

    The full range is loaded; callers gate by tick via slicing. Empty
    DataFrame is a fatal condition (Rule 18 fail-loud) — the caller
    raises rather than continuing with no data.

    Will use the existing ``data/polygon_feed.py`` Polygon client when
    implemented; that module already handles the apiKey URL-scrub
    (Rule 22).

    Args:
        ticker: equity symbol (e.g. "AAPL")
        start_date: inclusive
        end_date: inclusive

    Returns:
        DataFrame as described above.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "load_historical_bars_1min is M2.2 work; M2.1 scaffolding only "
        "declares the contract"
    )


def load_historical_bars_daily(
    ticker: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Load daily OHLCV bars for one ticker over a date range.

    Returns a DataFrame indexed by tz-aware America/New_York date
    (midnight ET), with columns ``open``, ``high``, ``low``, ``close``,
    ``volume``, ``vwap``. Sorted ascending.

    Replay needs at minimum 300 trading days back from ``start_date`` to
    warm daily indicators (SMA200 etc.). Callers extend the requested
    range accordingly before invoking.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "load_historical_bars_daily is M2.2 work; M2.1 scaffolding only "
        "declares the contract"
    )
