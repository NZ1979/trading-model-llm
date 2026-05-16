"""Multi-day driver for the M2 replay harness.

``run_replay`` is the outer-loop function the CLI calls. Given a
fully-constructed ``ReplayConfig``, wrapped tier clients
(typically ``CachedLLMClient`` instances from #8), and an opened
sentiment fixture connection, it loads the run-level
``MarketContextBundle`` once at run start, iterates trading days
in ``[config.start_date, config.end_date]`` (weekdays only), calls
``build_day_state`` then ``run_day_ticks`` per day, and returns one
``DayRunResult`` per processed day.

Scope boundaries (M2.2 sub-task #13):

  - Watchlist literal ``"watchlist"``: raises ``NotImplementedError``.
    Watchlist resolution via ``data/watchlist_builder.py`` is its own
    sub-task; until then, callers pass an explicit ticker tuple
    through ``ReplayConfig.tickers``.
  - Holiday calendar: weekday filter only via ``pandas.bdate_range``.
    US trading holidays (Christmas, Thanksgiving, etc.) survive the
    weekday filter and then fail ``build_day_state`` because Polygon
    returns empty bars; the per-day try/except converts them to
    ``DayRunResult(skipped=True, skip_reason="...")``. A proper
    holiday calendar is a small follow-up sub-task. Documenting the
    approximation in every report header is the design-doc
    convention.
  - Fill simulation, Tier 3 (Opus) labeling, base-strategy parallel
    evaluation, ``replay_results.db`` persistence: all explicitly
    out of scope -- their own sub-tasks. The ``portfolio`` arg is
    read for the pre-filter holding gate and the per-ticker
    position dict, never mutated here.

Failure semantics:

  - ``load_market_data`` failure at run start propagates loud. Without
    SPY the replay is unrunnable; partial output would mislead the
    comparison report.
  - Per-day ``build_day_state`` ``RuntimeError`` (all tickers failed,
    news batch failed, global outage): catch, log WARNING, append a
    skipped ``DayRunResult`` with the failure mode in
    ``skip_reason``, continue. Lets a 30-day replay survive a single
    bad day (typically a US holiday landing on a non-Sat/Sun).
  - Per-day ``run_day_ticks`` raising: shouldn't happen per #12's
    contract, but defensively wrapped in try/except and converted to
    skipped + continue. Programmer bugs surface in the skip_reason
    rather than aborting the whole replay.

Sentiment connection lifecycle: caller owns. ``run_replay`` receives
the open ``sqlite3.Connection`` and passes it through to each day's
``build_day_state``. The driver does NOT close it.

``EscalationBudget`` lifecycle: ``run_replay`` constructs one
``EscalationBudget(max_per_day=config.t2_max_per_day)`` at run start
and calls ``.reset()`` at the top of each day. Callers do not pass a
budget in -- one less thing to thread through the CLI.

Status: M2.2 sub-task #13 -- fully implemented.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from data.replay.config import ReplayConfig
from data.replay.day_state import build_day_state
from data.replay.fill_simulator import (
    EodExit,
    RejectedEntry,
    StopOut,
    apply_day_to_portfolio,
)
from data.replay.market_context import load_market_data
from data.replay.persistence import write_day_results
from data.replay.tick_loop import TickDecision, run_day_ticks
from sim.fills import SimulatedFill
from sim.portfolio import EquityPoint, SimulatedPortfolio
from strategy.llm.escalation import EscalationBudget
from strategy.llm.signal_engine import TierClients

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayRunResult:
    """Per-day rollup the comparison-report generator consumes.

    ``decisions`` is empty for days that were skipped or for quiet
    days where no ticker passed the pre-filter at any tick.
    ``failed_tickers`` is forwarded from ``DayState`` and lists
    tickers dropped during data prep (delisted, halted, zero-volume).
    ``t2_escalations_used`` is ``EscalationBudget.used`` at end of
    day -- 0 for skipped days (the budget was reset, no calls
    landed).

    ``skipped`` distinguishes "the day's data was unloadable" from
    "the day ran but produced zero decisions" -- both yield empty
    ``decisions`` but only the former should count against
    coverage in the report.

    ``fills``, ``rejections``, ``stop_outs``, ``eod_exits``, and
    ``equity_curve`` are populated when the caller passes a
    SimulatedPortfolio AND the day ran successfully. When ``portfolio``
    is None at the run_replay call (the M2.2 sub-task #13 default
    pattern, kept for backward compatibility), all five tuples are
    empty and no portfolio mutation happens.

    Field meanings (all chronological by emission order):

    - ``fills``: successful Buy/Sell entries. Flip-exits are visible
      on ``portfolio.closed_positions`` with ``exit_reason="flip"``;
      they are NOT first-class fills here.
    - ``rejections``: Buy/Sell decisions that did not transact
      (risk-gate rejection, no next bar at the last tick, missing
      5-min bar data, etc.) -- structured output rather than logged
      counts, so the report can break down "decisions that didn't
      transact" by reason without re-parsing logs.
    - ``stop_outs`` (sub-task #15): positions closed by
      ``portfolio.check_stops`` during the per-bar pass, with
      ``exit_reason="stop_hit"`` on the closed position.
    - ``eod_exits`` (sub-task #15): positions closed by the end-of-day
      flatten pass at 15:55 ET, with ``exit_reason="eod_flatten"``.
    - ``equity_curve`` (sub-task #15): EquityPoints emitted during
      the day. Typically 78 from the per-bar MTM, plus a final
      post-flatten point. May contain two points at 15:55 (pre- and
      post-flatten) when positions were open going into the final
      bar; the report can dedup or use both.
    """

    trading_date: date
    decisions: list[TickDecision] = field(default_factory=list)
    failed_tickers: dict[str, str] = field(default_factory=dict)
    t2_escalations_used: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    fills: tuple[SimulatedFill, ...] = ()
    rejections: tuple[RejectedEntry, ...] = ()
    stop_outs: tuple[StopOut, ...] = ()
    eod_exits: tuple[EodExit, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()


# ---------------------------------------------------------------------------
# Trading-day enumeration
# ---------------------------------------------------------------------------


def _trading_days(start: date, end: date) -> list[date]:
    """Return weekdays in ``[start, end]`` inclusive.

    Uses ``pandas.bdate_range`` which skips Saturday and Sunday. US
    trading holidays survive this filter and are caught at the
    per-day try/except layer when bars come back empty. A proper
    holiday calendar is a follow-up sub-task.
    """
    if end < start:
        return []
    return [
        ts.date()
        for ts in pd.bdate_range(start=start, end=end)
    ]


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


async def run_replay(
    *,
    config: ReplayConfig,
    clients: TierClients,
    sentiment_conn: sqlite3.Connection,
    portfolio: SimulatedPortfolio | None = None,
    persistence_conn: sqlite3.Connection | None = None,
    run_id: int | None = None,
) -> list[DayRunResult]:
    """Run the LLM-path replay over ``[config.start_date, config.end_date]``.

    Args:
        config: full ReplayConfig. The driver reads ``start_date``,
            ``end_date``, ``tickers`` (must NOT be the "watchlist"
            literal -- see Raises), ``t2_max_per_day``, plus the
            sub-knobs the downstream functions consume.
        clients: TierClients with at minimum a t1 client. The caller
            is expected to have wrapped both t1 and t2 in
            CachedLLMClient with the run's cache_dir before
            constructing this object so cache hits accrue across
            days.
        sentiment_conn: opened via
            ``data.replay.historical_sentiment.open_fixture``. Caller
            owns lifecycle -- driver does not close.
        portfolio: optional SimulatedPortfolio passed through to
            each day's ``run_day_ticks``. Read only; the driver does
            not mutate it. In M2.2 sub-task #13 (no fill simulation
            wired yet) this is typically None or a freshly-constructed
            empty portfolio.
        persistence_conn: optional opened ``replay_results.db``
            connection from ``data.replay.persistence.init_replay_db``.
            When set, ``write_day_results`` is invoked after each
            non-skipped day's result lands. Must be paired with
            ``run_id`` (both None or both set); passing only one is a
            programmer bug -- raises ``ValueError``. Caller owns
            lifecycle (open via init_replay_db; close after).
        run_id: optional run_id from
            ``data.replay.persistence.start_run``. Paired with
            ``persistence_conn``.

    Returns:
        ``list[DayRunResult]`` ordered by ``trading_date`` ascending.
        Includes skipped days so the report can surface coverage
        gaps.

    Raises:
        NotImplementedError: ``config.tickers == "watchlist"``.
            Resolve via watchlist_builder before constructing the
            config (separate sub-task).
        RuntimeError: ``load_market_data`` failed (SPY required;
            replay unrunnable without it).
        ValueError: ``persistence_conn`` and ``run_id`` not paired
            (exactly one is None). They must both be passed together
            for persistence to be enabled, or both omitted to disable
            it.
    """
    if config.tickers == "watchlist":
        raise NotImplementedError(
            "run_replay: ReplayConfig.tickers='watchlist' resolution "
            "is a follow-up sub-task. Pass an explicit ticker tuple "
            "through ReplayConfig.tickers for now."
        )
    if (persistence_conn is None) != (run_id is None):
        raise ValueError(
            "run_replay: persistence_conn and run_id must be paired "
            "(both None to disable persistence, or both set to enable). "
            f"got persistence_conn={'set' if persistence_conn else 'None'}, "
            f"run_id={run_id}"
        )
    persistence_enabled = persistence_conn is not None and run_id is not None

    tickers = config.tickers_tuple
    days = _trading_days(config.start_date, config.end_date)

    logger.info(
        "run_replay: %d trading day(s) in [%s, %s], %d ticker(s)",
        len(days), config.start_date, config.end_date, len(tickers),
    )

    # SPY/VIX once at run start. Loud failure -- the whole replay
    # depends on this context.
    market_ctx = await load_market_data(config.start_date, config.end_date)

    # One EscalationBudget across the run; reset per day.
    budget = EscalationBudget(max_per_day=config.t2_max_per_day)

    def _persist(day_result: DayRunResult) -> None:
        """Write one day's rows when persistence is enabled.

        No-ops when persistence is disabled OR when the day was skipped
        (write_day_results itself short-circuits on skipped days).
        Fills are written from ``portfolio.closed_positions``; without
        a portfolio there is no fill data to persist, so persistence
        only fires when both ``persistence_enabled`` and ``portfolio``
        are set.
        """
        if not persistence_enabled or portfolio is None:
            return
        write_day_results(
            persistence_conn,  # type: ignore[arg-type]  # narrowed by persistence_enabled
            run_id=run_id,  # type: ignore[arg-type]
            day_result=day_result,
            llm_portfolio=portfolio,
        )

    results: list[DayRunResult] = []
    for trading_date in days:
        budget.reset()

        # Per-day data prep. Loud failure here means the day cannot
        # produce meaningful LLMContexts; skip and continue.
        try:
            day_state = await build_day_state(
                config=config,
                trading_date=trading_date,
                tickers=tickers,
                market_ctx=market_ctx,
                sentiment_conn=sentiment_conn,
            )
        except Exception as exc:  # broad: capture build_day_state's loud paths
            reason = f"build_day_state failed: {type(exc).__name__}: {exc}"
            logger.warning(
                "run_replay: skipping %s -- %s", trading_date, reason
            )
            skipped_result = DayRunResult(
                trading_date=trading_date,
                skipped=True,
                skip_reason=reason,
            )
            results.append(skipped_result)
            _persist(skipped_result)
            continue

        # Per-day tick loop. run_day_ticks shouldn't raise per #12's
        # contract, but defensively wrap.
        try:
            decisions = await run_day_ticks(
                day_state=day_state,
                market_ctx=market_ctx,
                config=config,
                clients=clients,
                budget=budget,
                portfolio=portfolio,
            )
        except Exception as exc:
            reason = f"run_day_ticks failed: {type(exc).__name__}: {exc}"
            logger.warning(
                "run_replay: skipping %s -- %s", trading_date, reason
            )
            skipped_result = DayRunResult(
                trading_date=trading_date,
                failed_tickers=dict(day_state.failed_tickers),
                skipped=True,
                skip_reason=reason,
            )
            results.append(skipped_result)
            _persist(skipped_result)
            continue

        # Fill + stop + MTM + EOD-flatten simulation when the caller
        # supplied a SimulatedPortfolio. With no portfolio (the M2.2
        # #13 default pattern), all five new tuples remain empty --
        # backward-compatible with tests that don't yet wire a
        # portfolio.
        if portfolio is not None:
            day_result = apply_day_to_portfolio(
                decisions=decisions,
                day_state=day_state,
                portfolio=portfolio,
                config=config,
            )
            fills_t = day_result.fills
            rejections_t = day_result.rejections
            stop_outs_t = day_result.stop_outs
            eod_exits_t = day_result.eod_exits
            equity_curve_t = day_result.equity_curve
        else:
            fills_t = ()
            rejections_t = ()
            stop_outs_t = ()
            eod_exits_t = ()
            equity_curve_t = ()

        day_result = DayRunResult(
            trading_date=trading_date,
            decisions=decisions,
            failed_tickers=dict(day_state.failed_tickers),
            t2_escalations_used=budget.used,
            skipped=False,
            skip_reason=None,
            fills=fills_t,
            rejections=rejections_t,
            stop_outs=stop_outs_t,
            eod_exits=eod_exits_t,
            equity_curve=equity_curve_t,
        )
        results.append(day_result)
        _persist(day_result)
        logger.info(
            "run_replay: %s complete, %d decisions, %d failed ticker(s), "
            "%d escalation(s) used, %d fill(s), %d rejection(s), "
            "%d stop-out(s), %d eod exit(s), %d equity point(s)",
            trading_date, len(decisions), len(day_state.failed_tickers),
            budget.used, len(fills_t), len(rejections_t),
            len(stop_outs_t), len(eod_exits_t), len(equity_curve_t),
        )

    return results


__all__ = ["DayRunResult", "run_replay"]
