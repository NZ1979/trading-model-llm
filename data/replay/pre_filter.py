"""Per-tick candidate pre-filter for the M2 replay harness.

Cheap rule-based narrowing of the ticker universe BEFORE LLM
evaluation. The four-gate OR per ``docs/M2_REPLAY_HARNESS_DESIGN.md``
§ Pre-filter (lines 175-191):

  - ``pm_rvol >= config.pre_filter_min_pm_rvol``
  - ``abs(gap_pct) >= config.pre_filter_min_gap_pct``
  - news visible at ``tick_et`` within
    ``config.pre_filter_news_lookback_hours`` (after the
    ``config.news_lag_seconds`` ingestion buffer)
  - ``ticker in currently_holding``

Any one is sufficient. The full surviving set is then capped at
``config.max_candidates_per_tick`` (default 30); the cap is
deterministic given the input dict's iteration order, which Python
3.7+ guarantees as insertion order.

Why pre-filter at all:

The base codebase's signal engine evaluates every watchlist ticker on
every tick. The replay harness keeps that for the base portfolio (for
a fair comparison) but gates the LLM path through this pre-filter so a
~6-hour Qwen replay across a 50-ticker watchlist doesn't pay the LLM
cost on tickers that obviously aren't candidates. The design doc notes
that "miss for LLM, hit for base" cases (where the pre-filter rejects
a ticker the base strategy fired on) are tracked separately in the
comparison report.

This module is pure: no async, no I/O, no LLM. Reuses
``filter_visible_at`` from sub-task #6 for the news gate.

Status: M2.2 sub-task #11 -- fully implemented.
"""
from __future__ import annotations

from datetime import datetime

from data.replay.config import ReplayConfig
from data.replay.day_state import DayState
from data.replay.historical_news import filter_visible_at


__all__ = ["pre_filter_candidates"]


def pre_filter_candidates(
    day_state: DayState,
    tick_et: datetime,
    currently_holding: set[str],
    config: ReplayConfig,
) -> tuple[str, ...]:
    """Return the tickers that survive the pre-filter at ``tick_et``.

    Iterates ``day_state.tickers`` in dict-insertion order. For each
    ticker, evaluates four gates with early-exit on the first match
    (the gate that caused the pass is not surfaced -- the tick loop
    only needs the surviving set). Output is capped at
    ``config.max_candidates_per_tick``.

    Args:
        day_state: per-day bundle from
            ``data.replay.day_state.build_day_state``. Only tickers in
            ``day_state.tickers`` are considered; ``failed_tickers``
            are silently ignored.
        tick_et: current replay tick timestamp. tz-aware
            America/New_York. Must be tz-aware -- naive timestamps
            raise ``ValueError`` (the news gate's
            ``filter_visible_at`` requires tz-aware).
        currently_holding: set of tickers currently held in the LLM
            portfolio. Entries for tickers that aren't in
            ``day_state.tickers`` are silently ignored (we iterate
            day_state, not the holding set).
        config: full ``ReplayConfig``. Read for the four threshold
            knobs, ``news_lag_seconds``, and
            ``max_candidates_per_tick``.

    Returns:
        ``tuple[str, ...]`` of surviving tickers in dict-insertion
        order, truncated at ``config.max_candidates_per_tick``.

    Raises:
        ValueError: ``tick_et`` is naive.
    """
    if tick_et.tzinfo is None:
        raise ValueError(
            "pre_filter_candidates requires tz-aware tick_et; got naive"
        )

    candidates: list[str] = []
    max_candidates = config.max_candidates_per_tick

    for ticker, tds in day_state.tickers.items():
        # Gate 1: premarket RVOL. PremarketContext None defaults
        # pm_rvol to 0.0, which fails this gate (any sane threshold is
        # > 0.0). The default is the right "no information" semantics
        # -- the news / holding gates can still rescue the ticker.
        pm_rvol = (
            float(tds.premarket_context.premarket_rvol)
            if tds.premarket_context is not None
            else 0.0
        )
        if pm_rvol >= config.pre_filter_min_pm_rvol:
            candidates.append(ticker)
            if len(candidates) >= max_candidates:
                break
            continue

        # Gate 2: |gap_pct|. Negative gaps qualify just like positive
        # ones (a -3% gap is as actionable as +3%). PremarketContext
        # None defaults to 0.0 and fails.
        gap_pct = (
            float(tds.premarket_context.gap_pct)
            if tds.premarket_context is not None
            else 0.0
        )
        if abs(gap_pct) >= config.pre_filter_min_gap_pct:
            candidates.append(ticker)
            if len(candidates) >= max_candidates:
                break
            continue

        # Gate 3: recent news visible at the tick. Reuse
        # filter_visible_at (sub-task #6 pure helper) with the
        # pre-filter's lookback hours (config.pre_filter_news_lookback_hours,
        # default 2) and the standard news_lag_seconds buffer (default
        # 30s). Note this is a DIFFERENT lookback window than the LLM
        # context's 24h news window -- the pre-filter only cares about
        # recent catalysts, not the full day's news.
        visible_news = filter_visible_at(
            tds.news_items,
            as_of_et=tick_et,
            lookback_hours=config.pre_filter_news_lookback_hours,
            lag_seconds=config.news_lag_seconds,
        )
        if visible_news:
            candidates.append(ticker)
            if len(candidates) >= max_candidates:
                break
            continue

        # Gate 4: currently holding. Always re-evaluate held positions
        # even if they wouldn't pass the catalyst gates -- the LLM
        # needs the chance to issue an exit decision.
        if ticker in currently_holding:
            candidates.append(ticker)
            if len(candidates) >= max_candidates:
                break
            continue

    return tuple(candidates)
