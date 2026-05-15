"""Point-in-time historical bar loader for the replay harness.

Wraps ``data.polygon_feed.fetch_aggs`` to fetch 1-minute and daily
OHLCV bars over a date range. The replay loop is responsible for
gating to a per-tick "available at this timestamp" window — this
module does the I/O and the timestamp re-anchor, not the
point-in-time slicing.

Status: M2.2 sub-task #4. Both loaders fully wired against fetch_aggs.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Inputs.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from data import polygon_feed


async def load_historical_bars_1min(
    ticker: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Load 1-minute OHLCV bars for one ticker over a date range.

    Returns a DataFrame indexed by tz-aware America/New_York
    timestamps, with columns ``open``, ``high``, ``low``, ``close``,
    ``volume``, ``vwap``, ``trade_count``. Sorted ascending.

    The full range is loaded; callers gate by tick via slicing. Empty
    DataFrame is a fatal condition (Rule 18 fail-loud) — ``fetch_aggs``
    raises ``RuntimeError`` on empty results rather than letting the
    replay continue with no data.

    Args:
        ticker: equity symbol (e.g. "AAPL"). Case-sensitive on
            Polygon's side; not upper-cased here.
        start_date: inclusive
        end_date: inclusive

    Returns:
        DataFrame as described above.

    Raises:
        ValueError: ``end_date < start_date`` (passed through from
            ``fetch_aggs``). Caller bug.
        RuntimeError: Polygon-side failure (bad key, unknown ticker,
            4xx, retries exhausted, empty results, truncation at
            ``POLYGON_AGGS_PAGE_LIMIT``).

    Note on the timezone re-anchor: ``fetch_aggs`` returns a
    UTC-indexed DataFrame (the repo convention at the feed layer).
    This wrapper tz_converts to America/New_York so the index reads
    naturally for trading-hour slicing (09:30 ET, 09:35 ET, etc.) —
    the exact granularity the replay loop's eval ticks operate on.
    """
    df = await polygon_feed.fetch_aggs(
        ticker, 1, "minute", start_date, end_date
    )
    df.index = df.index.tz_convert("America/New_York")
    return df


async def load_historical_bars_daily(
    ticker: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Load daily OHLCV bars for one ticker over a date range.

    Returns a DataFrame indexed by tz-aware America/New_York midnight
    (one row per trading day), with columns ``open``, ``high``,
    ``low``, ``close``, ``volume``, ``vwap``, ``trade_count``. Sorted
    ascending.

    Replay needs at minimum 300 trading days back from ``start_date``
    to warm daily indicators (SMA200, ATR, etc.). Callers extend the
    requested range accordingly BEFORE invoking; this function does
    not pre-pad (unlike ``market_context.load_market_data`` which
    handles its own prepad because the window is fixed at 300d for
    every replay run). Keeping the prepad in the caller lets the
    replay loop set per-ticker warmup if needed (some indicators
    need less history).

    Args:
        ticker: equity symbol
        start_date: inclusive, already prepadded by the caller
        end_date: inclusive

    Returns:
        DataFrame as described above.

    Raises:
        ValueError: ``end_date < start_date``.
        RuntimeError: Polygon-side failure as in
            ``load_historical_bars_1min``.

    Note on the timezone re-anchor: Polygon's daily bars come back
    indexed at midnight UTC of the trading day. Converting that
    directly to America/New_York gives 19:00 or 20:00 ET on the
    PRIOR calendar date (depending on DST), which reads wrong for
    trading-day-keyed lookups. We instead extract the UTC date
    component (which IS the trading day) and re-anchor to midnight
    ET of that same date. This is a semantic interpretation
    (Polygon's daily ``t`` represents a trading session in ET), not
    a timezone conversion — different from the 1-min path on
    purpose.
    """
    df = await polygon_feed.fetch_aggs(
        ticker, 1, "day", start_date, end_date
    )
    # Re-anchor: UTC midnight of trading-day -> ET midnight of same date.
    df.index = (
        df.index
        .tz_convert(None)        # strip UTC tz, keep the wall clock (00:00:00)
        .normalize()             # floor to midnight (no-op; already midnight)
        .tz_localize("America/New_York")
    )
    return df
