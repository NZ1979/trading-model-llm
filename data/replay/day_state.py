"""Per-trading-day data-preparation orchestrator for the replay harness.

``build_day_state(config, trading_date, tickers, market_ctx, sentiment_conn)``
is the seam where every replay loader meets the tick loop. Called once
per trading day from the replay-loop driver, it:

  - For each ticker (bounded fan-out, default concurrency=8):
      * loads 25 cal days of 1-min bars (covers PM volume baseline + today)
      * loads 220 cal days of daily bars (covers SMA-200 warmup)
      * loads ticker_metadata as of trading_date
      * loads news for [trading_date - 1d, trading_date]
        (two-day window covers any sub-24h news lookback the tick loop applies)
      * derives DailyContext via compute_daily_context (sync, from analysis/indicators.py)
      * derives PremarketContext via compute_premarket_context with locally-
        derived 20-trading-day PM volume baseline
      * tails the last 5 daily closes for the LLMContext field
  - Once per day:
      * resolves the day's VIX level via .asof() on market_ctx.vix_daily
      * derives market_regime_label by running compute_daily_context on
        SPY daily bars through trading_date - 1 (bull / bear / neutral
        / unknown via the 200 SMA 0.5% buffer)
      * batches all per-ticker news through lookup_article_sentiments
        to build one union ``(article_id, ticker) -> float`` lookup the
        tick loop passes to items_to_context_dicts

``DayState`` carries only what the tick loop needs for THIS trading
day; the run-level ``MarketContextBundle`` (SPY 5-min, SPY daily, VIX
daily) stays owned by the replay-loop driver and is sliced per tick.
No SPY duplication on DayState.

Failure semantics (Rule 18):

  - SPY or VIX missing at run-load time: the MarketContextBundle
    already raises loud (SPY) or sets vix_daily=None (VIX best-effort)
    -- not this module's concern.
  - Per-ticker loader failure (any of bars / daily / metadata / news
    raising RuntimeError): drop the ticker from DayState.tickers with
    a WARNING, record the failure mode in DayState.failed_tickers,
    continue with the rest. Dropping a ticker entirely from the day's
    universe is NOT the Rule-18 "silent zero-substitution" violation
    -- the model never sees that ticker that day at all. A missing
    ticker on one day is a normal data gap (delisting, halt, zero
    volume).
  - All tickers failed: raise RuntimeError. Signals a global outage,
    not a per-ticker gap.
  - DailyContext / PremarketContext returning None (insufficient data):
    pass through to TickerDayState; the context builder + LLM prompt
    template already handle the None paths.

Earnings flags (has_earnings_today, has_earnings_within_3d): hardcoded
False with a TODO for a follow-up sub-task. Implementing the
historical earnings calendar is its own piece (data-source decisions
around Finnhub paid access vs a curated fixture). The comparison
report header documents this approximation.

Status: M2.2 sub-task #9 -- fully implemented.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from analysis.indicators import (
    DailyContext,
    PremarketContext,
    compute_daily_context,
    compute_premarket_context,
)
from data.replay.config import ReplayConfig
from data.replay.historical_bars import (
    load_historical_bars_1min,
    load_historical_bars_daily,
)
from data.replay.historical_news import (
    HistoricalNewsItem,
    load_historical_news,
)
from data.replay.historical_sentiment import lookup_article_sentiments
from data.replay.market_context import MarketContextBundle
from data.replay.ticker_metadata import TickerMetadata, get_ticker_metadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# 25 calendar days back covers 20 trading days even across the longest
# US holiday-stuffed weeks. Used as the per-ticker 1-min bars window
# (for today's session + the trailing 20 days of PM volume baselines).
MINUTE_BARS_LOOKBACK_CAL_DAYS = 25

# 220 calendar days back covers 200 trading days, which compute_daily_context
# requires for the SMA(200). Matches the prepad used by market_context for
# SPY daily.
DAILY_BARS_LOOKBACK_CAL_DAYS = 220

# Default per-ticker concurrency. Mirrors warm_metadata_cache + the
# load_historical_news fan-out: 8 in-flight requests is well below
# Polygon Stocks Starter's throughput cap.
DEFAULT_DAY_PREP_CONCURRENCY = 8

# PM RVOL baseline window (trading days). Matches the live PolygonRESTClient
# default and compute_premarket_context's expected input length.
PM_VOLUME_BASELINE_TRADING_DAYS = 20

# Regime classifier 0.5% SMA-200 buffer. Defined here for the SPY-on-daily
# call; the analysis layer's compute_daily_context uses the same buffer
# internally for per-ticker regime. We re-classify SPY's market_regime_label
# here rather than re-using compute_daily_context's "bull/bear/neutral" output
# directly because the field name on LLMContext is different (regime vs
# market_regime_label) and we want one explicit derivation site.
MARKET_REGIME_LABELS = ("bull", "bear", "neutral", "unknown")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickerDayState:
    """Per-ticker bundle the tick loop reads when building LLMContext.

    Frozen + slots so the assembled state is hashable / cheap and the
    tick loop cannot accidentally mutate it. The DataFrames inside
    remain mutable in principle (pandas doesn't honor frozen at the
    object level), but the tick loop treats them as read-only.
    """

    ticker: str
    minute_bars: pd.DataFrame                     # 04:00-20:00 ET on trading_date (plus prepad days)
    daily_bars: pd.DataFrame                      # trailing ~220 cal days through trading_date - 1
    daily_context: DailyContext | None            # None when <200 daily bars
    premarket_context: PremarketContext | None    # None when PM data insufficient
    ticker_metadata: TickerMetadata               # always present; sentinel fields on data gaps
    news_items: list[HistoricalNewsItem]          # full day's news, unfiltered (tick loop applies lag + lookback)
    last_5_daily_closes: tuple[float, ...]        # oldest first; convenience for LLMContext


@dataclass(frozen=True, slots=True)
class DayState:
    """Run-time bundle for one replay trading day.

    Constructed once per day in build_day_state. The tick loop reads
    everything it needs for that day from this object plus the
    run-level MarketContextBundle (which it slices per tick for SPY
    change_pct / rvol).
    """

    trading_date: date
    vix_level: float | None                       # FRED VIXCLS as-of trading_date; None when VIX feed unavailable
    market_regime_label: str                      # bull / bear / neutral / unknown via SPY 200 SMA through trading_date - 1
    sentiment_lookup: dict[tuple[str, str], float]  # (article_id, ticker) -> sentiment_score (float)
    tickers: dict[str, TickerDayState] = field(default_factory=dict)
    failed_tickers: dict[str, str] = field(default_factory=dict)
    # Earnings flags. Hardcoded False until a separate sub-task adds
    # a historical earnings-calendar loader. The comparison-report
    # header documents this approximation.
    has_earnings_today: dict[str, bool] = field(default_factory=dict)
    has_earnings_within_3d: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_pm_volume_history(
    minute_window_df: pd.DataFrame,
    trading_date: date,
    days: int = PM_VOLUME_BASELINE_TRADING_DAYS,
) -> list[int]:
    """Derive trailing-``days`` PM volume sums from a multi-day minute frame.

    Replay-aware substitute for ``PolygonRESTClient.get_premarket_volume_history``
    (which uses ``datetime.now()`` and can't be made as-of-aware without
    a signature change to a module shared with live).

    Filters ``minute_window_df`` to bars strictly before ``trading_date``
    AND in the pre-market window (04:00 <= ET hour < 09:30), groups by
    calendar date, sums volume, returns the last ``days`` entries
    sorted oldest -> newest. Trading days with zero PM activity get
    a zero entry; non-trading days drop out naturally.

    Empty result is fine -- compute_premarket_context handles
    ``historical_pm_volumes=[]`` by setting RVOL to 0.0.
    """
    if minute_window_df.empty:
        return []
    et = minute_window_df.index  # already tz-aware ET per load_historical_bars_1min
    before_today = et.date < trading_date
    in_pm = ((et.hour >= 4) & (
        (et.hour < 9) | ((et.hour == 9) & (et.minute < 30))
    ))
    pm_only = minute_window_df[before_today & in_pm]
    if pm_only.empty:
        return []
    vols_by_date = pm_only.groupby(pm_only.index.date)["volume"].sum()
    tail = vols_by_date.tail(days)
    return [int(v) for v in tail.values]


def _slice_to_date(
    minute_window_df: pd.DataFrame, trading_date: date
) -> pd.DataFrame:
    """Slice a multi-day minute frame to bars on ``trading_date`` only."""
    if minute_window_df.empty:
        return minute_window_df
    return minute_window_df[minute_window_df.index.date == trading_date]


def _slice_daily_before(
    daily_df: pd.DataFrame, trading_date: date
) -> pd.DataFrame:
    """Slice a daily bars frame to entries strictly before ``trading_date``.

    compute_daily_context and compute_premarket_context both want
    "through yesterday" -- they expect trading_date itself to be
    absent so today's open isn't accidentally treated as a historical
    close.
    """
    if daily_df.empty:
        return daily_df
    return daily_df[daily_df.index.date < trading_date]


def _last_5_daily_closes(daily_df: pd.DataFrame) -> tuple[float, ...]:
    """Return the last 5 daily closes oldest-first as a tuple of floats.

    Convenience for the LLMContext field of the same name. Empty
    DataFrame -> empty tuple; <5 rows -> however many exist, oldest
    first.
    """
    if daily_df.empty:
        return ()
    tail = daily_df.tail(5)["close"]
    return tuple(float(v) for v in tail.values)


def _derive_market_regime_label(spy_daily: pd.DataFrame, trading_date: date) -> str:
    """Classify SPY's regime as bull / bear / neutral / unknown.

    Reuses the live ``compute_daily_context`` heuristic exactly: bull
    when SPY close > sma_200 * 1.005, bear when < sma_200 * 0.995,
    neutral in between. ``unknown`` when SPY has <200 daily bars
    through trading_date - 1 (compute_daily_context returns None).
    """
    spy_through_prior = _slice_daily_before(spy_daily, trading_date)
    ctx = compute_daily_context(spy_through_prior, "SPY")
    if ctx is None:
        return "unknown"
    # compute_daily_context's Regime literal is exactly bull/bear/neutral,
    # which matches the three non-unknown values of MARKET_REGIME_LABELS.
    return str(ctx.regime)


def _resolve_vix_level(
    vix_daily: pd.DataFrame | None, trading_date: date
) -> float | None:
    """Return the most recent VIX close on or before ``trading_date``, or None.

    FRED publishes VIXCLS one business day after the close, so a
    same-day ask is often unavailable for very recent dates. Using
    .asof() picks the latest available <= trading_date, which is what
    the live system would have seen at trading_date's open.
    """
    if vix_daily is None or vix_daily.empty:
        return None
    # vix_daily's index is tz-aware UTC midnight per the
    # data.fred_vix.get_vix_history contract; the .asof query must be
    # tz-aware too or pandas raises "Cannot compare tz-naive and
    # tz-aware timestamps." Converting trading_date (an ET calendar
    # date) to UTC midnight gives 2026-04-15 00:00:00 UTC which is
    # 2026-04-14 20:00 ET; .asof returns the largest index <= that
    # query, which is the most recent VIX close at or before
    # trading_date's open -- exactly the value the live system would
    # have seen.
    val = vix_daily["vix_close"].asof(pd.Timestamp(trading_date, tz="UTC"))
    if pd.isna(val):
        return None
    return float(val)


# ---------------------------------------------------------------------------
# Per-ticker fetch
# ---------------------------------------------------------------------------


async def _load_one_ticker(
    ticker: str,
    trading_date: date,
    *,
    minute_start: date,
    daily_start: date,
    daily_end: date,
) -> TickerDayState:
    """Run the per-ticker loader fan-out for one (ticker, trading_date).

    Raises whatever the loaders raise (RuntimeError on Polygon-side
    failures, ValueError on caller bugs). The build_day_state
    orchestrator catches these for drop-and-continue handling at the
    day-level.
    """
    # Per-ticker concurrency: launch the three independent fetches in
    # parallel so a slow Polygon endpoint doesn't block the others.
    minute_task = load_historical_bars_1min(
        ticker, minute_start, trading_date
    )
    daily_task = load_historical_bars_daily(ticker, daily_start, daily_end)
    metadata_task = get_ticker_metadata(ticker, trading_date)

    minute_bars, daily_bars, metadata = await asyncio.gather(
        minute_task, daily_task, metadata_task
    )

    # Derive contexts. compute_daily_context wants bars through yesterday,
    # so slice. compute_premarket_context wants daily through yesterday
    # AND today's full session bars.
    daily_through_prior = _slice_daily_before(daily_bars, trading_date)
    today_bars = _slice_to_date(minute_bars, trading_date)

    daily_ctx = compute_daily_context(daily_through_prior, ticker)
    pm_volume_history = _derive_pm_volume_history(minute_bars, trading_date)
    if not pm_volume_history:
        logger.warning(
            "day_state: %s on %s has no derivable PM volume baseline; "
            "premarket_rvol will default to 0.0",
            ticker, trading_date,
        )
    pm_ctx = compute_premarket_context(
        daily_through_prior,
        today_bars,
        ticker,
        historical_pm_volumes=pm_volume_history,
    )

    return TickerDayState(
        ticker=ticker,
        minute_bars=minute_bars,
        daily_bars=daily_bars,
        daily_context=daily_ctx,
        premarket_context=pm_ctx,
        ticker_metadata=metadata,
        news_items=[],  # populated below from the day-level batch fetch
        last_5_daily_closes=_last_5_daily_closes(daily_through_prior),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_day_state(
    config: ReplayConfig,
    trading_date: date,
    tickers: tuple[str, ...],
    market_ctx: MarketContextBundle,
    sentiment_conn,
    *,
    concurrency: int = DEFAULT_DAY_PREP_CONCURRENCY,
) -> DayState:
    """Assemble all per-day state for one replay trading day.

    Calls the loaders we shipped in sub-tasks #4-#7 and the analysis
    layer's context builders to produce a tick-loop-ready DayState.
    Run-level inputs (SPY+VIX) come in via ``market_ctx`` so the
    orchestrator does not re-fetch them per day.

    Args:
        config: full ReplayConfig (referenced for ``news_lag_seconds``
            and any future news-lookback knobs the loaders need).
        trading_date: the date to assemble state for. Must be a US
            trading day -- weekends and holidays will fail loudly at
            the bars-fetch step (no bars exist).
        tickers: tuple of equity symbols to include.
        market_ctx: run-level SPY + VIX bundle from
            ``market_context.load_market_data``.
        sentiment_conn: sqlite3 connection from
            ``historical_sentiment.open_fixture``. Owned by the
            caller; this function does NOT close it.
        concurrency: max in-flight per-ticker fetches. Default 8.

    Returns:
        ``DayState`` populated for the day. ``DayState.tickers``
        contains successfully-loaded tickers; ``DayState.failed_tickers``
        maps dropped-ticker symbols to their failure message.

    Raises:
        ValueError: ``tickers`` is empty; ``concurrency < 1``.
        RuntimeError: every ticker failed (signals a global outage,
            not a per-ticker gap).
    """
    if not tickers:
        raise ValueError("build_day_state requires at least one ticker")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    # Run-level derivations first (cheap, no I/O beyond the already-loaded
    # market_ctx).
    market_regime = _derive_market_regime_label(
        market_ctx.spy_daily, trading_date
    )
    vix_level = _resolve_vix_level(market_ctx.vix_daily, trading_date)

    # Per-ticker bars/metadata fan-out.
    minute_start = trading_date - timedelta(days=MINUTE_BARS_LOOKBACK_CAL_DAYS)
    daily_start = trading_date - timedelta(days=DAILY_BARS_LOOKBACK_CAL_DAYS)
    daily_end = trading_date - timedelta(days=1)

    sem = asyncio.Semaphore(concurrency)
    failed: dict[str, str] = {}

    async def fetch_one(ticker: str) -> tuple[str, TickerDayState | None]:
        async with sem:
            try:
                state = await _load_one_ticker(
                    ticker,
                    trading_date,
                    minute_start=minute_start,
                    daily_start=daily_start,
                    daily_end=daily_end,
                )
                return ticker, state
            except Exception as exc:  # broad: capture every loader's raise paths
                logger.warning(
                    "day_state: dropping %s from %s (loader failure: %s)",
                    ticker, trading_date, exc,
                )
                failed[ticker] = f"{type(exc).__name__}: {exc}"
                return ticker, None

    results = await asyncio.gather(*(fetch_one(t) for t in tickers))
    states: dict[str, TickerDayState] = {
        t: s for t, s in results if s is not None
    }

    if not states:
        raise RuntimeError(
            f"build_day_state for {trading_date}: every ticker failed; "
            f"failures: {failed}"
        )

    # Day-level news + sentiment batch. Load news over [trading_date - 1d,
    # trading_date] to cover sub-24h lookbacks from any tick on
    # trading_date. Use only successfully-loaded tickers; news for a
    # dropped ticker is irrelevant (the model never sees it).
    surviving_tickers = tuple(states.keys())
    news_start = trading_date - timedelta(days=1)
    try:
        news_by_ticker = await load_historical_news(
            surviving_tickers, news_start, trading_date
        )
    except Exception as exc:
        # If the news loader fails globally (Polygon outage), the right
        # behavior is to fail loud: news is a direct LLMContext input
        # for every surviving ticker. (Per-ticker news failures are
        # internally raised loud by load_historical_news -- it does
        # not silent-skip.)
        raise RuntimeError(
            f"build_day_state for {trading_date}: news batch failed: {exc}"
        ) from exc

    # Distribute news back into TickerDayStates. Rebuild the dataclasses
    # since they're frozen (slots=True so dataclasses.replace is the
    # right idiom).
    import dataclasses as _dc

    updated_states: dict[str, TickerDayState] = {}
    all_news_items: list[HistoricalNewsItem] = []
    for ticker, state in states.items():
        items = news_by_ticker.get(ticker, [])
        all_news_items.extend(items)
        updated_states[ticker] = _dc.replace(state, news_items=items)

    # One sentiment lookup over the union of every surviving ticker's
    # day-window news. Sync (sqlite3 is local-disk).
    sentiment_lookup = lookup_article_sentiments(
        sentiment_conn, all_news_items
    )

    # Earnings flags: hardcoded False per the M2.2 sub-task #9 decision
    # to defer a historical-earnings-calendar loader to a follow-up.
    # The comparison-report header documents this approximation.
    earnings_today = {t: False for t in updated_states}
    earnings_3d = {t: False for t in updated_states}

    return DayState(
        trading_date=trading_date,
        vix_level=vix_level,
        market_regime_label=market_regime,
        sentiment_lookup=sentiment_lookup,
        tickers=updated_states,
        failed_tickers=failed,
        has_earnings_today=earnings_today,
        has_earnings_within_3d=earnings_3d,
    )


__all__ = [
    "DEFAULT_DAY_PREP_CONCURRENCY",
    "MARKET_REGIME_LABELS",
    "DayState",
    "TickerDayState",
    "build_day_state",
]
