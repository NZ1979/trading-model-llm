"""SPY + VIX market context loader for the replay harness.

Populates the market-context fields of ``LLMContext``:
``spy_change_pct``, ``spy_rvol``, ``vix_level``. Also feeds the regime
classifier (``analysis/regime.py``) which produces
``market_regime_label``.

Sources:

- SPY 5-min and SPY daily: Polygon via ``data.polygon_feed.fetch_aggs``.
  The replay window is used as-is for 5-min; SPY daily and VIX daily
  pre-pad by ``SPY_DAILY_PREPAD_CALENDAR_DAYS`` (~460 calendar days,
  covering 300 trading days) to give the regime classifier its required
  warmup window.
- VIX daily: FRED ``VIXCLS`` via ``data.fred_vix.get_vix_history``,
  wrapped in ``_load_vix_daily`` for best-effort failure handling.
  Polygon Stocks Starter does not include indices (I:VIX returns 403,
  verified 2026-05-13); FRED publishes the same daily close one
  business day later, free, with a stable 30-year API contract.

Status: M2.2 sub-task #3 — ``load_market_data`` fully wired.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from data import fred_vix, polygon_feed

logger = logging.getLogger(__name__)


# 300 trading days × ~1.5 calendar/trading-day ratio + holiday slack.
# Empirically, 460 calendar days back from any US-market date yields at
# least 300 trading days even with extended holiday weeks. ``fetch_aggs``
# does not pre-pad on its own — the caller (this function) extends the
# range explicitly so the contract stays "what you ask is what you get."
SPY_DAILY_PREPAD_CALENDAR_DAYS = 460


@dataclass(frozen=True, slots=True)
class MarketContextBundle:
    """SPY + VIX data bundle for one replay run.

    Loaded once at run start, sliced per-tick by the replay loop. Shape
    parallels what the regime classifier's data layer expects so the
    same DataFrames can feed both the LLMContext builder and the
    regime classifier.
    """

    spy_5min: pd.DataFrame  # 5-min bars over the replay range
    spy_daily: pd.DataFrame  # daily bars, 300+ days back from start_date
    vix_daily: pd.DataFrame | None  # None when VIX source is unavailable


async def load_market_data(
    start_date: date, end_date: date
) -> MarketContextBundle:
    """Load SPY (5-min + daily) and VIX (daily) over a replay date range.

    Async because it calls ``fetch_aggs`` and ``_load_vix_daily``, both
    async. The replay CLI lives inside an ``asyncio.run(main())`` at
    the entry point; this function is awaited once at run start.

    Range handling:
      - SPY 5-min uses ``[start_date, end_date]`` as-is.
      - SPY daily and VIX daily pre-pad by
        ``SPY_DAILY_PREPAD_CALENDAR_DAYS`` (~460 calendar days, covering
        300 trading days) so the regime classifier has its required
        SMA200-class warmup window without the caller needing to
        compute it.

    SPY required vs VIX best-effort (Rule 18):
      - SPY 5-min and SPY daily are required. Any ``fetch_aggs``
        failure propagates as ``RuntimeError``: the replay cannot
        produce meaningful market context without SPY, so a silent
        empty-DataFrame return would be a Rule 18 violation.
      - VIX is best-effort. ``_load_vix_daily`` already collapses any
        FRED-side ``RuntimeError`` to ``None`` with a WARNING log;
        the regime classifier and LLMContext both handle the None
        path (verified during regime rollout 2026-05-13).

    Args:
        start_date: inclusive, the replay window's first date.
        end_date: inclusive, the replay window's last date.

    Returns:
        ``MarketContextBundle`` with all three frames. ``vix_daily``
        is ``None`` only when the FRED fetch failed; ``spy_5min`` and
        ``spy_daily`` are always non-empty DataFrames (failure raises).

    Raises:
        ValueError: ``end_date < start_date``. Caller bug; would
            propagate the same way from ``fetch_aggs`` but we catch
            it up-front so the error message names the wrong field.
        RuntimeError: SPY 5-min or SPY daily fetch failed (bad key,
            ticker, range, retries exhausted, empty results,
            truncation). Replay aborts here rather than continuing
            with partial market context.
    """
    if end_date < start_date:
        raise ValueError(
            f"end_date {end_date} is before start_date {start_date}"
        )

    daily_start = start_date - timedelta(days=SPY_DAILY_PREPAD_CALENDAR_DAYS)

    logger.info(
        "Loading replay market context: SPY 5-min %s..%s, "
        "SPY daily %s..%s (300-day prepad), VIX daily %s..%s",
        start_date, end_date,
        daily_start, end_date,
        daily_start, end_date,
    )

    spy_5min = await polygon_feed.fetch_aggs(
        "SPY", 5, "minute", start_date, end_date
    )
    spy_daily = await polygon_feed.fetch_aggs(
        "SPY", 1, "day", daily_start, end_date
    )
    vix_daily = await _load_vix_daily(daily_start, end_date)

    logger.info(
        "Replay market context loaded: SPY 5-min rows=%d, "
        "SPY daily rows=%d, VIX daily rows=%s",
        len(spy_5min),
        len(spy_daily),
        len(vix_daily) if vix_daily is not None else "none (degraded)",
    )

    return MarketContextBundle(
        spy_5min=spy_5min,
        spy_daily=spy_daily,
        vix_daily=vix_daily,
    )


async def _load_vix_daily(
    start_date: date, end_date: date
) -> pd.DataFrame | None:
    """Best-effort VIX daily fetch over ``[start_date, end_date]`` inclusive.

    Wraps ``data.fred_vix.get_vix_history``. Per the ``load_market_data``
    contract, VIX is best-effort: any upstream FRED failure (missing key,
    4xx, retries exhausted, empty observations, all-sentinels) collapses
    to ``None`` with a WARNING log. Both the regime classifier and the
    LLMContext builder already handle ``vix_daily=None`` paths (verified
    during the regime classifier rollout, 2026-05-13).

    A ``ValueError`` from the underlying call (programming error, e.g.
    ``end_date < start_date``) is re-raised unchanged — that's a caller
    bug, not a best-effort failure mode, and silently returning None
    would hide it (Rule 18).

    Unexpected exception types also propagate; the only swallowed class
    is ``RuntimeError`` from ``fred_vix``, whose contract guarantees
    that path covers every legitimate FRED-side failure.

    Caller responsibilities:
      - Pre-pad ``start_date`` for whatever warmup window downstream
        consumers require (the regime classifier wants ~60 trading days
        of VIX history to compute its 60-day median).
      - Caller is sync or async; this helper is async, so a sync caller
        wraps it in ``asyncio.run`` once at the top of the replay load.

    Rule 22 note: ``fred_vix``'s ``RuntimeError`` messages are already
    scrubbed of the ``api_key=`` URL value before they reach this
    handler, so logging ``str(e)`` directly is safe. We rely on that
    contract rather than re-scrubbing here.

    Args:
        start_date: inclusive
        end_date: inclusive

    Returns:
        ``DataFrame`` indexed by tz-aware UTC midnight date with a single
        column ``vix_close`` (float), sorted ascending — i.e. exactly
        the shape ``fred_vix.get_vix_history`` returns. ``None`` if the
        FRED fetch failed.
    """
    try:
        return await fred_vix.get_vix_history(start_date, end_date)
    except RuntimeError as e:
        logger.warning(
            "VIX load failed; market_context.vix_daily=None: %s", e
        )
        return None
