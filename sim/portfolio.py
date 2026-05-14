"""Simulated portfolio for the M2 replay harness.

``SimulatedPortfolio`` tracks cash, open positions, realized P&L, and
the running equity curve / max drawdown for one strategy variant
within a replay run. The harness runs two instances in parallel — one
for the LLM-driven decisions, one for the base rule-based decisions —
to produce the side-by-side comparison the design doc requires.

What this module DOES:

- Bookkeeping: cash balance, per-ticker positions, realized P&L,
  equity curve, max drawdown.
- Entry handling: record an entry given a ``SimulatedFill``.
- Exit handling: stop-out, take-profit, end-of-day flatten — each
  computes realized P&L and updates cash.
- Mark-to-market: given a price snapshot, compute current equity and
  update the equity curve + drawdown.

What this module DOES NOT do:

- Decide what to trade — that's ``signal_engine.evaluate`` + the base
  rule signals, called by the replay loop.
- Run risk validation — that's ``strategy.risk.validate_order``, also
  called by the replay loop before ``record_entry``.
- Track LLM decisions — the replay loop's SQLite layer does that.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Simulation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .fills import SimulatedFill


@dataclass(slots=True)
class Position:
    """One open position in the simulated portfolio.

    The portfolio always uses ``side="buy"`` for long, ``"sell"`` for
    short, mirroring ``SimulatedFill.side``. ``qty`` is always positive;
    direction lives in ``side``. This avoids the signed-qty footgun
    where ``qty * close`` accidentally double-signs the dollar value.
    """

    ticker: str
    side: Literal["buy", "sell"]
    qty: int  # positive
    entry_price: float
    entry_timestamp: datetime
    stop_price: float
    decision_id: int
    # Exit fields populated when the position closes.
    exit_price: float | None = None
    exit_timestamp: datetime | None = None
    exit_reason: str | None = None  # "stop_hit" | "eod_flatten" | "take_profit"
    realized_pl: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def signed_qty(self) -> int:
        """Return signed share count: +long, -short."""
        return self.qty if self.side == "buy" else -self.qty

    def unrealized_pl(self, current_price: float) -> float:
        """Mark-to-market unrealized P&L at ``current_price``.

        For longs: ``(current - entry) * qty``.
        For shorts: ``(entry - current) * qty``.
        """
        if self.side == "buy":
            return (current_price - self.entry_price) * self.qty
        return (self.entry_price - current_price) * self.qty


@dataclass(slots=True)
class EquityPoint:
    """One point on the equity curve."""

    timestamp: datetime
    equity: float
    cash: float
    n_open_positions: int


@dataclass(slots=True)
class SimulatedPortfolio:
    """Cash + positions bookkeeping for one strategy variant.

    Initialize with ``starting_cash``; entries/exits/mark-to-market
    drive the state forward. Equity curve is maintained as a list of
    ``EquityPoint`` so the comparison report can plot drawdown
    timelines.
    """

    starting_cash: float
    name: str = "portfolio"  # "llm" or "base", set by the replay loop
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    closed_positions: list[Position] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    realized_pl_total: float = 0.0
    peak_equity: float = field(init=False)
    max_drawdown: float = 0.0  # positive number; max equity drop from peak
    max_drawdown_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError(
                f"starting_cash must be > 0; got {self.starting_cash}"
            )
        self.cash = self.starting_cash
        self.peak_equity = self.starting_cash

    # ---- Position lookups ----

    def has_position(self, ticker: str) -> bool:
        return ticker in self.positions and self.positions[ticker].is_open

    def get_position(self, ticker: str) -> Position | None:
        pos = self.positions.get(ticker)
        if pos is None or not pos.is_open:
            return None
        return pos

    # ---- Entry / exit ----

    def record_entry(self, fill: SimulatedFill) -> None:
        """Open a new position from a SimulatedFill.

        Deducts ``fill.qty * fill.fill_price`` from cash (long entry)
        or credits it (short entry — the cash from the short sale).
        For shorts, the realized P&L on exit will reflect the price
        decline.

        Raises:
            ValueError: portfolio already has an open position in this
                ticker (the harness must close before re-entering).
        """
        if self.has_position(fill.ticker):
            raise ValueError(
                f"portfolio already has open position in {fill.ticker}; "
                "close before re-entering"
            )
        notional = fill.qty * fill.fill_price
        if fill.side == "buy":
            self.cash -= notional
        else:  # short sell: cash credited
            self.cash += notional

        self.positions[fill.ticker] = Position(
            ticker=fill.ticker,
            side=fill.side,
            qty=fill.qty,
            entry_price=fill.fill_price,
            entry_timestamp=fill.fill_timestamp,
            stop_price=fill.stop_price,
            decision_id=fill.decision_id,
        )

    def record_exit(
        self,
        ticker: str,
        *,
        exit_price: float,
        exit_timestamp: datetime,
        exit_reason: str,
    ) -> float:
        """Close an open position at ``exit_price``.

        For longs: cash += qty * exit_price; realized = (exit - entry) * qty.
        For shorts: cash -= qty * exit_price; realized = (entry - exit) * qty.

        Moves the position from ``positions`` to ``closed_positions``,
        updates ``realized_pl_total``, and returns the realized P&L
        on this exit.

        Raises:
            KeyError: no open position in ``ticker``.
        """
        pos = self.positions.get(ticker)
        if pos is None or not pos.is_open:
            raise KeyError(f"no open position in {ticker}")

        if pos.side == "buy":
            realized = (exit_price - pos.entry_price) * pos.qty
            self.cash += pos.qty * exit_price
        else:
            realized = (pos.entry_price - exit_price) * pos.qty
            self.cash -= pos.qty * exit_price

        pos.exit_price = exit_price
        pos.exit_timestamp = exit_timestamp
        pos.exit_reason = exit_reason
        pos.realized_pl = realized
        self.realized_pl_total += realized

        self.closed_positions.append(pos)
        del self.positions[ticker]
        return realized

    # ---- Mark to market + equity tracking ----

    def equity(self, prices: dict[str, float]) -> float:
        """Compute current portfolio equity given a price snapshot.

        Equity = cash + sum over open positions of position contribution,
        where:

        - Long: contribution = ``qty * current_price``. Cash already has
          ``-qty * entry_price`` deducted at entry, so the long's net
          contribution to equity is its current market value.
        - Short: contribution = ``-qty * current_price``. Cash already
          has ``+qty * entry_price`` credited at entry from the short
          sale; the position is a liability worth ``qty * current_price``
          at the current quote.

        Verification:

        - Long 100 @ 150, starting cash 100k: cash -> 85k. At cp=150,
          equity = 85k + 100*150 = 100k. At cp=160, equity = 85k + 16k
          = 101k (unrealized +1k).
        - Short 100 @ 150, starting cash 100k: cash -> 115k. At cp=150,
          equity = 115k - 100*150 = 100k. At cp=140, equity = 115k -
          14k = 101k (unrealized +1k).

        Tickers missing from ``prices`` are valued at their entry
        price (treated as "no fresh quote"). This is rare in replay
        but possible if a bar series has a gap.
        """
        total = self.cash
        for ticker, pos in self.positions.items():
            if not pos.is_open:
                continue
            cp = prices.get(ticker, pos.entry_price)
            if pos.side == "buy":
                total += pos.qty * cp
            else:
                # Short: liability at current price. Cash already
                # reflects the entry credit; the open position's
                # contribution is the negative of its current
                # buy-to-cover cost.
                total -= pos.qty * cp
        return total

    def mark_to_market(
        self,
        timestamp: datetime,
        prices: dict[str, float],
    ) -> EquityPoint:
        """Compute equity, append to curve, update peak + drawdown.

        Returns the appended ``EquityPoint`` for caller convenience.
        """
        eq = self.equity(prices)
        if eq > self.peak_equity:
            self.peak_equity = eq
        drawdown = self.peak_equity - eq
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
            self.max_drawdown_at = timestamp
        point = EquityPoint(
            timestamp=timestamp,
            equity=eq,
            cash=self.cash,
            n_open_positions=len(self.positions),
        )
        self.equity_curve.append(point)
        return point

    # ---- Stop-loss checks ----

    def check_stops(
        self,
        timestamp: datetime,
        bar_lows: dict[str, float],
        bar_highs: dict[str, float],
    ) -> list[tuple[str, float]]:
        """Detect stop-outs during a bar.

        For each open position:
          - long: stop triggered if ``bar_lows[ticker] <= stop_price``.
          - short: stop triggered if ``bar_highs[ticker] >= stop_price``.

        Returns list of (ticker, fill_price) for positions that were
        closed. Fill price is the stop price itself — stops are
        treated as market orders that fill at the trigger (no
        additional slippage, per design doc § Simulation: fills and
        slippage). Caller is expected to invoke ``record_exit`` with
        ``exit_reason="stop_hit"``.

        Returning the list rather than directly mutating gives the
        replay loop ordering control (it can log decisions in order
        of stop_hit then re-entry).
        """
        triggered: list[tuple[str, float]] = []
        for ticker, pos in list(self.positions.items()):
            if not pos.is_open:
                continue
            if pos.side == "buy":
                low = bar_lows.get(ticker)
                if low is not None and low <= pos.stop_price:
                    triggered.append((ticker, pos.stop_price))
            else:
                high = bar_highs.get(ticker)
                if high is not None and high >= pos.stop_price:
                    triggered.append((ticker, pos.stop_price))
        return triggered

    # ---- Snapshot ----

    def snapshot(self) -> dict[str, float | int]:
        """Return a serializable dict of the portfolio state.

        Useful for inclusion in the replay run's metadata JSON.
        """
        return {
            "name": self.name,
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "n_open_positions": len(self.positions),
            "n_closed_positions": len(self.closed_positions),
            "realized_pl_total": self.realized_pl_total,
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
        }
