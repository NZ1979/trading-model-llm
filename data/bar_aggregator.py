"""Aggregate 1-min bars into 5-min bars per symbol.

Window alignment: 5-min windows start at minute % 5 == 0 (e.g., 9:30, 9:35,
9:40, ...). Standard chart convention. The 5-min bar starting at 9:30
contains 1-min bars at 9:30, 9:31, 9:32, 9:33, 9:34. The 1-min bar at 9:35
is the first bar of the NEXT 5-min window.

Usage
-----
    async def on_5min(bar: FiveMinBar) -> None:
        # update indicators, run signal engine, etc.

    aggregator = BarAggregator(on_5min)
    stream = AlpacaBarStream(..., on_bar=aggregator.on_minute_bar)
    await stream.run()

Why stateful: minute bars arrive one at a time, asynchronously, possibly
out of order across symbols. We hold the open 5-min window per symbol and
emit when the window rolls over. If a symbol has zero 1-min bars in a
window (very low volume), no 5-min bar is emitted for that window — this
is correct; no trades = no bar.

Edge case handled: bars can be MISSING (e.g., a stock has no trades for a
minute in pre-market). The aggregator emits a 5-min bar with bar_count<5
when the window rolls over with whatever 1-min bars it received. Indicators
downstream treat low-volume bars normally.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from data.bar_types import FiveMinBar, MinuteBar

logger = logging.getLogger(__name__)


def _window_start_for(ts: datetime) -> datetime:
    """Floor a timestamp to the start of its 5-min window."""
    return ts.replace(
        minute=(ts.minute // 5) * 5,
        second=0,
        microsecond=0,
    )


class BarAggregator:
    """Streaming 1-min -> 5-min bar aggregator, per-symbol stateful."""

    def __init__(
        self,
        on_5min_bar: Callable[[FiveMinBar], Awaitable[None]],
    ) -> None:
        self._callback = on_5min_bar
        # Per-symbol: list of 1-min bars currently buffered for the open window
        self._buffers: dict[str, list[MinuteBar]] = defaultdict(list)
        # Per-symbol: start timestamp of the currently-open 5-min window
        self._window_start: dict[str, datetime] = {}

    async def on_minute_bar(self, bar: MinuteBar) -> None:
        """Process an incoming 1-min bar.

        Decides if this bar belongs to the current open window or a new one.
        If new, emits the previous window first.
        """
        sym = bar.symbol
        target_window = _window_start_for(bar.timestamp)
        current_window = self._window_start.get(sym)

        if current_window is None:
            # First bar we've seen for this symbol; just buffer it
            self._buffers[sym].append(bar)
            self._window_start[sym] = target_window
            return

        if target_window == current_window:
            # Still in the same 5-min window
            self._buffers[sym].append(bar)
            return

        if target_window > current_window:
            # Window rolled over. Emit the old window, start a new one.
            await self._emit(sym, current_window)
            self._buffers[sym] = [bar]
            self._window_start[sym] = target_window
            return

        # target_window < current_window: late/out-of-order bar.
        # Drop it and log; reordering 5-min bars on the fly is more trouble
        # than it's worth for an intraday signal system.
        logger.warning(
            "Late minute bar for %s: bar_ts=%s, current_window_start=%s. Dropping.",
            sym, bar.timestamp, current_window,
        )

    async def _emit(self, symbol: str, window_start: datetime) -> None:
        bars = self._buffers.get(symbol)
        if not bars:
            return
        five = FiveMinBar(
            symbol=symbol,
            timestamp=window_start,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
            bar_count=len(bars),
        )
        try:
            await self._callback(five)
        except Exception:
            logger.exception(
                "5-min callback failed for %s window_start=%s",
                symbol, window_start,
            )

    async def flush_all(self) -> None:
        """Emit all currently-open windows. Call at shutdown or session end."""
        for sym in list(self._window_start.keys()):
            window_start = self._window_start[sym]
            await self._emit(sym, window_start)
        self._buffers.clear()
        self._window_start.clear()
