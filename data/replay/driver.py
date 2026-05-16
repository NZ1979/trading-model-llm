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
import time
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

from data.replay.config import ReplayConfig
from data.replay.day_state import build_day_state
from data.replay.fill_simulator import (
    DayApplicationResult,
    EodExit,
    RejectedEntry,
    StopOut,
    apply_day_to_portfolio,
)
from data.replay.market_context import load_market_data
from data.replay.persistence import write_day_results
from data.replay.t3_budget import T3Budget
from data.replay.tick_loop import (
    TickDecision,
    run_day_base_ticks,
    run_day_t3_ticks,
    run_day_ticks,
)
from data.watchlist_builder import read_watchlist_file
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
    # Tier 3 (Opus) labeling pass (M2.2 sub-task #20). Populated only
    # when run_replay is called with clients.t3 not None AND a
    # t3_budget. Empty list otherwise. T3 does NOT drive a portfolio
    # (no fills, no equity curve) -- it's pure labeling for the
    # comparison report's section 5d agreement analysis.
    t3_decisions: list[TickDecision] = field(default_factory=list)
    # Base-strategy parallel evaluation (M2.2 sub-task #17). All six
    # base_* fields default to empty: when run_replay is called without
    # base_portfolio, the base pass is skipped entirely and these stay
    # empty -- backward-compatible with every test that doesn't wire
    # a base portfolio.
    base_decisions: list[TickDecision] = field(default_factory=list)
    base_fills: tuple[SimulatedFill, ...] = ()
    base_rejections: tuple[RejectedEntry, ...] = ()
    base_stop_outs: tuple[StopOut, ...] = ()
    base_eod_exits: tuple[EodExit, ...] = ()
    base_equity_curve: tuple[EquityPoint, ...] = ()
    # Per-phase wall-clock timings in milliseconds (M2.2 sub-task #23).
    # Populated on the success path only; skipped days leave it None.
    # Keys: data_prep, tick_loop, fill_sim_llm, t3_labeling, base_pass.
    # Phases that did not run (e.g. t3_labeling when T3 disabled) are
    # omitted from the dict rather than recorded as 0 -- absence is the
    # signal so the summary builder can count n_days_with_phase
    # correctly.
    phase_durations_ms: dict[str, float] | None = None


# ---------------------------------------------------------------------------
# Trading-day enumeration
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _nyse_calendar() -> mcal.MarketCalendar:
    """NYSE trading calendar, cached at module level.

    ``mcal.get_calendar("NYSE")`` constructs an internal holiday
    calendar over a range of years; cache it so multi-day replays
    don't rebuild it on every ``_trading_days`` call. The
    ``maxsize=1`` cache turns the function into a memoized singleton
    while keeping it test-friendly (``_nyse_calendar.cache_clear()``
    available if a test ever needs to swap exchanges).
    """
    return mcal.get_calendar("NYSE")


def _trading_days(start: date, end: date) -> list[date]:
    """Return NYSE trading days in ``[start, end]`` inclusive.

    Filters weekends AND US market holidays (Thanksgiving, Christmas,
    Juneteenth, etc.) via ``pandas_market_calendars``. Promoted from
    the pre-#24 ``pd.bdate_range`` weekday-only approximation:
    holidays previously survived to ``build_day_state`` where Polygon
    returned empty bars and the per-day try/except converted them to
    skipped DayRunResults. With the calendar in place, holidays
    never enter the loop in the first place.

    Special cases:

    - ``end < start`` -> empty list (unchanged from the old impl).
    - Half-day sessions (day-after-Thanksgiving, Christmas Eve when
      it's a weekday): included. The NYSE calendar treats them as
      trading days regardless of the 1pm close; downstream the bar
      feed naturally returns fewer bars, and EOD-flatten at 15:55
      remains the universal flatten point.
    - Range entirely within holidays / weekends: empty list, which
      ``run_replay``'s ``for trading_date in days:`` loop handles
      cleanly with no iterations and no output rows.
    """
    if end < start:
        return []
    sched = _nyse_calendar().schedule(start_date=start, end_date=end)
    return [ts.date() for ts in sched.index]


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


