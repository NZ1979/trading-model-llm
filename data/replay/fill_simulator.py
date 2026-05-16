"""Decision-to-fill, per-bar stop/MTM, and EOD-flatten wiring for the M2 replay harness.

This module owns four public entry points that the replay driver
composes together to produce one trading day's portfolio mutations:

- ``apply_decisions_to_portfolio`` (#14): chronological walk of one
  day's ``list[TickDecision]``, converting Buy/Sell into entries +
  flips. Pure decision-driven mutations; no stops, no MTM, no EOD.
  Kept as a stable seam for ablation tests and the 31-test suite from
  the prior sub-task.
- ``apply_bar_to_portfolio`` (#15): for one canonical 5-min bar
  timestamp, runs ``portfolio.check_stops`` against the bar's
  high/low, records any triggered stop-outs with
  ``exit_reason="stop_hit"``, then calls ``portfolio.mark_to_market``
  with the bar's closes. Returns a structured ``BarApplicationResult``.
  Never raises on data shape; missing bars degrade visibly.
- ``flatten_at_eod`` (#15): one-shot pass over ``portfolio.positions``
  at the day's final bar timestamp, closing each at the bar close (or
  entry-price fallback with a WARNING log if the bar is missing) with
  ``exit_reason="eod_flatten"``. Emits a final mark-to-market so the
  equity curve ends in a fully-cash state.
- ``apply_day_to_portfolio`` (#15): the driver-facing orchestrator.
  Interleaves ``apply_bar_to_portfolio`` and decision processing over
  the 78 canonical 5-min ticks, then calls ``flatten_at_eod``. Returns
  a ``DayApplicationResult`` aggregating fills, rejections, stop-outs,
  EOD exits, and the equity curve emitted during the day.

Position-transition table (per design doc § Simulation), unchanged
from #14::

    | current | decision | action                                         |
    |---------|----------|------------------------------------------------|
    | flat    | Buy      | record_entry long                              |
    | flat    | Sell     | record_entry short                             |
    | flat    | Hold     | no-op                                          |
    | long    | Buy      | no-op (already long)                           |
    | long    | Sell     | record_exit long + record_entry short          |
    | long    | Hold     | no-op                                          |
    | short   | Buy      | record_exit short + record_entry long          |
    | short   | Sell     | no-op (already short)                          |
    | short   | Hold     | no-op                                          |

Ordering inside a single canonical tick when ``apply_day_to_portfolio``
runs (#15):

1. ``apply_bar_to_portfolio(bar_et)`` -- stops fire first (so a stop
   trigger frees the slot for a same-tick re-entry); then MTM.
2. Each decision at ``tick_et == bar_et`` is processed via
   ``_apply_one_decision`` in input order.

Rationale: stops are price-driven, decisions are decision-driven; in
live the broker fills the stop before the next 5-min eval, so the
sim observes stops before decisions at the same tick.

Status: M2.2 sub-task #15 -- fully implemented (stop-checking, EOD
flatten, MTM). Tier 3 (Opus) labeling, base-strategy parallel
evaluation, and ``replay_results.db`` persistence are still separate
follow-ups.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.tick_loop import TickDecision
from sim.fills import SimulatedFill, apply_slippage, simulate_fill
from sim.portfolio import EquityPoint, SimulatedPortfolio
from strategy.risk import (
    Position as RiskPosition,
    compute_atr_stop_pct,
    size_from_risk,
    validate_order,
)

logger = logging.getLogger(__name__)


ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# 78 canonical 5-min ticks per RTH day: 09:30, 09:35, ..., 15:55.
_TICKS_PER_DAY = 78


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RejectedEntry:
    """One Buy/Sell decision that did NOT produce a fill.

    Captured as structured output (rather than logged-and-forgotten) so
    the comparison-report generator can break down "decisions that
    didn't transact" by reason: oversized notional, no next bar at the
    last tick, missing bar data, etc.
    """

    tick_et: datetime
    ticker: str
    side: Literal["buy", "sell"]
    requested_qty: int  # 0 when rejection happened before sizing
    reason: str


@dataclass(frozen=True, slots=True)
class FillSimulationResult:
    """Manifest of one day's decision-walk fill simulation.

    ``fills`` lists every successful entry (in chronological order).
    Flip exits are NOT in ``fills`` (they live on the closed-position
    side of the portfolio via ``record_exit`` with
    ``exit_reason="flip"``); only entries are first-class fills here.
    ``rejections`` captures Buy/Sell decisions that didn't transact.
    """

    fills: tuple[SimulatedFill, ...]
    rejections: tuple[RejectedEntry, ...]


@dataclass(frozen=True, slots=True)
class StopOut:
    """One stop-out triggered on a bar's range.

    Captured by ``apply_bar_to_portfolio`` whenever a long position's
    stop_price is at or above the bar's low (or a short's stop_price is
    at or below the bar's high). Fill price IS the stop price -- stops
    are treated as market-on-trigger with no additional slippage (per
    design doc and ``check_stops`` docstring). The corresponding
    ``record_exit`` call uses ``exit_reason="stop_hit"`` and lives on
    ``portfolio.closed_positions``.
    """

    bar_et: datetime
    ticker: str
    side: Literal["buy", "sell"]  # the side BEFORE the stop closed it
    qty: int
    stop_price: float
    realized_pl: float


@dataclass(frozen=True, slots=True)
class BarApplicationResult:
    """Manifest of one canonical 5-min bar's price-driven mutations.

    Emitted by ``apply_bar_to_portfolio``. ``mtm_point`` is always
    populated (even on an empty portfolio it reflects the cash
    snapshot for the day). ``stop_outs`` is empty when no positions
    triggered during this bar.
    """

    bar_et: datetime
    stop_outs: tuple[StopOut, ...]
    mtm_point: EquityPoint


@dataclass(frozen=True, slots=True)
class EodExit:
    """One position closed by the end-of-day flatten pass."""

    flatten_et: datetime
    ticker: str
    side: Literal["buy", "sell"]  # the side BEFORE the flatten closed it
    qty: int
    exit_price: float
    realized_pl: float


@dataclass(frozen=True, slots=True)
class EodFlattenResult:
    """Manifest of one day's end-of-day flatten pass.

    ``exits`` is empty when no positions were open going into the
    final bar. The post-flatten mark-to-market that ``flatten_at_eod``
    emits is appended to ``portfolio.equity_curve`` directly (not
    returned here) since the driver collects the curve via the
    portfolio reference, not through this result.
    """

    flatten_et: datetime
    exits: tuple[EodExit, ...]


@dataclass(frozen=True, slots=True)
class DayApplicationResult:
    """Manifest of one trading day's interleaved bar+decision walk.

    Aggregates the output of ``apply_bar_to_portfolio`` (per bar),
    ``_apply_one_decision`` (per Buy/Sell decision), and
    ``flatten_at_eod`` (once at day end). ``equity_curve`` is a tuple
    of every ``EquityPoint`` emitted during the day in chronological
    order -- typically 78 from the per-bar MTM calls, plus one final
    post-flatten point. When the final bar is 15:55 ET and any
    positions were open, the curve has TWO points stamped 15:55:
    the per-bar MTM (pre-flatten, positions still open) and the
    post-flatten MTM (n_open_positions=0). The comparison report
    keeps both so drawdown timelines reflect the full intraday path.
    """

    fills: tuple[SimulatedFill, ...]
    rejections: tuple[RejectedEntry, ...]
    stop_outs: tuple[StopOut, ...]
    eod_exits: tuple[EodExit, ...]
    equity_curve: tuple[EquityPoint, ...]


# ---------------------------------------------------------------------------
# 5-min bar lookup helpers
# ---------------------------------------------------------------------------


def _build_today_5min(tds: TickerDayState, trading_date: date) -> pd.DataFrame:
    """Resample TickerDayState.minute_bars to RTH 5-min bars on trading_date.

    Filters to RTH (09:30 inclusive, 16:00 exclusive) before resampling
    so the 5-min frame's first index is 09:30 and its last index is
    15:55. Pre/post-market 1-min bars are excluded.

    Returns an empty DataFrame (same columns) when source minute_bars
    is empty or has no RTH bars on trading_date.
    """
    if tds.minute_bars.empty:
        return tds.minute_bars.iloc[0:0]
    # minute_bars is tz-aware ET per load_historical_bars_1min.
    same_date = tds.minute_bars.index.date == trading_date
    bars_time = tds.minute_bars.index.time
    rth_mask = (bars_time >= RTH_OPEN) & (bars_time < RTH_CLOSE)
    one_min = tds.minute_bars[same_date & rth_mask]
    if one_min.empty:
        return one_min.iloc[0:0]
    grouped = one_min.resample(
        "5min", label="left", closed="left", origin="start_day"
    )
    out = grouped.agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna(how="all")
    return out


def _get_or_build_5min(
    *,
    ticker: str,
    day_state: DayState,
    resample_cache: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Memoized accessor for a ticker's 5-min RTH frame.

    ``resample_cache`` is mutated in place -- the caller owns it and
    can share it across all four entry points for one day's walk so a
    ticker is resampled exactly once regardless of how many bars +
    decisions reference it.
    """
    if ticker not in resample_cache:
        tds = day_state.tickers.get(ticker)
        if tds is None:
            resample_cache[ticker] = pd.DataFrame()
        else:
            resample_cache[ticker] = _build_today_5min(
                tds, day_state.trading_date
            )
    return resample_cache[ticker]


