"""Single-day tick loop for the M2 replay harness.

``run_day_ticks`` is the per-day driver that strings the loaders +
context builders + signal engine together into the actual replay
flow. Given a fully-prepared ``DayState`` (sub-task #9), the run-level
``MarketContextBundle`` (#1-#3), the wrapped tier clients (typically
``CachedLLMClient`` wrappers from #8), and an
``EscalationBudget``, it iterates the 78 ticks at 5-min cadence,
calls ``pre_filter_candidates`` (#11), builds an ``LLMContext`` for
each survivor via ``build_tick_context`` (#10), calls
``signal_engine.evaluate`` (the shared T1+T2 orchestrator), and
collects the results into a ``list[TickDecision]``.

Explicitly out of scope for this sub-task:

  - Tier 3 (Opus) labeling. T3 is replay-only and runs against the
    same context with its own budget + sample-rate gate; distinct
    flow. ``clients.t3`` is not read here.
  - Fill simulation. The function accepts an optional
    ``SimulatedPortfolio`` so it can read currently-open positions
    for the pre-filter holding gate AND the per-ticker LLMContext
    position dict, but it does NOT call ``record_entry`` /
    ``record_exit`` / ``check_stops`` / ``mark_to_market``. Fill
    wiring lands in a later sub-task.
  - Base-strategy parallel evaluation. The design doc calls for the
    base rule-based strategy to run on every ticker every tick (no
    pre-filter) for the fair-comparison report. That's a separate
    pass over the same ticks; a later sub-task.
  - Persistence. The result is an in-memory list. The
    ``replay_results.db`` writer is its own sub-task.
  - Within-tick parallelism. Sequential within-tick keeps the
    ``EscalationBudget`` consumption ordering simple (the budget is
    consumed on ATTEMPT, not on success; with concurrent attempts
    multiple coroutines can race past the cap). On a 30-day x
    50-ticker replay this is the painful path on first run (~hours)
    but cache hits on re-runs collapse to milliseconds. A future
    sub-task may parallelize within-tick via ``asyncio.gather`` if
    first-run cost justifies the synchronization overhead.

Failure semantics:

  ``signal_engine.evaluate`` never raises (every error mode maps to
  a synthetic Hold with ``tier_provenance="t1_failed"`` and the
  failure mode encoded in ``setup_label``). ``build_tick_context``
  only raises ``KeyError`` on missing ticker, which can't happen
  here (we only call it on ``pre_filter_candidates`` survivors,
  which come from ``day_state.tickers``). ``pre_filter_candidates``
  only raises ``ValueError`` on naive ``tick_et``, which we control
  via ``tick_times_for_day`` (always tz-aware ET). Net:
  ``run_day_ticks`` should not raise during normal operation.

Per-ticker prior-decisions history:

  ``LLMContext.todays_prior_decisions`` is shaped as a tuple of
  ``{ts, action, setup_label, confidence}`` dicts, capped at the
  last 5 evaluations for THIS ticker on THIS day. The loop
  accumulates these across ticks via a per-ticker ``list[dict]``
  and feeds the tail-5 into the next tick's context. The history
  resets per day (caller constructs a fresh ``DayState`` per day;
  the loop's history dict is local).

Status: M2.2 sub-task #12 -- fully implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from data.replay.config import ReplayConfig
from data.replay.day_state import DayState
from data.replay.market_context import MarketContextBundle
from data.replay.pre_filter import pre_filter_candidates
from data.replay.tick_context import build_tick_context
from sim.portfolio import SimulatedPortfolio
from strategy.llm.escalation import EscalationBudget
from strategy.llm.signal_engine import TierClients, evaluate
from strategy.llm.types import LLMDecision


ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Tick clock
# ---------------------------------------------------------------------------

# RTH session: 09:30 -> 16:00 ET. Evaluation ticks at 5-min intervals
# starting at 09:30 produce 78 ticks per trading day with the last at
# 15:55 (16:00 itself is the close, not an eval tick).
TICKS_PER_DAY = 78
TICK_INTERVAL_MINUTES = 5
OPEN_HOUR = 9
OPEN_MINUTE = 30


def tick_times_for_day(trading_date: date) -> tuple[datetime, ...]:
    """Return the 78 ET evaluation ticks for one trading day.

    Tick 0 is 09:30 ET; tick 77 is 15:55 ET. tz-aware
    America/New_York throughout, DST-correct via zoneinfo. The
    function does NOT validate that ``trading_date`` is a US trading
    day (no holiday calendar here) -- the caller picks trading days
    when iterating the replay window.
    """
    open_dt = datetime.combine(
        trading_date,
        time(OPEN_HOUR, OPEN_MINUTE),
        tzinfo=ET,
    )
    return tuple(
        open_dt + timedelta(minutes=TICK_INTERVAL_MINUTES * i)
        for i in range(TICKS_PER_DAY)
    )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickDecision:
    """One LLM evaluation result on one (ticker, tick) pair.

    The merged T1+T2 ``LLMDecision`` carries its own ``tier_provenance``
    string ("t1_only", "t1_t2_agree", "t1_t2_disagree",
    "t1_fallback_t2", "t1_only_budget_exhausted", "t1_failed"), so
    the wrapper here doesn't need to duplicate that.

    The decision's ``raw_response`` field carries the LLM's actual
    JSON tool-use output for forensic debug; signal_engine populates
    it. Cache hits via ``CachedLLMClient`` round-trip the same value.
    """

    tick_et: datetime
    ticker: str
    decision: LLMDecision


# ---------------------------------------------------------------------------
# Portfolio adapters
# ---------------------------------------------------------------------------


def _currently_holding(portfolio: SimulatedPortfolio | None) -> set[str]:
    """Return the set of tickers with currently-open positions.

    ``None`` portfolio -> empty set (no LLM-side fills wired yet).
    A portfolio object is queried for ``positions[ticker].is_open``
    because closed positions stay in ``positions`` until the closed
    list is moved.
    """
    if portfolio is None:
        return set()
    return {t for t, pos in portfolio.positions.items() if pos.is_open}


def _position_dict(
    portfolio: SimulatedPortfolio | None, ticker: str
) -> dict[str, Any] | None:
    """Build the LLMContext position dict for ``ticker``, or None when flat.

    Shape per ``strategy.llm.context_builder.build_llm_context``:
    ``{qty, avg_price, unrealized_pl_pct, has_active_stop}``.

    ``unrealized_pl_pct`` is left ``None`` until a later sub-task
    wires per-tick mark-to-market (would need the current 5-min close
    sliced consistently with build_tick_context's internal slicing).
    The LLMContext default for ``position_unrealized_pl_pct`` is also
    None, so passing None here is semantically clean.

    ``has_active_stop`` is True because SimulatedPortfolio entries
    always carry a stop_price (M2.1 scaffolding contract); the
    portfolio doesn't currently support stop-less positions.
    """
    if portfolio is None:
        return None
    pos = portfolio.get_position(ticker)
    if pos is None:
        return None
    return {
        "qty": pos.signed_qty(),
        "avg_price": pos.entry_price,
        "unrealized_pl_pct": None,  # TODO: wire MTM in fill-sim sub-task
        "has_active_stop": True,
    }


# ---------------------------------------------------------------------------
# Prior-decisions history
# ---------------------------------------------------------------------------

PRIOR_DECISIONS_TAIL = 5


def _prior_record(tick_et: datetime, decision: LLMDecision) -> dict[str, Any]:
    """Shape one prior-decision dict per LLMContext.todays_prior_decisions."""
    return {
        "ts": tick_et.isoformat(),
        "action": decision.action,
        "setup_label": decision.setup_label,
        "confidence": decision.confidence,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_day_ticks(
    *,
    day_state: DayState,
    market_ctx: MarketContextBundle,
    config: ReplayConfig,
    clients: TierClients,
    budget: EscalationBudget,
    portfolio: SimulatedPortfolio | None = None,
) -> list[TickDecision]:
    """Run the 78-tick LLM evaluation pass for one trading day.

    Sequential across ticks AND within tick. Per tick: derive
    holdings, call pre_filter_candidates, per survivor build the
    LLMContext (with rolling prior-decisions history), call
    signal_engine.evaluate, accumulate.

    Args:
        day_state: per-day bundle from
            ``data.replay.day_state.build_day_state``.
        market_ctx: run-level SPY+VIX bundle from
            ``data.replay.market_context.load_market_data``.
        config: full ReplayConfig.
        clients: TierClients with t1 (required) and optionally t2.
            t3 is ignored at this layer. The caller is expected to
            have wrapped t1 and t2 in CachedLLMClient before
            constructing this object so cache hits accrue.
        budget: EscalationBudget. The caller resets it per day; this
            function does NOT reset it (resetting here would corrupt
            cross-day state if a caller ever shared a single budget
            across days, which would be a bug -- the design says one
            budget per day, but we don't enforce that contract).
        portfolio: optional SimulatedPortfolio. Read only -- this
            function never mutates it. Used for the pre-filter
            holding gate AND the per-ticker position dict in
            LLMContext. ``None`` means "no positions known," which
            is the M2.2 sub-task #12 default since fill simulation
            isn't wired yet.

    Returns:
        ``list[TickDecision]`` in (tick_et, ticker-in-pre-filter-order)
        order. Empty when every tick's pre-filter returns no
        candidates (which can happen on a quiet trading day with no
        watchlist tickers passing thresholds).
    """
    decisions: list[TickDecision] = []
    prior_history: dict[str, list[dict[str, Any]]] = {}

    for tick_et in tick_times_for_day(day_state.trading_date):
        holding = _currently_holding(portfolio)
        candidates = pre_filter_candidates(
            day_state, tick_et, holding, config
        )
        for ticker in candidates:
            ctx = build_tick_context(
                day_state=day_state,
                market_ctx=market_ctx,
                config=config,
                ticker=ticker,
                tick_et=tick_et,
                position=_position_dict(portfolio, ticker),
                todays_prior_decisions=tuple(
                    prior_history.get(ticker, [])[-PRIOR_DECISIONS_TAIL:]
                ),
            )
            decision = await evaluate(ctx, clients, budget)
            decisions.append(
                TickDecision(
                    tick_et=tick_et,
                    ticker=ticker,
                    decision=decision,
                )
            )
            prior_history.setdefault(ticker, []).append(
                _prior_record(tick_et, decision)
            )

    return decisions


__all__ = [
    "PRIOR_DECISIONS_TAIL",
    "TICKS_PER_DAY",
    "TICK_INTERVAL_MINUTES",
    "TickDecision",
    "run_day_ticks",
    "tick_times_for_day",
]
