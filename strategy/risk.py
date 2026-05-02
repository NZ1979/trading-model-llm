"""Pre-trade risk validation.

Every order placed by the platform must pass validate_order() before reaching
the broker. Three independent checks (any failure rejects the order):

  1. Single-position cap. New position notional <= max_position_pct of equity.
     Default 20% per spec. Prevents one bad trade blowing up the account.

  2. Total-exposure cap. Sum of all open position notionals (existing + new)
     <= max_total_exposure_pct of equity. Default 90%. Reserves headroom for
     stop-loss slippage and intraday margin spikes.

  3. Stop-loss sanity. The platform always attaches a stop-loss order, sized
     by stop_loss_pct (default 2%). validate_order computes the stop price and
     returns it alongside the validation result; main.py uses it when placing
     the bracket order.

Why these particular numbers?
  20%/90%/2% match the original Phase 6 spec and are conservative for an
  intraday system. They can be tuned in settings.yaml without code changes.
  More aggressive sizing belongs in a separate risk profile, not in defaults.

This module is a pure function - no I/O, no async. Easy to unit-test and
easy to call from anywhere in the order pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class Position:
    """Snapshot of an existing open position used for exposure math."""
    ticker: str
    quantity: int        # positive = long, negative = short
    avg_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return abs(self.quantity) * self.current_price


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    """Outcome of validate_order. If approved=False, reason explains why."""
    approved: bool
    quantity: int                  # final size (may be < requested if scaled down)
    stop_price: float | None       # auto-computed stop, None if rejected
    reason: str
    position_pct: float            # this order's notional as % of equity
    total_exposure_pct: float      # post-fill total exposure as % of equity


def validate_order(
    ticker: str,
    side: Side,
    requested_quantity: int,
    current_price: float,
    account_equity: float,
    open_positions: list[Position],
    max_position_pct: float = 20.0,
    max_total_exposure_pct: float = 90.0,
    stop_loss_pct: float = 2.0,
    scale_down_if_oversized: bool = True,
) -> RiskCheckResult:
    """Validate a proposed order against position and exposure limits.

    Args:
        ticker: symbol being traded.
        side: "buy" (long entry or short cover) or "sell" (short entry or long exit).
        requested_quantity: shares requested by signal engine. Must be > 0.
        current_price: latest mark price for sizing.
        account_equity: total account equity in dollars.
        open_positions: list of Position dataclasses for all currently held names.
        max_position_pct: max single-position notional as % of equity (default 20).
        max_total_exposure_pct: max total notional as % of equity (default 90).
        stop_loss_pct: stop-loss distance as % of entry (default 2).
        scale_down_if_oversized: if True (default), oversized orders are reduced
            to the largest allowed quantity rather than rejected outright.

    Returns:
        RiskCheckResult. If approved, quantity is the final (possibly reduced)
        size and stop_price is the auto-computed stop-loss trigger.
    """
    if requested_quantity <= 0:
        return _reject("invalid_quantity", current_price, side, stop_loss_pct,
                       0.0, 0.0)
    if current_price <= 0:
        return _reject("invalid_price", current_price, side, stop_loss_pct,
                       0.0, 0.0)
    if account_equity <= 0:
        return _reject("invalid_equity", current_price, side, stop_loss_pct,
                       0.0, 0.0)

    requested_notional = requested_quantity * current_price

    # ---- Check 1: single-position cap ----
    max_position_notional = account_equity * (max_position_pct / 100.0)
    if requested_notional > max_position_notional:
        if not scale_down_if_oversized:
            return _reject(
                f"position_cap_exceeded "
                f"(${requested_notional:,.0f} > ${max_position_notional:,.0f})",
                current_price, side, stop_loss_pct,
                requested_notional / account_equity * 100,
                _total_exposure_pct(open_positions, account_equity),
            )
        # Scale down to fit
        scaled_qty = int(max_position_notional // current_price)
        if scaled_qty <= 0:
            return _reject(
                "position_cap_below_one_share",
                current_price, side, stop_loss_pct, 0.0,
                _total_exposure_pct(open_positions, account_equity),
            )
        requested_quantity = scaled_qty
        requested_notional = scaled_qty * current_price

    # ---- Check 2: total exposure cap ----
    # An order that adds to an existing position increases exposure.
    # An order that reduces or closes a position decreases it.
    # We compute net change in total exposure and check the post-fill total.
    current_total = sum(p.market_value for p in open_positions)
    existing = next((p for p in open_positions if p.ticker == ticker), None)

    if existing is None:
        # New position: full notional adds to exposure
        new_total = current_total + requested_notional
    else:
        # Direction matters
        is_adding = (
            (side == "buy" and existing.quantity > 0)
            or (side == "sell" and existing.quantity < 0)
        )
        if is_adding:
            new_total = current_total + requested_notional
        else:
            # Reducing/closing: subtract up to the existing position's value
            offset = min(requested_notional, existing.market_value)
            new_total = current_total - offset
            if requested_notional > existing.market_value:
                # Flipped through zero: residual opens opposite side
                residual = requested_notional - existing.market_value
                new_total += residual

    max_total = account_equity * (max_total_exposure_pct / 100.0)
    if new_total > max_total:
        return _reject(
            f"total_exposure_cap_exceeded "
            f"(${new_total:,.0f} > ${max_total:,.0f})",
            current_price, side, stop_loss_pct,
            requested_notional / account_equity * 100,
            new_total / account_equity * 100,
        )

    # ---- All checks passed: compute stop-loss ----
    stop_price = _compute_stop(side, current_price, stop_loss_pct)

    return RiskCheckResult(
        approved=True,
        quantity=requested_quantity,
        stop_price=stop_price,
        reason="ok",
        position_pct=requested_notional / account_equity * 100,
        total_exposure_pct=new_total / account_equity * 100,
    )


def _compute_stop(side: Side, entry_price: float, stop_pct: float) -> float:
    """Stop is below entry for longs (buy), above entry for shorts (sell)."""
    delta = entry_price * (stop_pct / 100.0)
    return round(entry_price - delta, 2) if side == "buy" else round(entry_price + delta, 2)


def _total_exposure_pct(positions: list[Position], equity: float) -> float:
    if equity <= 0:
        return 0.0
    return sum(p.market_value for p in positions) / equity * 100


def _reject(
    reason: str, price: float, side: Side, stop_pct: float,
    position_pct: float, total_exposure_pct: float,
) -> RiskCheckResult:
    return RiskCheckResult(
        approved=False, quantity=0, stop_price=None, reason=reason,
        position_pct=position_pct, total_exposure_pct=total_exposure_pct,
    )


def size_from_risk(
    account_equity: float,
    entry_price: float,
    stop_loss_pct: float,
    risk_per_trade_pct: float = 0.5,
) -> int:
    """Helper: size a position so loss-at-stop = risk_per_trade_pct of equity.

    Example: equity=$100k, entry=$200, stop_pct=2% (= $4 risk per share),
    risk_per_trade=0.5% (= $500 max loss). Quantity = $500 / $4 = 125 shares.

    This sizes by RISK rather than by notional. The position-cap check above
    will further constrain if the resulting notional exceeds 20% of equity.
    """
    if entry_price <= 0 or stop_loss_pct <= 0 or account_equity <= 0:
        return 0
    risk_per_share = entry_price * (stop_loss_pct / 100.0)
    max_loss = account_equity * (risk_per_trade_pct / 100.0)
    return max(1, int(max_loss // risk_per_share))