@dataclass(frozen=True, slots=True)
class _BarLookup:
    """Result of resolving the fill bars at a given tick."""

    current_close: float
    next_open: float | None
    next_timestamp: datetime | None


def _lookup_bars_at(
    five_min: pd.DataFrame, tick_et: datetime
) -> _BarLookup | str:
    """Return _BarLookup at tick_et, or a rejection-reason string.

    Reasons returned (strings, not exceptions, so the caller can record
    them on a RejectedEntry):

    - ``"no_5min_bars"``: 5-min frame is empty (typically the data prep
      stage gave us a ticker with no RTH bars).
    - ``"no_5min_bar_at_tick"``: the tick timestamp is not present in
      the 5-min index (gap in the source 1-min frame at that 5-min
      window).
    """
    if five_min.empty:
        return "no_5min_bars"
    tick_ts = pd.Timestamp(tick_et)
    if tick_ts not in five_min.index:
        return "no_5min_bar_at_tick"
    current_close = float(five_min.loc[tick_ts, "close"])
    next_ts = tick_ts + pd.Timedelta(minutes=5)
    if next_ts in five_min.index:
        next_open: float | None = float(five_min.loc[next_ts, "open"])
        next_timestamp: datetime | None = next_ts.to_pydatetime()
    else:
        next_open = None
        next_timestamp = None
    return _BarLookup(
        current_close=current_close,
        next_open=next_open,
        next_timestamp=next_timestamp,
    )


