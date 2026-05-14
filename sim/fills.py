"""Fill simulation for the M2 replay harness.

The harness simulates broker fills given historical bars; production
fills come from Alpaca and are observed, not simulated. Replay's job
is to make the simulation honest enough that conclusions transfer.

Fill modes (per ``ReplayConfig.fill_at``):

- ``next_bar_open``: decision at tick T fills at tick T+1's open price
  plus slippage. The recommended default — most realistic for a
  signal-then-place workflow where the bracket order goes in after
  the decision is made.
- ``current_close``: decision at tick T fills at tick T's close plus
  slippage. Optimistic; useful for "best case" replay sensitivity.

Slippage is a single scalar (bps) applied symmetrically: buys fill
above mid, sells below. This is the simplest honest model; the design
doc § "Backtest credibility checklist" notes a richer model (worse of
bar open + half-spread / first-30s VWAP) is a Phase 5 followup.

Stop-loss fills are handled separately by the portfolio's
mark-to-market loop (each bar's low for longs, high for shorts) and
do not call ``simulate_fill``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One simulated broker fill — entry only.

    Exit fills (stop-hit, end-of-day flatten, take-profit) are recorded
    as ``Position.exit_*`` fields on the portfolio side, not as
    standalone ``SimulatedFill`` rows, since exits don't need
    slippage/timing simulation (stops are market orders that fill at
    the stop trigger price by definition; EOD flattens fill at the
    final bar close).
    """

    ticker: str
    side: Literal["buy", "sell"]  # entry direction; long=buy, short=sell
    qty: int  # positive (direction is in ``side``)
    fill_price: float
    fill_timestamp: datetime  # tz-aware America/New_York
    stop_price: float
    decision_id: int  # foreign key into replay_decisions table


def apply_slippage(reference_price: float, side: str, slippage_bps: float) -> float:
    """Adjust a reference price for slippage.

    Buys fill ABOVE the reference (worse for the buyer); sells fill
    BELOW (worse for the seller). Slippage in basis points: 5 bps =
    0.05% = ``reference_price * 0.0005``.

    Pure function, no I/O. Tested in M2.1.e.
    """
    if slippage_bps < 0:
        raise ValueError(f"slippage_bps must be >= 0; got {slippage_bps}")
    delta = reference_price * (slippage_bps / 10_000.0)
    if side == "buy":
        return reference_price + delta
    if side == "sell":
        return reference_price - delta
    raise ValueError(f"side must be 'buy' or 'sell'; got {side!r}")


def simulate_fill(
    *,
    ticker: str,
    side: Literal["buy", "sell"],
    qty: int,
    decision_id: int,
    fill_at: Literal["next_bar_open", "current_close"],
    current_bar_close: float,
    next_bar_open: float | None,
    next_bar_timestamp: datetime | None,
    current_bar_timestamp: datetime,
    stop_price: float,
    slippage_bps: float,
) -> SimulatedFill | None:
    """Build a SimulatedFill given the bars surrounding the decision.

    Returns ``None`` when:
      - ``fill_at="next_bar_open"`` and ``next_bar_open is None`` (the
        decision fired on the last bar of the day; there is no next
        bar to fill against, and per the design doc EOD flattens
        handle this case at the portfolio level — we don't enter a
        position that would immediately flatten).

    Args:
        ticker: equity symbol.
        side: ``"buy"`` for long entries, ``"sell"`` for short entries.
        qty: positive share count.
        decision_id: foreign key into ``replay_decisions``.
        fill_at: ``"next_bar_open"`` or ``"current_close"``.
        current_bar_close: close price of the decision bar.
        next_bar_open: open price of the next bar (None if no next bar).
        next_bar_timestamp: timestamp of the next bar (None if no next).
        current_bar_timestamp: timestamp of the decision bar.
        stop_price: pre-computed stop level from risk module.
        slippage_bps: bps to apply to the reference fill price.

    Returns:
        SimulatedFill on success, None if the entry must be skipped.

    Raises:
        ValueError: qty <= 0, slippage_bps < 0, bad side, bad fill_at.
    """
    if qty <= 0:
        raise ValueError(f"qty must be > 0; got {qty}")
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell'; got {side!r}")

    if fill_at == "next_bar_open":
        if next_bar_open is None or next_bar_timestamp is None:
            return None
        reference_price = next_bar_open
        fill_ts = next_bar_timestamp
    elif fill_at == "current_close":
        reference_price = current_bar_close
        fill_ts = current_bar_timestamp
    else:
        raise ValueError(
            "fill_at must be 'next_bar_open' or 'current_close'; "
            f"got {fill_at!r}"
        )

    fill_price = apply_slippage(reference_price, side, slippage_bps)
    return SimulatedFill(
        ticker=ticker,
        side=side,
        qty=qty,
        fill_price=fill_price,
        fill_timestamp=fill_ts,
        stop_price=stop_price,
        decision_id=decision_id,
    )
