"""Shared bar types used across data providers and the aggregator.

Convention: `timestamp` is the START of the bar window in UTC. A MinuteBar
with timestamp 14:30:00 UTC covers 14:30:00 -> 14:31:00 UTC (= 9:30-9:31 ET
during EST). This matches Alpaca's native convention; we normalize Polygon's
end-timestamps to start-timestamps when ingesting.

Why start-timestamps: window math is simpler. The 5-min window starting at
9:30 contains 1-min bars at 9:30, 9:31, 9:32, 9:33, 9:34. The bar at 9:35
belongs to the next window. Floor-divide-by-5 on the minute field gives the
window directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MinuteBar:
    symbol: str
    timestamp: datetime  # START of the 1-minute window, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None  # per-bar VWAP from the provider, not session-anchored


@dataclass(frozen=True, slots=True)
class FiveMinBar:
    symbol: str
    timestamp: datetime  # START of the 5-minute window, UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    bar_count: int  # number of 1-min bars that contributed (1-5; <5 means gaps)
