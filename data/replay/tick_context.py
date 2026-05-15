"""Per-tick LLMContext slicing for the M2 replay harness.

``build_tick_context`` is the pure function the replay tick loop calls
on every (ticker, tick_et) pair. Given the day's pre-loaded state
(``DayState`` from sub-task #9), the run-level SPY/VIX bundle
(``MarketContextBundle`` from #1-#3), the run config, and the current
tick timestamp, it slices the per-ticker 1-min bars to as-of, resamples
to 5-min, runs ``compute_intraday_indicators``, filters and shapes the
visible news, derives the tick-time SPY and time-of-day fields, and
assembles an ``LLMContext`` via the shared ``build_llm_context``.

No async, no I/O, no LLM. The function is the deterministic adapter
between the day-level data prep and the LLM evaluation step. The tick
loop in a later sub-task calls this once per surviving ticker per
tick.

Module conventions per the design doc:

- Tick ticks are 5-minute aligned ET timestamps: 09:30, 09:35, ...,
  15:55. The function does not enforce alignment -- the tick loop owns
  scheduling. Any tz-aware ET datetime on ``day_state.trading_date``
  is accepted.
- SPY change-pct is "session change so far" per
  ``docs/LLM_SIGNAL_INTERFACE.md`` line 212.
- SPY rvol is the simpler current-bar-vs-20-bar-SMA ratio rather than
  the richer "cumulative since 09:30 vs 20-day mean cumulative at the
  same time of day." The simpler form matches the per-ticker
  ``volume_ratio_vs_20bar`` convention from compute_intraday_indicators
  and is non-misleading; a follow-up sub-task may upgrade to the
  cumulative version once the report design calls for it.
- News lookback for the LLM context is hardcoded 24h per the
  ``LLMContext.news_items`` field-comment spec ("filtered to last 24h,
  capped at 5 items"). The ``pre_filter_news_lookback_hours`` config
  knob (default 2h) is a SEPARATE gate used by the pre-filter, not
  here.
- News items are capped at the top 5 most recent visible per the same
  field-comment spec.
- ``in_gap_and_go_window`` is True for the first 60 minutes of RTH
  (09:30-10:30 ET inclusive of 09:30, exclusive of 10:31). Matches the
  live gap-and-go signal's morning-window semantics.
- ``catalyst_flags`` is hardcoded to ``()``. Real catalyst-flag
  derivation needs headline keyword extraction or a separate
  classifier signal -- its own sub-task. The LLM still sees the
  underlying headlines via ``news_items``.

Status: M2.2 sub-task #10 -- fully implemented.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from analysis.indicators import compute_intraday_indicators
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState
from data.replay.historical_news import (
    filter_visible_at,
    items_to_context_dicts,
)
from data.replay.market_context import MarketContextBundle
from strategy.llm.context_builder import build_llm_context
from strategy.llm.types import LLMContext


ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# LLMContext.news_items spec: last 24h, top 5 items.
LLM_NEWS_LOOKBACK_HOURS = 24
LLM_NEWS_MAX_ITEMS = 5

# Gap-and-go morning window (live convention).
GAP_AND_GO_WINDOW_MINUTES = 60

# RTH session anchors used for minutes_since_open / minutes_until_close.
RTH_OPEN_HOUR = 9
RTH_OPEN_MINUTE = 30
RTH_CLOSE_HOUR = 16
RTH_CLOSE_MINUTE = 0
RTH_LENGTH_MINUTES = 390  # 09:30 -> 16:00

# Volume-ratio window for SPY rvol; matches per-ticker
# volume_ratio_vs_20bar convention.
SPY_RVOL_WINDOW_BARS = 20


# ---------------------------------------------------------------------------
# 5-minute aggregation
# ---------------------------------------------------------------------------


def _vwap_weighted_mean(group: pd.DataFrame) -> float:
    """Volume-weighted mean of vwap within a resample group.

    Standard reaggregation: sum(vwap * volume) / sum(volume). When the
    group has zero total volume (highly unusual; can happen on an empty
    resample window the caller didn't filter out), fall back to a
    plain mean so the field has a value instead of NaN. compute_intraday_
    indicators happens to overwrite the vwap column with its own rolling
    calculation anyway, so the value here mostly matters as a tie-break
    when the 5-min frame's last bar is read directly elsewhere.
    """
    vol = group["volume"].sum()
    if vol <= 0:
        return float(group["vwap"].mean()) if not group["vwap"].empty else 0.0
    return float((group["vwap"] * group["volume"]).sum() / vol)


def _aggregate_5min(one_min_df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1-min OHLCV bars to standard 5-min bars.

    Empty input -> empty output (same columns). Otherwise produces a
    DataFrame indexed at the 5-min window start (09:30, 09:35, ...)
    with columns ``open, high, low, close, volume, vwap, trade_count``.
    Skips any 5-min window that has zero 1-min source bars (no trades
    = no bar; matches BarAggregator's live convention).
    """
    if one_min_df.empty:
        return one_min_df.iloc[0:0]
    # Resample left-closed left-labeled so the 09:30-09:34 1-min bars
    # form the "09:30" 5-min bar. The agg dict spells out the standard
    # OHLCV reductions; vwap goes through a volume-weighted helper.
    grouped = one_min_df.resample(
        "5min", label="left", closed="left", origin="start_day"
    )
    out = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "trade_count": "sum",
        }
    )
    # Drop rows where every value is NaN (gaps in the source 1-min frame).
    out = out.dropna(how="all")
    if out.empty:
        return out
    # Compute vol-weighted vwap per group. resample().apply returns one
    # value per group; reindex against the aggregated frame.
    vwap_series = grouped.apply(_vwap_weighted_mean)
    out["vwap"] = vwap_series.reindex(out.index)
    return out[["open", "high", "low", "close", "volume", "vwap", "trade_count"]]


