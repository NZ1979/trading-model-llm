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

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from analysis.indicators import compute_intraday_indicators, generate_signal
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.historical_sentiment import latest_sentiment
from data.replay.market_context import MarketContextBundle
from data.replay.pre_filter import pre_filter_candidates
from data.replay.tick_context import build_tick_context
from sim.portfolio import SimulatedPortfolio
from strategy.llm.escalation import EscalationBudget
from strategy.llm.signal_engine import TierClients, evaluate
from strategy.llm.types import LLMDecision
from strategy.signal_engine import TradeDecision, evaluate_trade


logger = logging.getLogger(__name__)


ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


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


# ===========================================================================
# Base-strategy parallel evaluation (M2.2 sub-task #17)
# ===========================================================================
#
# The base rule-based pass mirrors the live ``main.py`` evaluation loop:
# every ticker, every 5-min tick, no pre-filter. It feeds the SAME
# ``apply_day_to_portfolio`` machinery the LLM side uses by emitting
# ``TickDecision`` rows whose ``LLMDecision`` payload is adapted from
# ``TradeDecision``. The fill simulator can't tell the difference -- it
# reads action / confidence / setup_label / reasoning, all of which the
# adapter populates from the base ``TradeDecision`` shape.
#
# Synchronous (no network I/O); the driver calls it directly without
# ``await``.