async def run_replay(
    *,
    config: ReplayConfig,
    clients: TierClients,
    sentiment_conn: sqlite3.Connection,
    portfolio: SimulatedPortfolio | None = None,
    base_portfolio: SimulatedPortfolio | None = None,
    persistence_conn: sqlite3.Connection | None = None,
    run_id: int | None = None,
    t3_budget: T3Budget | None = None,
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
        RuntimeError: ``load_market_data`` failed (SPY required;
            replay unrunnable without it), or
            ``config.tickers == "watchlist"`` but
            ``read_watchlist_file(config.watchlist_path)`` returned
            ``None`` (missing / malformed / empty watchlist fixture).
        ValueError: ``persistence_conn`` and ``run_id`` not paired
            (exactly one is None). They must both be passed together
            for persistence to be enabled, or both omitted to disable
            it.
    """
    # Resolve tickers (M2.2 sub-task #25). The "watchlist" literal
    # reads from a JSON fixture written by main.py's daily refresh;
    # the staleness check is bypassed because replay runs against
    # historical bars and the file's recency has no semantic meaning
    # here. See ReplayConfig.watchlist_path for the file location.
    if config.tickers == "watchlist":
        resolved = read_watchlist_file(
            config.watchlist_path, max_age_days=365 * 100,
        )
        if not resolved:
            raise RuntimeError(
                "run_replay: ReplayConfig.tickers='watchlist' but no "
                f"valid watchlist found at {config.watchlist_path}. "
                "Either run the watchlist refresh job to (re)generate "
                "that file or pass an explicit ticker tuple via "
                "ReplayConfig.tickers."
            )
        tickers: tuple[str, ...] = tuple(resolved)
    else:
        tickers = config.tickers_tuple

    if (persistence_conn is None) != (run_id is None):
        raise ValueError(
            "run_replay: persistence_conn and run_id must be paired "
            "(both None to disable persistence, or both set to enable). "
            f"got persistence_conn={'set' if persistence_conn else 'None'}, "
            f"run_id={run_id}"
        )
    persistence_enabled = persistence_conn is not None and run_id is not None

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

    def _persist(
        day_result: DayRunResult, *, regime: str | None = None,
    ) -> None:
        """Write one day's rows when persistence is enabled.

        No-ops when persistence is disabled OR when the day was skipped
        (write_day_results itself short-circuits on skipped days).
        Fills are written from ``portfolio.closed_positions``; without
        a portfolio there is no fill data to persist, so persistence
        only fires when both ``persistence_enabled`` and ``portfolio``
        are set. When ``base_portfolio`` is also set, base-side
        decisions / fills / equity points are written in the same
        transaction with ``decision_source='base'`` and
        ``portfolio_name='base'``.

        ``regime`` is ``day_state.market_regime_label`` (per M2.2
        sub-task #22). Skipped-day calls before ``day_state`` exists
        pass ``regime=None``; ``write_day_results`` no-ops on skipped
        days regardless, so the value is informational only on that
        path.
        """
        if not persistence_enabled or portfolio is None:
            return
        write_day_results(
            persistence_conn,  # type: ignore[arg-type]  # narrowed by persistence_enabled
            run_id=run_id,  # type: ignore[arg-type]
            day_result=day_result,
            llm_portfolio=portfolio,
            base_portfolio=base_portfolio,
            regime=regime,
        )

    results: list[DayRunResult] = []
    for trading_date in days:
        budget.reset()
        # Per-phase wall-clock timings (M2.2 sub-task #23). Phases that
        # don't run (T3 disabled, base disabled, etc.) are omitted; the
        # absence is the signal for the summary builder.
        phase_ms: dict[str, float] = {}

        # Per-day data prep. Loud failure here means the day cannot
        # produce meaningful LLMContexts; skip and continue.
        _t0 = time.monotonic()
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
        phase_ms["data_prep"] = (time.monotonic() - _t0) * 1000.0

        # Per-day tick loop. run_day_ticks shouldn't raise per #12's
        # contract, but defensively wrap.
        _t0 = time.monotonic()
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
            _persist(skipped_result, regime=day_state.market_regime_label)
            continue
        phase_ms["tick_loop"] = (time.monotonic() - _t0) * 1000.0

        # LLM-side fill + stop + MTM + EOD-flatten simulation when the
        # caller supplied a SimulatedPortfolio. With no portfolio (the
        # M2.2 #13 default pattern), all five LLM-side tuples remain
        # empty.
        if portfolio is not None:
            _t0 = time.monotonic()
            llm_day_app = apply_day_to_portfolio(
                decisions=decisions,
                day_state=day_state,
                portfolio=portfolio,
                config=config,
            )
            phase_ms["fill_sim_llm"] = (time.monotonic() - _t0) * 1000.0
            fills_t = llm_day_app.fills
            rejections_t = llm_day_app.rejections
            stop_outs_t = llm_day_app.stop_outs
            eod_exits_t = llm_day_app.eod_exits
            equity_curve_t = llm_day_app.equity_curve
        else:
            fills_t = ()
            rejections_t = ()
            stop_outs_t = ()
            eod_exits_t = ()
            equity_curve_t = ()

        # Tier 3 (Opus) labeling pass (M2.2 sub-task #20). Runs only
        # when clients.t3 is supplied AND t3_budget is supplied;
        # produces 't3_only' rows for the comparison report's § 5d
        # agreement analysis. T3 does NOT drive a portfolio -- it's
        # pure labeling. Same LLMContext the live T1+T2 path saw, so
        # T1 ↔ T3 agreement metrics are apples-to-apples.
        if clients.t3 is not None and t3_budget is not None:
            _t0 = time.monotonic()
            t3_decisions = await run_day_t3_ticks(
                day_state=day_state,
                market_ctx=market_ctx,
                config=config,
                clients=clients,
                budget=t3_budget,
                portfolio=portfolio,
            )
            phase_ms["t3_labeling"] = (time.monotonic() - _t0) * 1000.0
        else:
            t3_decisions = []

        # Base-strategy parallel evaluation (M2.2 sub-task #17). Runs
        # only when base_portfolio is set; emits its own decisions via
        # run_day_base_ticks (no pre-filter, every ticker every tick),
        # then drives the SAME apply_day_to_portfolio against the base
        # portfolio so stops + EOD flatten + MTM apply symmetrically.
        if base_portfolio is not None:
            _t0 = time.monotonic()
            base_decisions = run_day_base_ticks(
                day_state=day_state,
                config=config,
                sentiment_conn=sentiment_conn,
                portfolio=base_portfolio,
            )
            base_day_app: DayApplicationResult = apply_day_to_portfolio(
                decisions=base_decisions,
                day_state=day_state,
                portfolio=base_portfolio,
                config=config,
            )
            phase_ms["base_pass"] = (time.monotonic() - _t0) * 1000.0
            base_fills_t = base_day_app.fills
            base_rejections_t = base_day_app.rejections
            base_stop_outs_t = base_day_app.stop_outs
            base_eod_exits_t = base_day_app.eod_exits
            base_equity_curve_t = base_day_app.equity_curve
        else:
            base_decisions = []
            base_fills_t = ()
            base_rejections_t = ()
            base_stop_outs_t = ()
            base_eod_exits_t = ()
            base_equity_curve_t = ()

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
            t3_decisions=t3_decisions,
            base_decisions=base_decisions,
            base_fills=base_fills_t,
            base_rejections=base_rejections_t,
            base_stop_outs=base_stop_outs_t,
            base_eod_exits=base_eod_exits_t,
            base_equity_curve=base_equity_curve_t,
            phase_durations_ms=phase_ms,
        )
        results.append(day_result)
        _persist(day_result, regime=day_state.market_regime_label)
        logger.info(
            "run_replay: %s complete, %d llm decisions / %d base decisions / "
            "%d t3 decisions, %d failed ticker(s), %d escalation(s), "
            "%d llm fills (%d stops, %d eod) / %d base fills (%d stops, %d eod), "
            "%d llm rejections / %d base rejections, "
            "%d llm equity pts / %d base equity pts",
            trading_date,
            len(decisions), len(base_decisions), len(t3_decisions),
            len(day_state.failed_tickers), budget.used,
            len(fills_t), len(stop_outs_t), len(eod_exits_t),
            len(base_fills_t), len(base_stop_outs_t), len(base_eod_exits_t),
            len(rejections_t), len(base_rejections_t),
            len(equity_curve_t), len(base_equity_curve_t),
        )

    return results


__all__ = ["DayRunResult", "run_replay"]