# ---------------------------------------------------------------------------
# SPY-derived tick fields
# ---------------------------------------------------------------------------


def _spy_slice_today_to_as_of(
    spy_5min: pd.DataFrame, tick_et: datetime
) -> pd.DataFrame:
    """Return SPY 5-min bars on tick_et's date at or before tick_et.

    Empty frame in -> empty frame out. tick_et must be tz-aware; SPY's
    index is tz-aware UTC per polygon_feed convention, so we compare
    timezone-correctly via pandas Timestamp.
    """
    if spy_5min.empty:
        return spy_5min
    tick_ts = pd.Timestamp(tick_et)
    # SPY frame is UTC-indexed; convert tick_et to UTC for comparison.
    if tick_ts.tzinfo is not None:
        tick_ts = tick_ts.tz_convert("UTC")
    et_index = spy_5min.index.tz_convert(ET) if spy_5min.index.tz is not None else spy_5min.index
    same_date = et_index.date == tick_et.date()
    before_or_at = spy_5min.index <= tick_ts
    return spy_5min[same_date & before_or_at]


def _spy_change_pct(spy_today_to_as_of: pd.DataFrame) -> float:
    """SPY session change percent: (last_close - first_open) / first_open * 100.

    Returns 0.0 on empty input (matches LLMContext.spy_change_pct default).
    Returns 0.0 if first open is non-positive (shouldn't happen on SPY
    but defends against degenerate data).
    """
    if spy_today_to_as_of.empty:
        return 0.0
    first_open = float(spy_today_to_as_of["open"].iloc[0])
    if first_open <= 0:
        return 0.0
    last_close = float(spy_today_to_as_of["close"].iloc[-1])
    return (last_close - first_open) / first_open * 100.0


def _spy_rvol(spy_5min: pd.DataFrame, spy_today_to_as_of: pd.DataFrame) -> float:
    """Current SPY 5-min bar volume divided by the 20-bar SMA of SPY volume.

    The 20-bar SMA spans the full SPY 5-min frame (not just today). On
    the typical replay window the trailing 20 bars are ~100 minutes of
    SPY action; even on a single-day replay there are enough RTH bars
    to populate.

    Falls back to 1.0 (the LLMContext default) when:
      - the today-to-as_of slice is empty (no SPY data at the tick)
      - the 20-bar SMA is zero or NaN
    """
    if spy_today_to_as_of.empty:
        return 1.0
    current_volume = float(spy_today_to_as_of["volume"].iloc[-1])
    tail = spy_5min["volume"].tail(SPY_RVOL_WINDOW_BARS)
    if tail.empty:
        return 1.0
    baseline = float(tail.mean())
    if not (baseline > 0):
        return 1.0
    return current_volume / baseline


# ---------------------------------------------------------------------------
# Time-of-day fields
# ---------------------------------------------------------------------------


