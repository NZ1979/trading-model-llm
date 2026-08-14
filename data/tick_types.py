"""Tick-level types for the microstructure feed (spec v4).

Separate from `bar_types.py` deliberately: bars are the 5-min signal path's
input and are consumed by `main.py`; ticks are the microstructure daemon's
input and are consumed by `analysis/microstructure.py` and the MCP server.
Keeping them apart means a change here cannot break the live signal path.

Timestamp convention
--------------------
Alpaca emits RFC-3339 with up to nanosecond precision
("2026-08-14T19:51:44.208123456Z"). Python's `datetime` only resolves to
microseconds, so round-tripping a tape through it silently collapses prints
that are nanoseconds apart into equal timestamps — which breaks the ordering
the Lee-Ready tick test depends on. These types therefore carry `ts_ns` as an
integer count of nanoseconds since the Unix epoch: lossless, sortable, and
directly storable in SQLite's INTEGER column. `as_datetime()` exists for
display and interop only, and truncates to microseconds by construction —
never sort or dedupe on its result.

Sizes are in SHARES throughout. This differs from Schwab's level one, where
bid/ask sizes are in lots — that conversion belongs in the Schwab adapter,
not here, so nothing downstream has to remember which vendor a size came
from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class Trade:
    """A single executed print from the consolidated tape."""

    symbol: str
    ts_ns: int  # nanoseconds since epoch, UTC
    price: float
    size: int  # shares
    exchange: str  # single-char Alpaca exchange code
    conditions: tuple[str, ...]  # trade condition codes; see Alpaca reference
    tape: str  # 'A' NYSE, 'B' NYSE Arca/regional, 'C' Nasdaq
    trade_id: int | None

    def as_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1e9, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class Quote:
    """An NBBO update from the consolidated tape."""

    symbol: str
    ts_ns: int
    bid_price: float
    bid_size: int  # shares
    bid_exchange: str
    ask_price: float
    ask_size: int  # shares
    ask_exchange: str
    conditions: tuple[str, ...]
    tape: str

    def as_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1e9, tz=timezone.utc)

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def midpoint(self) -> float:
        return (self.ask_price + self.bid_price) / 2.0


@dataclass(frozen=True, slots=True)
class TradingStatus:
    """Halt / resume / LULD pause notification.

    Consumed so pace and flow windows spanning a halt can be excluded rather
    than silently computed across a gap (spec v4 §10).
    """

    symbol: str
    ts_ns: int
    status_code: str  # e.g. 'H' halted, 'T' trading resumed
    status_message: str
    reason_code: str
    reason_message: str
    tape: str

    def as_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ns / 1e9, tz=timezone.utc)

    @property
    def is_halt(self) -> bool:
        """True when this message indicates trading is NOT occurring.

        Deliberately conservative: anything that is not an explicit resume is
        treated as a halt, so an unrecognized code suppresses metrics rather
        than silently letting them compute across a gap (Rule 18 — degrade
        visibly, never silently).
        """
        return self.status_code.upper() not in ("T", "Q", "R")
