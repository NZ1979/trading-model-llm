"""Detect support/resistance walls in a futures order book.

A "wall" is a price level with substantially more resting size than typical.
Walls act as institutional support/resistance: large bids absorb selling
(support) and large asks absorb buying (resistance). When price reaches a
wall, it tends to stall, fade, or reverse.

Two layers
----------
1. detect_walls(snapshot, ...) — single-snapshot detection. Returns the top
   N largest bid sizes (support) and top N ask sizes (resistance) within
   max_distance_pct of mid price. Pure function, no state.

2. PersistentWallScanner — anti-spoofing wrapper. Spoofing is endemic in
   ES: large size flashes onto a level, sits 10-30 seconds, then disappears
   without filling when price approaches. A wall is "real" only if it
   persists across multiple snapshots. We require the price level to appear
   in at least min_persistence of the last window_size snapshots.

3. FuturesWallMonitor — orchestrator. Polls the book client on a 3-second
   timer, feeds snapshots to PersistentWallScanner, exposes .walls() for
   the signal engine to read.

Distance threshold
------------------
The original Phase 1 spec said "5% of current price" — too wide for ES.
At ES=5000, 5% = 250 points, which covers the entire likely day's range.
Default here is 1.0%, matching Phase 5's signal logic ("support wall is
less than 1% below current price"). Tunable per-call.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from dataclasses import dataclass
from typing import Literal

from data.databento_feed import (
    BookLevel,
    DatabentoFutureBookClient,
    OrderBookSnapshot,
)

logger = logging.getLogger(__name__)

WallSide = Literal["support", "resistance"]


@dataclass(frozen=True, slots=True)
class Wall:
    side: WallSide
    price: float
    size: int
    distance_pct: float  # signed: support negative (below mid), resistance positive


def detect_walls(
    snapshot: OrderBookSnapshot,
    max_distance_pct: float = 1.0,
    top_n: int = 3,
    min_size_multiple: float = 2.0,
) -> tuple[list[Wall], list[Wall]]:
    """Single-snapshot wall detection.

    Args:
        snapshot: order book snapshot.
        max_distance_pct: only consider levels within this % of mid price.
        top_n: return up to this many walls per side.
        min_size_multiple: a level must be at least this multiple of the
            median size across all levels in the book to count as a wall.
            This filters out "biggest of a thin book" false positives.

    Returns (support_walls, resistance_walls), sorted by size descending.
    """
    mid = snapshot.mid_price
    if mid is None or mid <= 0:
        return ([], [])

    # Compute a size threshold: levels must be at least min_size_multiple
    # times the median to qualify as a wall.
    all_sizes = [lvl.size for lvl in snapshot.bids] + [lvl.size for lvl in snapshot.asks]
    if not all_sizes:
        return ([], [])
    sorted_sizes = sorted(all_sizes)
    median_size = sorted_sizes[len(sorted_sizes) // 2]
    size_threshold = max(1, int(median_size * min_size_multiple))

    support: list[Wall] = []
    for lvl in snapshot.bids:
        dist_pct = (lvl.price - mid) / mid * 100  # negative for bids
        if abs(dist_pct) > max_distance_pct:
            continue
        if lvl.size < size_threshold:
            continue
        support.append(Wall(
            side="support",
            price=lvl.price,
            size=lvl.size,
            distance_pct=dist_pct,
        ))

    resistance: list[Wall] = []
    for lvl in snapshot.asks:
        dist_pct = (lvl.price - mid) / mid * 100  # positive for asks
        if dist_pct > max_distance_pct:
            continue
        if lvl.size < size_threshold:
            continue
        resistance.append(Wall(
            side="resistance",
            price=lvl.price,
            size=lvl.size,
            distance_pct=dist_pct,
        ))

    support.sort(key=lambda w: w.size, reverse=True)
    resistance.sort(key=lambda w: w.size, reverse=True)
    return (support[:top_n], resistance[:top_n])


class PersistentWallScanner:
    """Track walls across snapshots; surface only persistent ones.

    Anti-spoofing: a wall must appear at the same price level in at least
    `min_persistence` of the last `window_size` snapshots to be reported.

    With 3s scan interval and window_size=10, that's a 30-second window;
    min_persistence=7 means the wall has to be visible at least 70% of the
    time. Spoofed walls that flash for a few seconds get filtered out.
    """

    def __init__(
        self,
        window_size: int = 10,
        min_persistence: int = 7,
        max_distance_pct: float = 1.0,
        top_n: int = 3,
        min_size_multiple: float = 2.0,
    ) -> None:
        self._window_size = window_size
        self._min_persistence = min_persistence
        self._max_distance_pct = max_distance_pct
        self._top_n = top_n
        self._min_size_multiple = min_size_multiple
        # History of price-sets per side
        self._support_history: deque[set[float]] = deque(maxlen=window_size)
        self._resistance_history: deque[set[float]] = deque(maxlen=window_size)
        # Most-recent wall details by price (so we can return latest size/distance)
        self._latest_support: dict[float, Wall] = {}
        self._latest_resistance: dict[float, Wall] = {}

    def update(self, snapshot: OrderBookSnapshot) -> None:
        support, resistance = detect_walls(
            snapshot,
            max_distance_pct=self._max_distance_pct,
            top_n=self._top_n,
            min_size_multiple=self._min_size_multiple,
        )
        self._support_history.append({w.price for w in support})
        self._resistance_history.append({w.price for w in resistance})
        for w in support:
            self._latest_support[w.price] = w
        for w in resistance:
            self._latest_resistance[w.price] = w

        # Periodically prune stale entries from the latest dicts
        # (prices that haven't been seen in the recent window)
        active_support_prices = set().union(*self._support_history) if self._support_history else set()
        active_resistance_prices = set().union(*self._resistance_history) if self._resistance_history else set()
        for p in list(self._latest_support.keys()):
            if p not in active_support_prices:
                del self._latest_support[p]
        for p in list(self._latest_resistance.keys()):
            if p not in active_resistance_prices:
                del self._latest_resistance[p]

    def persistent_walls(self) -> tuple[list[Wall], list[Wall]]:
        """Return (support, resistance) walls meeting the persistence threshold."""
        if len(self._support_history) < self._min_persistence:
            return ([], [])

        sup_counts: Counter[float] = Counter()
        for prices in self._support_history:
            sup_counts.update(prices)
        res_counts: Counter[float] = Counter()
        for prices in self._resistance_history:
            res_counts.update(prices)

        sup = [
            self._latest_support[p]
            for p, count in sup_counts.items()
            if count >= self._min_persistence and p in self._latest_support
        ]
        res = [
            self._latest_resistance[p]
            for p, count in res_counts.items()
            if count >= self._min_persistence and p in self._latest_resistance
        ]
        sup.sort(key=lambda w: w.size, reverse=True)
        res.sort(key=lambda w: w.size, reverse=True)
        return (sup[:self._top_n], res[:self._top_n])


class FuturesWallMonitor:
    """Run the live book client and periodic wall scanner together.

    Phase 5's signal engine calls `walls()` to get the latest persistent
    walls when evaluating each ticker. Walls are computed on a 3s timer
    inside this class; reading them is just a dict lookup.
    """

    def __init__(
        self,
        api_key: str,
        symbol: str = "ES.c.0",
        scan_interval_sec: float = 3.0,
        window_size: int = 10,
        min_persistence: int = 7,
        max_distance_pct: float = 1.0,
        top_n: int = 3,
        min_size_multiple: float = 2.0,
    ) -> None:
        self._book = DatabentoFutureBookClient(api_key=api_key, symbol=symbol)
        self._scanner = PersistentWallScanner(
            window_size=window_size,
            min_persistence=min_persistence,
            max_distance_pct=max_distance_pct,
            top_n=top_n,
            min_size_multiple=min_size_multiple,
        )
        self._scan_interval = scan_interval_sec
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Spawn the book client and scan loop. Returns immediately."""
        self._tasks = [
            asyncio.create_task(self._book.run(), name="DatabentoBook"),
            asyncio.create_task(self._scan_loop(), name="WallScanner"),
        ]

    async def stop(self) -> None:
        self._book.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval)
            snap = self._book.latest()
            if snap is None:
                continue
            self._scanner.update(snap)

    def walls(self) -> tuple[list[Wall], list[Wall]]:
        """Return current persistent (support, resistance) walls."""
        return self._scanner.persistent_walls()

    def latest_book(self) -> OrderBookSnapshot | None:
        """Expose the raw latest snapshot (for debugging/journaling)."""
        return self._book.latest()
