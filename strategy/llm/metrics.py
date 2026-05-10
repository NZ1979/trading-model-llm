"""Pure-deterministic metrics for shadow analytics + Calmar tracking.

All functions in this module are stateless and side-effect-free. They take
recorded data (decisions, bars, equity series) and return computed metrics.
The shadow_outcomes follower / backfill processor calls these functions
to populate the table; the operator calls them to compute portfolio-level
performance summaries (Calmar, max drawdown, realized R per bucket).

Why pure functions: the metrics math is the foundation that every v2
performance question rests on. Bugs here propagate to every analysis we
ever do. Pure-function implementations are testable in isolation against
synthetic data with hand-computed expected values; that's the only way to
trust the math.

References:
- docs/LLM_MODEL_V2_REFINEMENTS.md § A.2 (Shadow analytics)
- docs/LLM_MODEL_V2_REFINEMENTS.md § 0 (Calmar as the primary metric)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Bar:
    """Single bar with OHLCV. Timestamps in epoch seconds (UTC)."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """Computed outcome for a single decision.

    All percentage fields are in percent (e.g., 1.5 = 1.5%, not 0.015).
    Sign convention:
      - Forward returns: positive when price moves IN FAVOR of the decision.
        For Buy/Hold, that's price going up; for Sell, price going down.
      - MAE: always <= 0 (adverse). Worst against-direction excursion.
      - MFE: always >= 0 (favorable). Best in-direction excursion.

    For Hold decisions, returns and excursions are computed as if the
    decision had been a Buy at decision_price (so we can measure the
    *cost of not trading*). stop/target fields are None for Holds.
    """

    decision_id: int

    # Forward returns at fixed horizons (% of decision price, sign-adjusted to direction)
    return_5m_pct: float | None
    return_15m_pct: float | None
    return_30m_pct: float | None
    return_60m_pct: float | None
    return_eod_pct: float | None

    # Excursion (% of decision price)
    mae_pct: float | None        # <= 0
    mfe_pct: float | None        # >= 0
    mae_at_minutes: int | None
    mfe_at_minutes: int | None

    # Would-stop / would-target simulation
    stop_would_hit: bool
    stop_hit_at_minutes: int | None
    target_would_hit: bool
    target_hit_at_minutes: int | None
    first_touch: str             # 'stop' | 'target' | 'neither' | 'n/a'

    # Liquidity proxy
    avg_spread_bps: float | None
    estimated_slippage_bps: float | None

    # Metadata
    horizon_complete: str        # '5m' | '15m' | '30m' | '60m' | 'eod' | 'final'


# ----------------------------------------------------------------------
# compute_outcome — the workhorse
# ----------------------------------------------------------------------


def compute_outcome(
    decision_id: int,
    decision_ts: float,
    decision_price: float,
    side: str,
    stop_price: float | None,
    target_price: float | None,
    bars: Sequence[Bar],
    eod_ts: float,
) -> ShadowOutcome:
    """Compute forward returns, MAE/MFE, and stop/target touches.

    Args:
        decision_id: FK to the decisions table row.
        decision_ts: epoch seconds of the decision.
        decision_price: the price at which we'd have entered (limit price for
            Buy/Sell, observed close for Hold).
        side: 'buy' | 'sell' | 'hold'. Determines sign convention.
        stop_price: stop-loss trigger. None for Hold or when not set.
        target_price: take-profit trigger. None when no TP attached.
        bars: 1-min bars sorted ascending, starting at or after decision_ts.
            The function expects bars covering up to 60 minutes after decision.
        eod_ts: epoch seconds for end-of-day cutoff (15:55 ET typically).

    Returns:
        ShadowOutcome with the horizon-relevant fields populated. Fields
        whose horizon hasn't completed are None.
    """
    if not bars:
        return _empty_outcome(decision_id, side, "no_bars")

    # Effective direction: +1 means "in favor when price goes up", -1 the opposite.
    # Hold is treated as if it were a Buy for measurement purposes.
    direction = -1 if side == "sell" else +1

    # Filter to bars at or after decision_ts (defensive)
    relevant = [b for b in bars if b.ts >= decision_ts]
    if not relevant:
        return _empty_outcome(decision_id, side, "no_bars_after_decision")

    # Walk bars to compute MAE/MFE, stop/target touches, and forward returns.
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_at_minutes: int = 0
    mfe_at_minutes: int = 0

    stop_hit_at_minutes: int | None = None
    target_hit_at_minutes: int | None = None
    first_touch = "n/a" if side == "hold" else "neither"

    horizon_returns: dict[int, float | None] = {5: None, 15: None, 30: None, 60: None}
    eod_return: float | None = None

    for bar in relevant:
        elapsed_min = int((bar.ts - decision_ts) / 60)

        # Adverse and favorable price extremes within this bar
        if direction == +1:
            adverse_price = bar.low
            favorable_price = bar.high
        else:
            adverse_price = bar.high
            favorable_price = bar.low

        # MAE: worst adverse excursion as a signed percentage of entry.
        # Always <= 0 by construction.
        bar_mae_pct = direction * (adverse_price - decision_price) / decision_price * 100.0
        if bar_mae_pct < mae_pct:
            mae_pct = bar_mae_pct
            mae_at_minutes = elapsed_min

        # MFE: best favorable excursion as a signed percentage. Always >= 0.
        bar_mfe_pct = direction * (favorable_price - decision_price) / decision_price * 100.0
        if bar_mfe_pct > mfe_pct:
            mfe_pct = bar_mfe_pct
            mfe_at_minutes = elapsed_min

        # Stop / target touches (only for Buy/Sell, not Hold).
        # For a long, stop hits when low <= stop_price, target when high >= target_price.
        # For a short, stop hits when high >= stop_price, target when low <= target_price.
        if side != "hold":
            if stop_price is not None and stop_hit_at_minutes is None:
                if (direction == +1 and bar.low <= stop_price) or \
                   (direction == -1 and bar.high >= stop_price):
                    stop_hit_at_minutes = elapsed_min
                    if first_touch == "neither":
                        first_touch = "stop"
            if target_price is not None and target_hit_at_minutes is None:
                if (direction == +1 and bar.high >= target_price) or \
                   (direction == -1 and bar.low <= target_price):
                    target_hit_at_minutes = elapsed_min
                    if first_touch == "neither":
                        first_touch = "target"

        # Forward returns at fixed horizons. Use the close of the bar that
        # CONTAINS the horizon timestamp (i.e., the last bar with elapsed_min
        # <= horizon_min and the next bar's elapsed > horizon_min). Simpler:
        # record the close at any bar where elapsed_min == horizon_min. If no
        # exact match, record at the first bar with elapsed_min > horizon_min.
        for h in horizon_returns:
            if horizon_returns[h] is None and elapsed_min >= h:
                horizon_returns[h] = direction * (bar.close - decision_price) / decision_price * 100.0

        # End-of-day return: last bar at or before eod_ts
        if bar.ts <= eod_ts:
            eod_return = direction * (bar.close - decision_price) / decision_price * 100.0

    # Determine horizon_complete based on the last bar we saw.
    last_elapsed_min = int((relevant[-1].ts - decision_ts) / 60)
    if relevant[-1].ts >= eod_ts:
        horizon_complete = "final"
    elif last_elapsed_min >= 60:
        horizon_complete = "60m"
    elif last_elapsed_min >= 30:
        horizon_complete = "30m"
    elif last_elapsed_min >= 15:
        horizon_complete = "15m"
    elif last_elapsed_min >= 5:
        horizon_complete = "5m"
    else:
        horizon_complete = "partial"

    # Liquidity proxy: rough spread estimate from bar high-low ranges.
    # In a reasonably-tight stock, the typical 1-min H-L is ~5-15 bps;
    # wider markets (illiquid names, crisis hours) widen to 50+ bps.
    # This is a coarse proxy; real spread estimation needs quote-level data.
    spread_samples = [
        (b.high - b.low) / b.close * 10000.0  # bps
        for b in relevant[:60] if b.close > 0
    ]
    avg_spread_bps = sum(spread_samples) / len(spread_samples) if spread_samples else None
    # Slippage estimate: half the typical spread (we usually fill near mid).
    estimated_slippage_bps = avg_spread_bps / 2.0 if avg_spread_bps is not None else None

    return ShadowOutcome(
        decision_id=decision_id,
        return_5m_pct=horizon_returns[5],
        return_15m_pct=horizon_returns[15],
        return_30m_pct=horizon_returns[30],
        return_60m_pct=horizon_returns[60],
        return_eod_pct=eod_return,
        mae_pct=mae_pct if mae_pct < 0 else 0.0,
        mfe_pct=mfe_pct if mfe_pct > 0 else 0.0,
        mae_at_minutes=mae_at_minutes,
        mfe_at_minutes=mfe_at_minutes,
        stop_would_hit=stop_hit_at_minutes is not None,
        stop_hit_at_minutes=stop_hit_at_minutes,
        target_would_hit=target_hit_at_minutes is not None,
        target_hit_at_minutes=target_hit_at_minutes,
        first_touch=first_touch,
        avg_spread_bps=avg_spread_bps,
        estimated_slippage_bps=estimated_slippage_bps,
        horizon_complete=horizon_complete,
    )


def _empty_outcome(decision_id: int, side: str, reason: str) -> ShadowOutcome:
    """Return a fully-null outcome when bars are missing or unusable."""
    return ShadowOutcome(
        decision_id=decision_id,
        return_5m_pct=None, return_15m_pct=None, return_30m_pct=None,
        return_60m_pct=None, return_eod_pct=None,
        mae_pct=None, mfe_pct=None,
        mae_at_minutes=None, mfe_at_minutes=None,
        stop_would_hit=False, stop_hit_at_minutes=None,
        target_would_hit=False, target_hit_at_minutes=None,
        first_touch="n/a" if side == "hold" else "neither",
        avg_spread_bps=None, estimated_slippage_bps=None,
        horizon_complete=reason,
    )


# ----------------------------------------------------------------------
# Calmar / drawdown / realized-R
# ----------------------------------------------------------------------


def compute_max_drawdown(
    equity_curve: Sequence[tuple[date, float]],
) -> tuple[float, date | None, date | None]:
    """Return (max_drawdown_pct, peak_date, trough_date) from a daily equity series.

    max_drawdown_pct is positive (e.g., 12.5 means a 12.5% drawdown). Returns
    (0.0, None, None) for an empty or single-point series.

    The series should be sorted ascending by date. If unsorted, we sort first.
    """
    if len(equity_curve) < 2:
        return (0.0, None, None)

    sorted_curve = sorted(equity_curve, key=lambda x: x[0])

    peak_value = sorted_curve[0][1]
    peak_date_at_peak = sorted_curve[0][0]
    max_dd_pct = 0.0
    max_dd_peak: date | None = None
    max_dd_trough: date | None = None

    for d, v in sorted_curve:
        if v > peak_value:
            peak_value = v
            peak_date_at_peak = d
        drawdown_pct = (peak_value - v) / peak_value * 100.0
        if drawdown_pct > max_dd_pct:
            max_dd_pct = drawdown_pct
            max_dd_peak = peak_date_at_peak
            max_dd_trough = d

    return (max_dd_pct, max_dd_peak, max_dd_trough)


def compute_calmar(
    equity_curve: Sequence[tuple[date, float]],
    window_days: int = 90,
    trading_days_per_year: int = 252,
) -> float | None:
    """Calmar ratio = annualized return ÷ max drawdown over the trailing window.

    Args:
        equity_curve: daily (date, equity) tuples, sorted ascending. The
            series should reflect end-of-day equity for each trading day.
        window_days: how many days back to consider. Default 90 (the V2 doc's
            primary objective horizon).
        trading_days_per_year: annualization factor. 252 is standard for US equities.

    Returns:
        Calmar ratio as a dimensionless float, or None when the series is too
        short or contains no drawdown (returns infinity in math terms; we
        return None so callers don't divide-by-zero downstream).
    """
    if len(equity_curve) < 2:
        return None

    sorted_curve = sorted(equity_curve, key=lambda x: x[0])

    # Trim to the trailing window
    cutoff = sorted_curve[-1][0] - timedelta(days=window_days)
    window = [(d, v) for d, v in sorted_curve if d >= cutoff]
    if len(window) < 2:
        return None

    # Annualized return: (end / start)^(252 / N) - 1 where N is the number of
    # trading days in the window.
    start_eq = window[0][1]
    end_eq = window[-1][1]
    n_days = len(window)
    if start_eq <= 0 or n_days < 2:
        return None
    period_return = end_eq / start_eq
    if period_return <= 0:
        return None
    annualized_return = period_return ** (trading_days_per_year / n_days) - 1.0

    max_dd_pct, _, _ = compute_max_drawdown(window)
    if max_dd_pct <= 0:
        # No drawdown observed in the window — Calmar is infinity in math, but
        # we return None so downstream code doesn't have to handle inf.
        return None

    return annualized_return * 100.0 / max_dd_pct


def compute_realized_r(
    decision_price: float,
    side: str,
    stop_price: float | None,
    target_price: float | None,
    take_profit_atr_multiple: float | None,
    stop_loss_atr_multiple: float | None,
    outcome: ShadowOutcome,
) -> float | None:
    """Realized R-multiple for a single decision.

    R = realized P&L expressed in units of the planned stop distance. -1 means
    the position took a full stop-loss; +2 means it gained twice what the stop
    risked. The bucket-expectancy SQL aggregates this across decisions in the
    same bucket to compute expected_R.

    Logic:
      - If stop hit first: realized_r = -1.0
      - If target hit first: realized_r = take_profit_atr / stop_loss_atr (the
        designed R/R)
      - If neither hit: realized_r = (eod_return / stop_distance_pct), where
        stop_distance_pct is the planned stop distance from entry as a
        percentage (so a -2% return with a 1% planned stop = -2.0R)
      - For Hold or when stop_price is None: returns None (no R-multiple
        meaningful)
    """
    if side == "hold" or stop_price is None:
        return None
    if outcome.first_touch == "stop":
        return -1.0
    if outcome.first_touch == "target":
        if take_profit_atr_multiple is not None and stop_loss_atr_multiple is not None and stop_loss_atr_multiple > 0:
            return take_profit_atr_multiple / stop_loss_atr_multiple
        # Fallback: derive R from the realized prices directly.
        if target_price is not None:
            stop_dist = abs(decision_price - stop_price)
            target_dist = abs(target_price - decision_price)
            return target_dist / stop_dist if stop_dist > 0 else None
        return None
    # Neither stop nor target hit — use EOD return scaled by planned stop distance.
    if outcome.return_eod_pct is None:
        return None
    stop_dist_pct = abs(decision_price - stop_price) / decision_price * 100.0
    if stop_dist_pct <= 0:
        return None
    return outcome.return_eod_pct / stop_dist_pct