# ---------------------------------------------------------------------------
# Risk-module adapter + small per-decision helpers
# ---------------------------------------------------------------------------


def _open_positions_for_risk(
    portfolio: SimulatedPortfolio,
) -> list[RiskPosition]:
    """Convert sim.portfolio.Position rows to strategy.risk.Position rows.

    ``strategy.risk.Position`` uses SIGNED quantity (positive long,
    negative short) and tracks ``current_price`` separately from
    ``avg_price``. Until per-tick mark-to-market lands across the
    risk gate (the gate still re-reads on each decision), the
    ``current_price`` here is approximated as ``entry_price``. The
    portfolio's equity curve from MTM IS exact for reporting -- the
    risk-gate approximation is a slightly conservative
    "exposure-at-entry" view, which is fine for cap enforcement.
    """
    out: list[RiskPosition] = []
    for ticker, pos in portfolio.positions.items():
        if not pos.is_open:
            continue
        out.append(
            RiskPosition(
                ticker=ticker,
                quantity=pos.signed_qty(),
                avg_price=pos.entry_price,
                current_price=pos.entry_price,
            )
        )
    return out


def _entry_side(action: str) -> Literal["buy", "sell"]:
    """Map LLMDecision.action ('Buy'|'Sell') to fill side ('buy'|'sell')."""
    if action == "Buy":
        return "buy"
    if action == "Sell":
        return "sell"
    raise ValueError(f"Cannot derive entry side from action {action!r}")


def _fill_reference(
    bar_lookup: _BarLookup, fill_at: str
) -> tuple[float, datetime | None]:
    """Pick the reference price + timestamp the fill should use.

    next_bar_open: (next_open, next_timestamp). Returns (NaN, None)
        when there is no next bar -- caller has already filtered that
        before reaching this helper, so this branch is defensive.
    current_close: (current_close, None). The caller substitutes the
        current bar timestamp.
    """
    if fill_at == "next_bar_open":
        if bar_lookup.next_open is None:
            return float("nan"), None
        return bar_lookup.next_open, bar_lookup.next_timestamp
    if fill_at == "current_close":
        return bar_lookup.current_close, None
    raise ValueError(f"Unknown fill_at value: {fill_at!r}")


