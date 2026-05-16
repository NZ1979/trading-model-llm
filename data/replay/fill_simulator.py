"""Decision-to-fill wiring for the M2 replay harness.

``apply_decisions_to_portfolio`` walks one day's ``list[TickDecision]``
(the output of ``run_day_ticks``) in chronological order and converts
each Buy/Sell into a ``SimulatedFill`` on the supplied
``SimulatedPortfolio``. The position-transition table the harness
operates under is the design-doc spec, reproduced here::

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

For each entry attempt the function:

1. Locates the current 5-min bar at ``tick_et`` and the next 5-min bar
   (post-tick) by resampling ``TickerDayState.minute_bars`` once per
   ticker to RTH 5-min bars indexed at the bar-start ET timestamp.
2. Selects the fill-reference price per ``config.fill_at``
   (``next_bar_open`` is the recommended default; ``current_close`` is
   the optimistic alternative for sensitivity).
3. Derives ATR-based stop distance via ``compute_atr_stop_pct`` using
   ``TickerDayState.daily_context.daily_atr_14``. ``compute_atr_stop_pct``
   handles ``daily_context is None`` / ``daily_atr_14 == 0`` via its
   own ``fallback_pct``, so no separate gate is needed here.
4. Sizes the position via ``size_from_risk`` against the portfolio's
   current equity and ``config.risk_per_trade_pct``.
5. Routes through ``strategy.risk.validate_order`` for the position
   and total-exposure caps (same code path live uses), and computes
   the bracket stop price as the auto-computed value the risk module
   returns.
6. Calls ``sim.fills.simulate_fill`` to apply slippage and produce the
   ``SimulatedFill`` record.
7. Hands the fill to ``portfolio.record_entry`` (mutating the
   portfolio in place).

Flip decisions (``long+Sell`` / ``short+Buy``) close the existing
position first at the same fill-reference price (with slippage applied
to the EXIT side, opposite of the existing position's direction), with
``exit_reason="flip"``, then open the opposite side using the entry
path described above. Both legs share the same fill timestamp.

Rejections and skips are returned as a parallel ``tuple[RejectedEntry, ...]``
on the result so the eventual comparison report can count by reason
without re-parsing log lines (Rule 18 -- structured visibility, not
silent degradation).

Explicitly out of scope (deferred follow-up sub-tasks within #14's
neighborhood):

- Stop-checking against each subsequent bar's low (long) or high
  (short). That is a per-bar loop, not a per-tick loop.
- End-of-day flatten at 15:55 for positions still open after the last
  tick of the day. A one-time pass over ``portfolio.positions`` at the
  close.
- Mark-to-market intermediate equity updates. The risk-gate equity
  approximation used here marks all open positions at their entry
  price; once MTM is wired, the risk gate will see live equity.

Caller contract: ``decisions`` must be in chronological order (which
is what ``run_day_ticks`` returns by construction). The function
mutates ``portfolio`` in place AND returns the result -- the result
is the manifest of what got fills versus what got rejected; the
portfolio carries the resulting state.

Status: M2.2 sub-task #14 -- fully implemented (entries + flips only;
stops/EOD/MTM are follow-ups).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.tick_loop import TickDecision
from sim.fills import SimulatedFill, apply_slippage, simulate_fill
from sim.portfolio import SimulatedPortfolio
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
    """Manifest of one day's fill simulation.

    ``fills`` lists every successful entry (in chronological order).
    Flip exits are NOT in ``fills`` (they live on the closed-position
    side of the portfolio via ``record_exit`` with
    ``exit_reason="flip"``); only entries are first-class fills here.
    ``rejections`` captures Buy/Sell decisions that didn't transact.
    """

    fills: tuple[SimulatedFill, ...]
    rejections: tuple[RejectedEntry, ...]


# ---------------------------------------------------------------------------
# 5-min bar lookup
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
# Risk-module adapter
# ---------------------------------------------------------------------------


def _open_positions_for_risk(
    portfolio: SimulatedPortfolio,
) -> list[RiskPosition]:
    """Convert sim.portfolio.Position rows to strategy.risk.Position rows.

    ``strategy.risk.Position`` uses SIGNED quantity (positive long,
    negative short) and tracks ``current_price`` separately from
    ``avg_price``. Until per-tick mark-to-market lands (follow-up
    sub-task), ``current_price`` is approximated as ``entry_price``,
    which means the risk gate sees a slightly conservative
    "exposure-at-entry" view. Real intra-day equity awaits MTM wiring.
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


