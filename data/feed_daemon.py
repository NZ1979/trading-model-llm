"""Feed daemon: Alpaca SIP stream -> TickStore. Spec docs/FEED_SPEC_V4.md §2.

Wires the read loop to the writer with the read/compute split the spec
requires: stream callbacks do nothing but enqueue, all SQLite work happens on
a separate task. Both vendors drop slow consumers, so anything expensive on
the read loop eventually manifests as a disconnect.

Also implements the continuous clock-skew monitor. Godzilla was found 2.41s
slow on 2026-08-14, which would have silently invalidated the 500ms quote-age
confidence tag. Measuring once at connect is not enough — drift returns — so
skew is sampled on every trade and the rolling median is persisted with each
flush.

Run from C:\\trading\\LLM model with the venv active:

    python -m scripts.run_feed_daemon --symbols SNDK --seconds 60
    python -m scripts.run_feed_daemon --symbols SNDK,MU,NVDA   # until Ctrl-C
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque

from data.alpaca_market_data import AlpacaMarketStream
from data.bar_types import MinuteBar
from data.tick_store import TickStore
from data.tick_types import Quote, Trade, TradingStatus

logger = logging.getLogger(__name__)


class ClockSkewMonitor:
    """Rolling estimate of local clock error against exchange timestamps.

    Sign convention, stated carefully because getting it backwards sends you
    the wrong way during an incident:

        skew_ms = (local_now - event_timestamp)

    NEGATIVE means the local clock is BEHIND the exchange — events appear to
    arrive from the future. This is what a slow clock looks like, and it is
    what Godzilla showed on 2026-08-14: roughly -2400ms.

    POSITIVE means the local clock reads later than the event. A small
    positive value is normal transport latency, since a print always reaches
    you some milliseconds after it happened. A large positive value means the
    local clock is genuinely AHEAD.

    So the healthy range is a small positive number. Zero or negative means
    the clock is slow by at least the transport latency.

    Uses a median over a bounded window rather than a mean: network jitter and
    out-of-order arrival produce outliers that would drag a mean around, and
    we want the systematic offset, not the worst packet. This conflates clock
    error with latency by construction — the median of a busy tape is
    dominated by the systematic component, which is the part that matters.
    """

    def __init__(self, window: int = 512, alert_ms: float = 500.0) -> None:
        self._samples: deque[float] = deque(maxlen=window)
        self._alert_ms = alert_ms
        self._alerted = False

    def observe(self, event_ts_ns: int) -> None:
        self._samples.append((time.time_ns() - event_ts_ns) / 1e6)

    @property
    def median_ms(self) -> float:
        if not self._samples:
            return 0.0
        return statistics.median(self._samples)

    def check(self) -> None:
        """Warn once if skew crosses the threshold that breaks §5.1."""
        if self._alerted or len(self._samples) < 32:
            return
        m = self.median_ms
        if abs(m) > self._alert_ms:
            self._alerted = True
            logger.error(
                "CLOCK SKEW %.0fms exceeds %.0fms (%s). The quote-age "
                "confidence tag (500ms) is meaningless at this offset and "
                "cross-vendor timestamp correlation is invalid. Fix with "
                "'w32tm /resync' from an ELEVATED PowerShell, then restart.",
                m, self._alert_ms,
                "local clock is BEHIND the exchange" if m < 0
                else "local clock is AHEAD of the exchange",
            )


class FeedDaemon:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: set[str],
        db_dir: str,
        *,
        feed: str = "sip",
        lock_path: str | None = None,
        bar_symbols: set[str] | None = None,
    ) -> None:
        self.store = TickStore(db_dir)
        self.skew = ClockSkewMonitor()
        self._counts = {"trades": 0, "quotes": 0, "bars": 0, "statuses": 0}
        self.stream = AlpacaMarketStream(
            api_key=api_key,
            api_secret=api_secret,
            symbols=(bar_symbols or symbols),
            on_bar=self._on_bar,
            feed=feed,
            on_trade=self._on_trade,
            on_quote=self._on_quote,
            on_status=self._on_status,
            tick_symbols=symbols,
            lock_path=lock_path,
        )
        self._tasks: list[asyncio.Task] = []

    # --- callbacks: enqueue and return. Nothing expensive here. ---

    async def _on_trade(self, t: Trade) -> None:
        self.skew.observe(t.ts_ns)
        self.store.enqueue_trade(t)
        self._counts["trades"] += 1

    async def _on_quote(self, q: Quote) -> None:
        self.store.enqueue_quote(q)
        self._counts["quotes"] += 1

    async def _on_bar(self, b: MinuteBar) -> None:
        self.store.enqueue_bar(b)
        self._counts["bars"] += 1

    async def _on_status(self, s: TradingStatus) -> None:
        self.store.enqueue_status(s)
        self._counts["statuses"] += 1
        logger.warning("TRADING STATUS %s: %s (%s) halt=%s",
                       s.symbol, s.status_message, s.reason_message, s.is_halt)

    # --- lifecycle ---

    async def _skew_loop(self, interval_s: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            self.skew.check()

    async def _writer_loop(self) -> None:
        # Re-reads the current skew estimate on each flush so the health
        # ledger records skew as it was, not as it was at startup.
        while True:
            await self.store.run(clock_skew_ms=self.skew.median_ms)
            return

    async def start(self) -> None:
        await self.store.open()
        self._writer = asyncio.create_task(
            self.store.run(clock_skew_ms=0.0), name="TickStoreWriter")
        self._tasks = [
            asyncio.create_task(self.stream.run(), name="AlpacaStream"),
            asyncio.create_task(self._skew_loop(), name="SkewMonitor"),
        ]

    async def stop(self) -> None:
        self.stream.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.store.close(self._writer)

    @property
    def summary(self) -> dict:
        return {
            "received": dict(self._counts),
            "persisted": self.store.stats.as_dict(),
            "clock_skew_ms": round(self.skew.median_ms, 2),
        }