def _attempt_entry(
    *,
    tick_et: datetime,
    ticker: str,
    side: Literal["buy", "sell"],
    bar_lookup: _BarLookup,
    decision_id: int,
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,
) -> tuple[SimulatedFill | None, RejectedEntry | None]:
    """Run sizing + risk gate + simulate_fill + record_entry for one decision.

    Returns ``(fill, None)`` on success or ``(None, rejection)`` on any
    failure mode (oversized notional, missing bar data, risk-gate
    rejection, etc). ``portfolio`` is mutated in place on success via
    ``record_entry``.
    """
    reference_price, _ = _fill_reference(bar_lookup, config.fill_at)

    tds = day_state.tickers[ticker]
    daily_atr = (
        tds.daily_context.daily_atr_14
        if tds.daily_context is not None
        else 0.0
    )
    stop_pct = compute_atr_stop_pct(
        entry_price=reference_price,
        daily_atr=daily_atr,
    )

    equity = portfolio.equity({})
    if equity <= 0:
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker, side=side,
            requested_qty=0, reason="invalid_equity",
        )

    requested_qty = size_from_risk(
        account_equity=equity,
        entry_price=reference_price,
        stop_loss_pct=stop_pct,
        risk_per_trade_pct=config.risk_per_trade_pct,
    )
    if requested_qty <= 0:
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker, side=side,
            requested_qty=0, reason="size_from_risk_zero",
        )

    check = validate_order(
        ticker=ticker,
        side=side,
        requested_quantity=requested_qty,
        current_price=reference_price,
        account_equity=equity,
        open_positions=_open_positions_for_risk(portfolio),
        max_position_pct=config.max_position_pct,
        stop_loss_pct=stop_pct,
    )
    if not check.approved:
        logger.info(
            "fill_simulator: %s @ %s %s qty=%d rejected: %s",
            ticker, tick_et, side, requested_qty, check.reason,
        )
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker, side=side,
            requested_qty=requested_qty, reason=check.reason,
        )

    current_bar_ts = pd.Timestamp(tick_et).to_pydatetime()
    sf = simulate_fill(
        ticker=ticker,
        side=side,
        qty=check.quantity,
        decision_id=decision_id,
        fill_at=config.fill_at,
        current_bar_close=bar_lookup.current_close,
        next_bar_open=bar_lookup.next_open,
        next_bar_timestamp=bar_lookup.next_timestamp,
        current_bar_timestamp=current_bar_ts,
        stop_price=check.stop_price,  # type: ignore[arg-type]
        slippage_bps=config.slippage_bps,
    )
    if sf is None:
        # simulate_fill returns None only when fill_at="next_bar_open"
        # and next_bar_open is None. The decision walker filters that
        # case before calling _attempt_entry, so reaching here is a
        # defensive guard rather than a normal path.
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker, side=side,
            requested_qty=check.quantity,
            reason="simulate_fill_returned_none",
        )

    portfolio.record_entry(sf)
    return sf, None