# ---------------------------------------------------------------------------
# Main entry point
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

    Raises:
        Nothing on normal data. Bad ``config.fill_at`` (not one of
        ``next_bar_open`` / ``current_close``) raises ValueError via
        ``_fill_reference`` -- but ``ReplayConfig.__post_init__``
        already validates this at construction time, so reaching it is
        a programmer bug, not a data condition.
    """
    fills: list[SimulatedFill] = []
    rejections: list[RejectedEntry] = []

    # Cache per-ticker 5-min frames so a multi-decision day on the same
    # ticker doesn't re-resample once per decision.
    five_min_by_ticker: dict[str, pd.DataFrame] = {}

    def _five_min_for(ticker: str) -> pd.DataFrame:
        if ticker not in five_min_by_ticker:
            tds = day_state.tickers.get(ticker)
            if tds is None:
                five_min_by_ticker[ticker] = pd.DataFrame()
            else:
                five_min_by_ticker[ticker] = _build_today_5min(
                    tds, day_state.trading_date
                )
        return five_min_by_ticker[ticker]

    def _attempt_entry(
        *,
        tick_et: datetime,
        ticker: str,
        side: Literal["buy", "sell"],
        bar_lookup: _BarLookup,
        decision_id: int,
    ) -> None:
        """Run sizing + risk gate + simulate_fill + record_entry.

        Mutates fills / rejections / portfolio from the enclosing scope
        rather than returning a value. Keeps the per-decision dispatch
        flat.
        """
        reference_price, fill_ts = _fill_reference(bar_lookup, config.fill_at)

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
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker, side=side,
                    requested_qty=0, reason="invalid_equity",
                )
            )
            return

        requested_qty = size_from_risk(
            account_equity=equity,
            entry_price=reference_price,
            stop_loss_pct=stop_pct,
            risk_per_trade_pct=config.risk_per_trade_pct,
        )
        if requested_qty <= 0:
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker, side=side,
                    requested_qty=0, reason="size_from_risk_zero",
                )
            )
            return

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
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker, side=side,
                    requested_qty=requested_qty, reason=check.reason,
                )
            )
            logger.info(
                "fill_simulator: %s @ %s %s qty=%d rejected: %s",
                ticker, tick_et, side, requested_qty, check.reason,
            )
            return

        # Compose timestamps consistent with the fill_at mode.
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
            # and next_bar_open is None. We've already filtered that
            # case before invoking _attempt_entry, so reaching here is
            # a defensive guard rather than a normal path.
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker, side=side,
                    requested_qty=check.quantity,
                    reason="simulate_fill_returned_none",
                )
            )
            return

        portfolio.record_entry(sf)
        fills.append(sf)

    # ---- Walk the decisions ----
    for idx, td in enumerate(decisions):
        decision_id = decision_id_start + idx
        action = td.decision.action
        ticker = td.ticker
        tick_et = td.tick_et

        if action == "Hold":
            continue

        # Bars first -- any failure to locate the fill bars produces a
        # structured rejection regardless of position state.
        five_min = _five_min_for(ticker)
        bar_lookup = _lookup_bars_at(five_min, tick_et)
        if isinstance(bar_lookup, str):
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker,
                    side=_entry_side(action),
                    requested_qty=0, reason=bar_lookup,
                )
            )
            continue

        if config.fill_at == "next_bar_open" and bar_lookup.next_open is None:
            rejections.append(
                RejectedEntry(
                    tick_et=tick_et, ticker=ticker,
                    side=_entry_side(action),
                    requested_qty=0, reason="no_next_bar_for_fill",
                )
            )
            continue

        existing = portfolio.get_position(ticker)
        if existing is not None:
            # No-op: same-direction
            if existing.side == "buy" and action == "Buy":
                continue
            if existing.side == "sell" and action == "Sell":
                continue
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
        _attempt_entry(
            tick_et=tick_et,
            ticker=ticker,
            side=_entry_side(action),
            bar_lookup=bar_lookup,
            decision_id=decision_id,
        )

    return FillSimulationResult(
        fills=tuple(fills),
        rejections=tuple(rejections),
    )


__all__ = [
    "FillSimulationResult",
    "RejectedEntry",
    "apply_decisions_to_portfolio",
]