def _build_today_5min_indicators(
    tds: TickerDayState, trading_date: date
) -> pd.DataFrame:
    """Resample minute_bars to RTH 5-min + add intraday indicator columns.

    Indicators (SMA, EMA, RSI, MACD, ADX, VWAP, BBands) are backward-
    looking and emit NaN until their warmup period elapses; computing
    them once on the full day's frame is equivalent to recomputing them
    on each tick's prefix slice, which is the efficiency win this
    cache buys.

    Returns the same shape as ``fill_simulator._build_today_5min`` plus
    the indicator columns ``compute_intraday_indicators`` adds. An
    empty DataFrame (no RTH bars on trading_date) returns the same
    empty shape.
    """
    if tds.minute_bars.empty:
        return tds.minute_bars.iloc[0:0]
    same_date = tds.minute_bars.index.date == trading_date
    bars_time = tds.minute_bars.index.time
    rth_mask = (bars_time >= RTH_OPEN) & (bars_time < RTH_CLOSE)
    one_min = tds.minute_bars[same_date & rth_mask]
    if one_min.empty:
        return one_min.iloc[0:0]
    five_min = one_min.resample(
        "5min", label="left", closed="left", origin="start_day"
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna(how="all")
    if five_min.empty:
        return five_min
    return compute_intraday_indicators(five_min, rth_only=False)


def _trade_to_llm_decision(td: TradeDecision) -> LLMDecision:
    """Adapt a base TradeDecision to an LLMDecision-shape for the fill simulator.

    The fill simulator (``apply_day_to_portfolio``) reads only
    ``action`` / ``confidence`` / ``setup_label`` / ``reasoning`` from
    the decision payload; everything else is LLM-tier metadata the
    base pass doesn't have. The adapter maps:

    - ``TradeDecision.action`` -> ``LLMDecision.action`` (same enum).
    - ``TradeDecision.technical_confidence`` -> ``LLMDecision.confidence``.
    - ``TradeDecision.setup`` -> ``LLMDecision.setup_label`` (truncated
      to 50 chars per the pydantic schema's ``max_length=50``).
    - ``TradeDecision.reasons`` -> ``LLMDecision.reasoning`` joined
      with "; " and truncated to 280 chars (the schema cap).

    The pydantic defaults for ``stop_loss_atr_multiple``,
    ``take_profit_atr_multiple``, ``time_horizon``, and the forward-
    prediction fields land at their declared defaults -- the base
    strategy doesn't predict take-profit or time horizon, so the
    defaults are the right "no-prediction" sentinels for the
    comparison report.
    """
    setup_label = (td.setup or "none")[:50]
    reasoning = "; ".join(td.reasons) if td.reasons else "no_reasons"
    reasoning = reasoning[:280]
    return LLMDecision(
        action=td.action,
        confidence=int(td.technical_confidence),
        setup_label=setup_label,
        reasoning=reasoning,
    )


def run_day_base_ticks(
    *,
    day_state: DayState,
    config: ReplayConfig,
    sentiment_conn: sqlite3.Connection,
    portfolio: SimulatedPortfolio | None = None,
) -> list[TickDecision]:
    """Run the 78-tick base rule-based evaluation pass for one trading day.

    For each of the 78 canonical 5-min ticks (09:30..15:55 ET) and
    every ticker in ``day_state.tickers``:

    1. Resample minute_bars to 5-min RTH with intraday indicators
       (cached per ticker per day -- one resample + one indicator
       pass per ticker regardless of tick count).
    2. Slice the indicator frame up to and including ``tick_et``
       (point-in-time correct: no peeking at future bars beyond the
       tick that just fired).
    3. ``generate_signal(intraday_df, daily_ctx, premarket_ctx)``
       returns a ``TechnicalSignal``.
    4. ``latest_sentiment(sentiment_conn, ticker, tick_et)`` returns
       the most-recent sentiment score (or None) within the live
       default 3600s freshness window.
    5. ``evaluate_trade(ticker, sentiment, technical_signal,
       futures_walls=None,
       require_walls_for_pullback=config.base_require_walls_for_pullback)``
       returns a ``TradeDecision``. Walls are always None in replay
       (Databento canceled, PROJECT_BLUEPRINT § Vendor stack).
    6. Adapt ``TradeDecision`` -> ``LLMDecision`` via
       ``_trade_to_llm_decision``, wrap in ``TickDecision``, append.

    NO pre-filter is applied -- base evaluation runs on every ticker
    every tick, per the design doc § Pre-filter: "the base codebase's
    signal engine evaluates ALL watchlist tickers every tick. To make
    the comparison fair, we run base evaluation on every ticker too
    -- only the LLM evaluation is gated by the pre-filter."

    Args:
        day_state: per-day bundle from ``build_day_state``.
        config: full ReplayConfig. Reads
            ``base_require_walls_for_pullback``.
        sentiment_conn: open historical-sentiment fixture (the SAME
            connection the LLM pass uses; one DB, two readers).
        portfolio: optional ``SimulatedPortfolio`` (read-only). The
            base pass does NOT use a pre-filter holding gate -- the
            base side evaluates regardless of current position; the
            fill simulator's transition table handles the no-op /
            flip cases. The arg is reserved here for symmetry with
            ``run_day_ticks`` and for future use if the base pass ever
            wants position context.

    Returns:
        ``list[TickDecision]`` in (tick_et, ticker-order-of-day_state.tickers)
        order. Empty when ``day_state.tickers`` is empty. The order
        matches what ``apply_day_to_portfolio`` expects (chronological,
        with ties broken by input order at the same tick).

    Side effects: queries ``sentiment_conn`` (read-only via
    ``latest_sentiment``). Does NOT mutate ``portfolio``.

    Never raises on normal data shape. Per-ticker failures (empty
    bars, indicator NaN, etc.) degrade visibly: the technical signal
    returns Hold and the loop continues.
    """
    decisions: list[TickDecision] = []
    indicator_cache: dict[str, pd.DataFrame] = {}

    # Iterate the canonical 78 ticks (same as run_day_ticks).
    for tick_et in tick_times_for_day(day_state.trading_date):
        for ticker, tds in day_state.tickers.items():
            # Build / look up the indicator-augmented 5-min frame.
            if ticker not in indicator_cache:
                indicator_cache[ticker] = _build_today_5min_indicators(
                    tds, day_state.trading_date
                )
            five_min = indicator_cache[ticker]
            # Point-in-time slice: bars at or before this tick.
            if not five_min.empty:
                tick_ts = pd.Timestamp(tick_et)
                intraday_df = five_min[five_min.index <= tick_ts]
            else:
                intraday_df = five_min

            tech = generate_signal(
                intraday_df,
                tds.daily_context,
                tds.premarket_context,
            )

            sentiment_score = latest_sentiment(
                sentiment_conn, ticker, tick_et,
            )

            trade = evaluate_trade(
                ticker=ticker,
                sentiment_score=sentiment_score,
                technical_signal=tech,
                futures_walls=None,
                require_walls_for_pullback=(
                    config.base_require_walls_for_pullback
                ),
            )
            decisions.append(
                TickDecision(
                    tick_et=tick_et,
                    ticker=ticker,
                    decision=_trade_to_llm_decision(trade),
                )
            )

    return decisions


# ===========================================================================
# Tier 3 (Opus) labeling pass (M2.2 sub-task #20)
# ===========================================================================
#
# Tier 3 is replay-only -- live evaluate() ignores clients.t3
# (signal_engine.py line 12-14). The replay harness runs it as a
# DISTINCT FLOW (not a tier inside evaluate): same LLMContext the live
# path saw, but a different decision path. The persisted rows use
# decision_source='t3_only' so the comparison report's section 5d can
# compute T1↔T3 agreement metrics without touching the live merged
# stream.
#
# Two gates protect cost:
#   1. Sample-rate gate (deterministic hash-based, cache-reusable).
#   2. T3Budget cap (Rule 18 visible degradation -- skip-with-counter,
#      no silent throttling).


_T3_LABEL_SCHEMA_INVALID = "schema_invalid_t3"
_T3_LABEL_API_FAILURE = "api_failure_t3"
_T3_LABEL_UNEXPECTED = "t3_unexpected"


def _make_t3_hold_failure(label: str, reason: str) -> LLMDecision:
    """Synthesize a Hold for Tier 3 failure paths.

    Mirrors ``strategy.llm.signal_engine._make_hold_failure`` but
    encodes T3 provenance so post-hoc analysis can distinguish T1
    failures from T3 failures by tier_provenance alone.
    """
    return LLMDecision(
        action="Hold",
        confidence=0,
        setup_label=label[:50],
        reasoning=reason[:280],
        tier_provenance="t3_failed",
    )


def _t3_should_sample(
    ticker: str, tick_et: datetime, sample_rate: float,
) -> bool:
    """Deterministic hash-based sampling decision.

    Returns True iff the (ticker, tick_et) hash bucket falls under
    ``sample_rate``. Same (ticker, tick) always samples or doesn't at
    the same rate, so re-runs are reproducible. Reducing rate from 1.0
    to 0.5 produces a strict subset of the rate=1.0 selections, which
    means a prior 1.0 cache is fully reusable on a 0.5 follow-up (no
    cache miss for any sampled candidate).
    """
    if sample_rate >= 1.0:
        return True
    if sample_rate <= 0.0:
        return False
    h = hashlib.sha256(
        f"{ticker}|{tick_et.isoformat()}".encode("utf-8")
    ).hexdigest()
    bucket = int(h[:8], 16) / 0xFFFFFFFF
    return bucket < sample_rate


async def run_day_t3_ticks(
    *,
    day_state: DayState,
    market_ctx: MarketContextBundle,
    config: ReplayConfig,
    clients: TierClients,
    budget,  # T3Budget; not annotated to avoid circular import at module top
    portfolio: SimulatedPortfolio | None = None,
) -> list[TickDecision]:
    """Run the 78-tick Opus labeling pass for one trading day.

    For each (tick, ticker) that survives the same pre-filter the LLM
    path uses, this function:

    1. Checks the deterministic sample-rate gate. Skips on miss
       (budget.record_skip_sample()).
    2. Checks the T3Budget cap. Skips with WARNING on exhaustion
       (budget.record_skip_budget()).
    3. Builds the SAME LLMContext that ``run_day_ticks`` would have
       built for this (ticker, tick) -- includes prior-decisions
       history scoped to T3's own history (not shared with the live
       T1+T2 stream).
    4. Calls ``clients.t3.evaluate(ctx)``.
    5. On success: records the call, emits a TickDecision with the
       raw T3 LLMDecision (tier_provenance set by the T3 client or
       defaulting to None).
    6. On failure: emits a TickDecision with a synthetic Hold whose
       setup_label encodes the failure mode (schema_invalid_t3 /
       api_failure_t3 / t3_unexpected) and ``tier_provenance="t3_failed"``.

    Args:
        day_state: per-day bundle from ``build_day_state``.
        market_ctx: run-level SPY+VIX bundle.
        config: full ReplayConfig. Reads ``t3_sample_rate``.
        clients: ``TierClients`` with a non-None ``t3``. Caller is
            expected to have wrapped t3 in ``CachedLLMClient`` to
            avoid re-paying Opus cost on re-runs.
        budget: ``T3Budget`` instance owned by the caller. Mutated in
            place via ``record_call`` / ``record_skip_*``.
        portfolio: optional ``SimulatedPortfolio``. Read only -- used
            for the pre-filter holding gate AND the per-ticker
            position dict in LLMContext, mirroring ``run_day_ticks``'s
            semantics so T3 sees the same context as T1.

    Returns:
        ``list[TickDecision]`` for every candidate that actually
        produced a T3 call (sampled in AND budget allowed). Skipped
        candidates do NOT appear in the result; their counts live on
        the budget.

    Raises:
        ValueError: clients.t3 is None. The driver guards this at the
            call site; reaching this exception is a programmer bug.
    """
    if clients.t3 is None:
        raise ValueError(
            "run_day_t3_ticks called with clients.t3=None; "
            "the driver should guard this. T3 has no client to call."
        )

    decisions: list[TickDecision] = []
    t3_prior_history: dict[str, list[dict[str, Any]]] = {}

    for tick_et in tick_times_for_day(day_state.trading_date):
        holding = _currently_holding(portfolio)
        candidates = pre_filter_candidates(
            day_state, tick_et, holding, config,
        )
        for ticker in candidates:
            # Sample-rate gate first (cheap, hash-only).
            if not _t3_should_sample(ticker, tick_et, config.t3_sample_rate):
                budget.record_skip_sample()
                continue
            # Budget gate next.
            if not budget.has_capacity():
                budget.record_skip_budget()
                logger.warning(
                    "run_day_t3_ticks: budget exhausted at %s / %s "
                    "(used=%.4f / cap=%.4f); skipping",
                    tick_et, ticker,
                    budget.used_dollars, budget.cap_dollars,
                )
                continue

            ctx = build_tick_context(
                day_state=day_state,
                market_ctx=market_ctx,
                config=config,
                ticker=ticker,
                tick_et=tick_et,
                position=_position_dict(portfolio, ticker),
                todays_prior_decisions=tuple(
                    t3_prior_history.get(ticker, [])[-PRIOR_DECISIONS_TAIL:]
                ),
            )

            # Call T3. Budget is consumed on attempt, not success
            # (mirrors the live EscalationBudget pattern for T2).
            budget.record_call()
            try:
                raw = await clients.t3.evaluate(ctx)
                # Tag tier_provenance for in-memory analysis. The
                # persistence layer uses decision_source='t3_only'
                # separately; both labels carry the same info but
                # the in-memory one survives without a DB round-trip.
                t3_decision = raw.model_copy(
                    update={"tier_provenance": "t3_only"}
                )
            except Exception as exc:  # broad: T3 must never raise out
                # Inspect type via class name to avoid importing
                # strategy.llm.clients here (would create a deeper
                # dependency cycle).
                exc_cls = type(exc).__name__
                if exc_cls == "SchemaInvalidError":
                    t3_decision = _make_t3_hold_failure(
                        _T3_LABEL_SCHEMA_INVALID, str(exc),
                    )
                    logger.warning(
                        "T3 schema-invalid for %s @ %s: %s",
                        ticker, tick_et, exc,
                    )
                elif exc_cls == "APIUnavailableError":
                    t3_decision = _make_t3_hold_failure(
                        _T3_LABEL_API_FAILURE, str(exc),
                    )
                    logger.error(
                        "T3 API unavailable for %s @ %s: %s",
                        ticker, tick_et, exc,
                    )
                else:
                    t3_decision = _make_t3_hold_failure(
                        _T3_LABEL_UNEXPECTED,
                        f"{exc_cls}: {exc}",
                    )
                    logger.exception(
                        "T3 unexpected error for %s @ %s", ticker, tick_et,
                    )

            decisions.append(
                TickDecision(
                    tick_et=tick_et,
                    ticker=ticker,
                    decision=t3_decision,
                )
            )
            t3_prior_history.setdefault(ticker, []).append(
                _prior_record(tick_et, t3_decision)
            )

    return decisions


__all__ = [
    "PRIOR_DECISIONS_TAIL",
    "TICKS_PER_DAY",
    "TICK_INTERVAL_MINUTES",
    "TickDecision",
    "run_day_base_ticks",
    "run_day_t3_ticks",
    "run_day_ticks",
    "tick_times_for_day",
]