def _apply_one_decision(
    *,
    decision: TickDecision,
    decision_id: int,
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,
    resample_cache: dict[str, pd.DataFrame],
) -> tuple[SimulatedFill | None, RejectedEntry | None]:
    """Apply one TickDecision against the portfolio.

    Returns ``(fill, rejection)`` with at most one populated. Flip
    exits (long+Sell, short+Buy) are recorded on
    ``portfolio.closed_positions`` via ``record_exit`` and are NOT
    surfaced through the return value -- the caller observes flips by
    inspecting ``portfolio.closed_positions``.
    """
    action = decision.decision.action
    ticker = decision.ticker
    tick_et = decision.tick_et

    if action == "Hold":
        return None, None

    # Bars first -- any failure to locate the fill bars produces a
    # structured rejection regardless of position state.
    five_min = _get_or_build_5min(
        ticker=ticker, day_state=day_state, resample_cache=resample_cache,
    )
    bar_lookup = _lookup_bars_at(five_min, tick_et)
    if isinstance(bar_lookup, str):
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker,
            side=_entry_side(action),
            requested_qty=0, reason=bar_lookup,
        )

    if config.fill_at == "next_bar_open" and bar_lookup.next_open is None:
        return None, RejectedEntry(
            tick_et=tick_et, ticker=ticker,
            side=_entry_side(action),
            requested_qty=0, reason="no_next_bar_for_fill",
        )

    existing = portfolio.get_position(ticker)
    if existing is not None:
        # No-op: same-direction
        if existing.side == "buy" and action == "Buy":
            return None, None
        if existing.side == "sell" and action == "Sell":
            return None, None
        # Flip path: close existing first at the same reference, with
        # slippage applied to the EXIT side (opposite of existing.side).
        exit_side: Literal["buy", "sell"] = (
            "sell" if existing.side == "buy" else "buy"
        )
        ref_price, ref_ts = _fill_reference(bar_lookup, config.fill_at)
        exit_price = apply_slippage(
            ref_price, exit_side, config.slippage_bps
        )
        exit_ts = (
            ref_ts
            if ref_ts is not None
            else pd.Timestamp(tick_et).to_pydatetime()
        )
        portfolio.record_exit(
            ticker=ticker,
            exit_price=exit_price,
            exit_timestamp=exit_ts,
            exit_reason="flip",
        )

    # Open new (flat path OR post-flip).
    return _attempt_entry(
        tick_et=tick_et,
        ticker=ticker,
        side=_entry_side(action),
        bar_lookup=bar_lookup,
        decision_id=decision_id,
        day_state=day_state,
        portfolio=portfolio,
        config=config,
    )


# ---------------------------------------------------------------------------
# Public entry point #1: decisions-only walker (sub-task #14)
# ---------------------------------------------------------------------------


def apply_decisions_to_portfolio(
    *,
    decisions: list[TickDecision],
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,
    decision_id_start: int = 1,
) -> FillSimulationResult:
    """Walk one day's decisions and convert them to portfolio mutations.

    Decisions-only walk; no per-bar stop-checks, no per-bar MTM, no
    EOD flatten. Kept as the stable seam for the 31-test #14 suite and
    for ablation runs that want raw decision behavior without the
    price-driven mutations layered on top.

    For the full interleaved walk used by ``run_replay``, see
    ``apply_day_to_portfolio``.

    Args:
        decisions: chronological list of TickDecisions for one trading
            day (as returned by run_day_ticks).
        day_state: the same DayState that produced ``decisions``;
            re-used here to slice per-ticker minute bars and read
            DailyContext.daily_atr_14 for ATR-based stops.
        portfolio: the SimulatedPortfolio to mutate. Entry / exit
            bookkeeping happens in place; the caller retains the
            reference for downstream MTM and EOD-flatten passes.
        config: ReplayConfig. The function reads ``fill_at``,
            ``slippage_bps``, ``risk_per_trade_pct``,
            ``max_position_pct``.
        decision_id_start: the value to assign to the FIRST decision's
            ``SimulatedFill.decision_id``. Subsequent decisions get
            consecutive ids in input order. Default 1; the persistence
            sub-task will reconcile these with the
            ``replay_decisions`` table's primary keys.

    Returns:
        FillSimulationResult with successful entry fills and structured
        rejection records. Flip exits are visible via
        ``portfolio.closed_positions`` with ``exit_reason="flip"``.

    Side effects: mutates ``portfolio``.
    """
    fills: list[SimulatedFill] = []
    rejections: list[RejectedEntry] = []
    resample_cache: dict[str, pd.DataFrame] = {}

    for idx, td in enumerate(decisions):
        decision_id = decision_id_start + idx
        fill, rejection = _apply_one_decision(
            decision=td,
            decision_id=decision_id,
            day_state=day_state,
            portfolio=portfolio,
            config=config,
            resample_cache=resample_cache,
        )
        if fill is not None:
            fills.append(fill)
        if rejection is not None:
            rejections.append(rejection)

    return FillSimulationResult(
        fills=tuple(fills),
        rejections=tuple(rejections),
    )


# ---------------------------------------------------------------------------
# Public entry point #2: per-bar stops + MTM (sub-task #15)
# ---------------------------------------------------------------------------