def _minutes_since_open(tick_et: datetime) -> int:
    """Minutes from 09:30 ET on tick_et's date to tick_et, clamped [0, 390]."""
    open_dt = tick_et.replace(
        hour=RTH_OPEN_HOUR, minute=RTH_OPEN_MINUTE, second=0, microsecond=0
    )
    delta_min = int((tick_et - open_dt).total_seconds() // 60)
    if delta_min < 0:
        return 0
    if delta_min > RTH_LENGTH_MINUTES:
        return RTH_LENGTH_MINUTES
    return delta_min


def _minutes_until_close(tick_et: datetime) -> int:
    """Minutes from tick_et to 16:00 ET on tick_et's date, clamped [0, 390]."""
    close_dt = tick_et.replace(
        hour=RTH_CLOSE_HOUR, minute=RTH_CLOSE_MINUTE, second=0, microsecond=0
    )
    delta_min = int((close_dt - tick_et).total_seconds() // 60)
    if delta_min < 0:
        return 0
    if delta_min > RTH_LENGTH_MINUTES:
        return RTH_LENGTH_MINUTES
    return delta_min


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_tick_context(
    *,
    day_state: DayState,
    market_ctx: MarketContextBundle,
    config: ReplayConfig,
    ticker: str,
    tick_et: datetime,
    position: dict | None = None,
    todays_prior_decisions: tuple[dict, ...] = (),
) -> LLMContext:
    """Assemble an LLMContext for ``(ticker, tick_et)`` from pre-loaded state.

    Pure function. Slices the day's data to as-of, runs indicator
    computation, gates news visibility, derives the tick-time SPY and
    time-of-day fields, and dispatches to
    ``strategy.llm.context_builder.build_llm_context`` for the
    LLMContext assembly.

    Args:
        day_state: per-day bundle from ``data.replay.day_state.build_day_state``.
        market_ctx: run-level SPY+VIX bundle from
            ``data.replay.market_context.load_market_data``.
        config: full ReplayConfig (read for ``news_lag_seconds`` and
            ``llm_prompt_version``).
        ticker: the symbol to build context for. Must be present in
            ``day_state.tickers`` -- the tick loop iterates only over
            surviving tickers, so a missing key is a caller bug.
        tick_et: the current replay tick timestamp. tz-aware
            America/New_York, typically 5-min aligned (09:30, 09:35,
            ..., 15:55). The function does not enforce alignment.
        position: per-ticker portfolio state dict shaped
            ``{qty, avg_price, unrealized_pl_pct, has_active_stop}``,
            or None for flat. Passed through to build_llm_context.
        todays_prior_decisions: tuple of prior-decision dicts for this
            ticker on this trading day, shaped per
            ``LLMContext.todays_prior_decisions``. Passed through.

    Returns:
        Fully-populated ``LLMContext``.

    Raises:
        KeyError: ``ticker`` not in ``day_state.tickers``. The tick
            loop should iterate over ``day_state.tickers.keys()``.
    """
    tds = day_state.tickers[ticker]

    # ---- Slice per-ticker 1-min bars to as-of, resample to 5-min, indicators ----
    if tds.minute_bars.empty:
        today_minute = tds.minute_bars
    else:
        # tds.minute_bars is tz-aware ET per load_historical_bars_1min.
        same_date = tds.minute_bars.index.date == tick_et.date()
        before_or_at = tds.minute_bars.index <= pd.Timestamp(tick_et)
        today_minute = tds.minute_bars[same_date & before_or_at]
    bars_5min = _aggregate_5min(today_minute)
    df_ind = compute_intraday_indicators(bars_5min)

    # ---- News: filter visible at tick, cap top 5 most recent ----
    visible_news = filter_visible_at(
        tds.news_items,
        as_of_et=tick_et,
        lookback_hours=LLM_NEWS_LOOKBACK_HOURS,
        lag_seconds=config.news_lag_seconds,
    )
    # filter_visible_at returns items in input order; the news loader
    # sorts items ascending by ts_et in build_day_state so the trailing
    # slice is "most recent N."
    capped_news = visible_news[-LLM_NEWS_MAX_ITEMS:]
    news_dicts = items_to_context_dicts(capped_news, day_state.sentiment_lookup)

    # ---- SPY-derived tick fields ----
    spy_today = _spy_slice_today_to_as_of(market_ctx.spy_5min, tick_et)
    spy_change = _spy_change_pct(spy_today)
    spy_rvol = _spy_rvol(market_ctx.spy_5min, spy_today)

    # ---- Time-of-day ----
    minutes_since_open = _minutes_since_open(tick_et)
    minutes_until_close = _minutes_until_close(tick_et)
    in_gap_and_go = minutes_since_open <= GAP_AND_GO_WINDOW_MINUTES

    # ---- Per-ticker fundamentals + earnings flags ----
    meta = tds.ticker_metadata
    earnings_today = day_state.has_earnings_today.get(ticker, False)
    earnings_3d = day_state.has_earnings_within_3d.get(ticker, False)

    return build_llm_context(
        ticker=ticker,
        timestamp_et=tick_et.isoformat(),
        prompt_version=config.llm_prompt_version,
        df_ind=df_ind,
        daily_ctx=tds.daily_context,
        premarket_ctx=tds.premarket_context,
        sentiment=None,
        news_items=news_dicts,
        has_earnings_today=earnings_today,
        has_earnings_within_3d=earnings_3d,
        position=position,
        spy_change_pct=spy_change,
        spy_rvol=spy_rvol,
        vix_level=day_state.vix_level,
        market_regime_label=day_state.market_regime_label,
        sector=meta.sector,
        market_cap_bucket=meta.market_cap_bucket,
        avg_daily_volume=meta.avg_daily_volume,
        minutes_since_open=minutes_since_open,
        minutes_until_close=minutes_until_close,
        in_gap_and_go_window=in_gap_and_go,
        todays_prior_decisions=todays_prior_decisions,
        catalyst_flags=(),
        last_5_daily_closes=tds.last_5_daily_closes,
    )


__all__ = [
    "GAP_AND_GO_WINDOW_MINUTES",
    "LLM_NEWS_LOOKBACK_HOURS",
    "LLM_NEWS_MAX_ITEMS",
    "SPY_RVOL_WINDOW_BARS",
    "build_tick_context",
]