def apply_bar_to_portfolio(
    *,
    bar_et: datetime,
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,  # accepted for symmetry / future use
    resample_cache: dict[str, pd.DataFrame] | None = None,
) -> BarApplicationResult:
    """Apply one canonical 5-min bar's price-driven mutations.

    Pulls each open position's bar (high, low, close) from its
    resampled 5-min frame at ``bar_et``, runs ``portfolio.check_stops``,
    records exits for any triggered stops with
    ``exit_reason="stop_hit"``, then calls ``portfolio.mark_to_market``
    with the gathered close prices.

    Missing bar data for a position's ticker degrades visibly: the
    ticker is omitted from the price dicts so ``check_stops`` cannot
    false-positive (Rule 18 -- never silent), and ``mark_to_market``
    falls back to the position's entry price via ``equity()``'s
    missing-key handling (portfolio.py lines 231-233).

    Args:
        bar_et: the 5-min bar's start timestamp in ET. The function
            looks up exactly that key in each ticker's resampled frame
            (``label="left", closed="left"`` per the resample contract).
        day_state: source of per-ticker minute bars for resampling.
        portfolio: the SimulatedPortfolio to mutate.
        config: ReplayConfig. Accepted for forward compatibility and
            symmetric signatures with the other entry points; not
            currently read inside this function.
        resample_cache: optional per-day cache of resampled frames.
            When the caller (typically ``apply_day_to_portfolio``)
            supplies one, it is mutated in place and shared with the
            decision walker and EOD flatten to keep resampling cost
            at one pass per ticker per day. Pass ``None`` and one will
            be allocated locally.

    Returns:
        BarApplicationResult with the triggered stop-outs and the
        emitted equity point.

    Side effects: mutates ``portfolio`` (record_exit, mark_to_market).

    Never raises on normal data shape.
    """
    if resample_cache is None:
        resample_cache = {}

    # Gather per-open-position bar data at this timestamp.
    bar_lows: dict[str, float] = {}
    bar_highs: dict[str, float] = {}
    close_prices: dict[str, float] = {}
    tick_ts = pd.Timestamp(bar_et)
    for ticker in list(portfolio.positions.keys()):
        pos = portfolio.positions.get(ticker)
        if pos is None or not pos.is_open:
            continue
        five_min = _get_or_build_5min(
            ticker=ticker, day_state=day_state, resample_cache=resample_cache,
        )
        if five_min.empty or tick_ts not in five_min.index:
            # Visible degradation: ticker absent from price dicts means
            # check_stops cannot fire on it (no false positive), and
            # equity() falls back to entry_price (no false MTM swing).
            logger.debug(
                "apply_bar_to_portfolio: no bar for %s at %s; "
                "stop-check skipped, MTM falls back to entry_price",
                ticker, bar_et,
            )
            continue
        bar_lows[ticker] = float(five_min.loc[tick_ts, "low"])
        bar_highs[ticker] = float(five_min.loc[tick_ts, "high"])
        close_prices[ticker] = float(five_min.loc[tick_ts, "close"])

    # Stop-check.
    triggered = portfolio.check_stops(bar_et, bar_lows, bar_highs)
    stop_outs: list[StopOut] = []
    for ticker, stop_price in triggered:
        pos = portfolio.positions.get(ticker)
        if pos is None or not pos.is_open:
            # Defensive: check_stops returned a ticker we no longer
            # hold. Shouldn't happen with the current portfolio impl.
            continue
        side = pos.side
        qty = pos.qty
        realized = portfolio.record_exit(
            ticker=ticker,
            exit_price=stop_price,
            exit_timestamp=bar_et,
            exit_reason="stop_hit",
        )
        stop_outs.append(
            StopOut(
                bar_et=bar_et,
                ticker=ticker,
                side=side,
                qty=qty,
                stop_price=stop_price,
                realized_pl=realized,
            )
        )

    # MTM at the bar's close prices.
    mtm_point = portfolio.mark_to_market(bar_et, close_prices)

    return BarApplicationResult(
        bar_et=bar_et,
        stop_outs=tuple(stop_outs),
        mtm_point=mtm_point,
    )


# ---------------------------------------------------------------------------
# Public entry point #3: EOD flatten (sub-task #15)
# ---------------------------------------------------------------------------


def flatten_at_eod(
    *,
    flatten_et: datetime,
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,  # accepted for symmetry / future use
    resample_cache: dict[str, pd.DataFrame] | None = None,
) -> EodFlattenResult:
    """Close every open position at ``flatten_et``'s bar close.

    Iterates a snapshot of ``portfolio.positions`` (snapshot before
    mutation to avoid dict-size-changed errors), closes each at the
    bar's close price via ``record_exit`` with
    ``exit_reason="eod_flatten"``. When the bar at ``flatten_et`` is
    missing for a ticker, falls back to the position's entry price and
    logs a WARNING (Rule 18 -- visible degradation).

    Emits a final ``mark_to_market`` at ``flatten_et`` AFTER the
    flatten loop so the equity curve's final point reflects the
    all-cash post-flatten state. This produces a SECOND equity point
    stamped at ``flatten_et`` if ``apply_bar_to_portfolio`` was also
    called for that same timestamp earlier in the day's walk. Both
    points are kept; the comparison report can dedup by timestamp or
    use whichever it prefers.

    Args:
        flatten_et: the canonical timestamp at which to close. In
            production replays this is the day's last 5-min bar
            timestamp (15:55 ET); tests may pass other timestamps.
        day_state: source of per-ticker minute bars for resampling.
        portfolio: the SimulatedPortfolio to mutate.
        config: ReplayConfig. Accepted for forward compatibility.
        resample_cache: optional shared per-day resample cache.

    Returns:
        EodFlattenResult listing every position that was closed.

    Side effects: mutates ``portfolio`` (record_exit, mark_to_market).

    Never raises on normal data shape.
    """
    if resample_cache is None:
        resample_cache = {}

    exits: list[EodExit] = []
    close_prices: dict[str, float] = {}
    tick_ts = pd.Timestamp(flatten_et)
    # Snapshot tickers before mutation. record_exit deletes from
    # positions, so we must avoid iterating the live dict.
    open_tickers = [
        t for t, p in portfolio.positions.items() if p.is_open
    ]
    for ticker in open_tickers:
        pos = portfolio.positions.get(ticker)
        if pos is None or not pos.is_open:
            continue
        five_min = _get_or_build_5min(
            ticker=ticker, day_state=day_state, resample_cache=resample_cache,
        )
        if not five_min.empty and tick_ts in five_min.index:
            exit_price = float(five_min.loc[tick_ts, "close"])
        else:
            logger.warning(
                "flatten_at_eod: no bar for %s at %s; "
                "falling back to entry_price=%.4f",
                ticker, flatten_et, pos.entry_price,
            )
            exit_price = pos.entry_price
        side = pos.side
        qty = pos.qty
        realized = portfolio.record_exit(
            ticker=ticker,
            exit_price=exit_price,
            exit_timestamp=flatten_et,
            exit_reason="eod_flatten",
        )
        exits.append(
            EodExit(
                flatten_et=flatten_et,
                ticker=ticker,
                side=side,
                qty=qty,
                exit_price=exit_price,
                realized_pl=realized,
            )
        )
        close_prices[ticker] = exit_price

    # Final MTM: portfolio is now all-cash (or unchanged if there were
    # no positions). The equity point's n_open_positions is 0.
    portfolio.mark_to_market(flatten_et, close_prices)

    return EodFlattenResult(
        flatten_et=flatten_et,
        exits=tuple(exits),
    )


# ---------------------------------------------------------------------------
# Bar timeline helper
# ---------------------------------------------------------------------------


def _canonical_bar_timeline(trading_date: date) -> list[datetime]:
    """Return the 78 canonical 5-min RTH bar-start timestamps in ET.

    09:30, 09:35, 09:40, ..., 15:55 on ``trading_date`` (ET-aware
    datetimes). These are the timestamps the resample step produces
    (``label="left", closed="left"``), so a key in this list is the
    exact lookup key in any ticker's resampled 5-min frame.
    """
    base = datetime(
        trading_date.year,
        trading_date.month,
        trading_date.day,
        9, 30,
        tzinfo=ET,
    )
    return [base + timedelta(minutes=5 * i) for i in range(_TICKS_PER_DAY)]


# ---------------------------------------------------------------------------
# Public entry point #4: day orchestrator (sub-task #15)
# ---------------------------------------------------------------------------


def apply_day_to_portfolio(
    *,
    decisions: list[TickDecision],
    day_state: DayState,
    portfolio: SimulatedPortfolio,
    config: ReplayConfig,
    decision_id_start: int = 1,
) -> DayApplicationResult:
    """Walk one trading day with interleaved bars + decisions + EOD flatten.

    For each of the 78 canonical 5-min ticks (09:30..15:55 ET) on
    ``day_state.trading_date``:

    1. ``apply_bar_to_portfolio(bar_et)`` -- stops fire first against
       this bar's high/low; then MTM is taken at this bar's close.
    2. For every ``TickDecision`` with ``tick_et == bar_et``, apply it
       via ``_apply_one_decision`` (in input order).

    After the per-bar loop, ``flatten_at_eod`` runs at the timeline's
    final timestamp (15:55 ET) closing any still-open positions and
    emitting a final MTM.

    Ordering rationale: stops are price-driven and conceptually
    observed before the next 5-min decision tick fires (in live, the
    broker reports stop fills async, but they hit the order log before
    the next eval). Same-tick re-entry after a stop is therefore
    possible: stop closes at apply_bar; decision at the same tick can
    open the opposite side.

    Decision IDs advance in input order regardless of action (Hold
    consumes an id too), matching ``apply_decisions_to_portfolio``'s
    contract.

    Args:
        decisions: chronological list of TickDecisions for the day.
        day_state: the DayState that produced ``decisions``.
        portfolio: the SimulatedPortfolio to mutate.
        config: ReplayConfig.
        decision_id_start: first decision's id.

    Returns:
        DayApplicationResult aggregating fills, rejections, stop-outs,
        EOD exits, and the equity curve emitted during the day.

    Side effects: mutates ``portfolio`` via record_entry, record_exit,
    and mark_to_market.

    Never raises on normal data shape.
    """
    fills: list[SimulatedFill] = []
    rejections: list[RejectedEntry] = []
    stop_outs: list[StopOut] = []
    equity_curve: list[EquityPoint] = []
    resample_cache: dict[str, pd.DataFrame] = {}

    # Group decisions by tick_et for O(1) per-bar lookup.
    decisions_by_tick: dict[datetime, list[tuple[int, TickDecision]]] = {}
    for idx, td in enumerate(decisions):
        decision_id = decision_id_start + idx
        decisions_by_tick.setdefault(td.tick_et, []).append(
            (decision_id, td)
        )

    timeline = _canonical_bar_timeline(day_state.trading_date)

    for bar_et in timeline:
        # 1) Stops + MTM
        bar_result = apply_bar_to_portfolio(
            bar_et=bar_et,
            day_state=day_state,
            portfolio=portfolio,
            config=config,
            resample_cache=resample_cache,
        )
        stop_outs.extend(bar_result.stop_outs)
        equity_curve.append(bar_result.mtm_point)

        # 2) Decisions at this tick (in input order)
        for decision_id, td in decisions_by_tick.get(bar_et, []):
            fill, rejection = _apply_one_decision(
                decision=td,
                decision_id=decision_id,
                day_state=day_state,
                portfolio=portfolio,
                config=config,
                resample_cache=resample_cache,
            )
            if fill is not None:
                fills.append(fill)
            if rejection is not None:
                rejections.append(rejection)

    # EOD flatten at the timeline's final timestamp (15:55 ET).
    eod_result = flatten_at_eod(
        flatten_et=timeline[-1],
        day_state=day_state,
        portfolio=portfolio,
        config=config,
        resample_cache=resample_cache,
    )
    # The post-flatten MTM was appended by flatten_at_eod itself;
    # snapshot the tail to include it on equity_curve.
    if portfolio.equity_curve:
        equity_curve.append(portfolio.equity_curve[-1])

    return DayApplicationResult(
        fills=tuple(fills),
        rejections=tuple(rejections),
        stop_outs=tuple(stop_outs),
        eod_exits=eod_result.exits,
        equity_curve=tuple(equity_curve),
    )


__all__ = [
    "BarApplicationResult",
    "DayApplicationResult",
    "EodExit",
    "EodFlattenResult",
    "FillSimulationResult",
    "RejectedEntry",
    "StopOut",
    "apply_bar_to_portfolio",
    "apply_day_to_portfolio",
    "apply_decisions_to_portfolio",
    "flatten_at_eod",
]
