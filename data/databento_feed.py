"""Databento live order book for ES futures (MBP-10 schema).

ES (E-mini S&P 500) is the futures contract day-traders use as the proxy for
SPY/SPX direction. Walls in its order book — large size sitting at specific
price levels — act as institutional support/resistance and predict short-term
turning points in equity index direction.

What MBP-10 gives us
--------------------
Each Databento MBP-10 record contains the full top 10 bid and ask levels at
the moment the event was published. Schema is "stateless" — we don't need to
apply incremental updates ourselves; each message is a complete snapshot of
the top of the book. Reconnection is correspondingly simple: resubscribe and
the next message has full state.

Symbology
---------
- Dataset: GLBX.MDP3 (CME Globex)
- Symbol: "ES.c.0" with stype_in="continuous" — front-month continuous
  contract that auto-rolls at expiry. Better than hardcoding "ESM5" because
  rollover is handled by Databento.

Cost note
---------
Databento bills per uncompressed byte streamed. ES MBP-10 emits tens of
thousands of records per second during active hours. Verify your usage in
the Databento portal periodically. Heartbeat-only (no subscription) is free.

Architecture
------------
This client maintains the LATEST snapshot only — we deliberately don't
queue every message, since that would balloon memory and the wall scanner
only reads on a 3-second timer anyway. The signal engine reads the latest
snapshot when generating signals every 5 min.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import databento as db

logger = logging.getLogger(__name__)

DATABENTO_PRICE_SCALE = 1e9  # MBP-10 prices are integer * 1e-9 (so divide by 1e9)
DEFAULT_DATASET = "GLBX.MDP3"
DEFAULT_SCHEMA = "mbp-10"
DEFAULT_SYMBOL = "ES.c.0"  # front-month continuous E-mini S&P 500
DEFAULT_STYPE = "continuous"


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float    # in contract points (e.g., 5234.25)
    size: int       # contracts at this level
    count: int      # number of distinct orders at this level


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    symbol: str
    timestamp: datetime  # ts_event from Databento, UTC
    bids: tuple[BookLevel, ...]  # sorted high to low (best bid first)
    asks: tuple[BookLevel, ...]  # sorted low to high (best ask first)

    @property
    def best_bid(self) -> Optional[BookLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[BookLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return (bb.price + ba.price) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb and ba:
            return ba.price - bb.price
        return None


class DatabentoFutureBookClient:
    """Maintains the latest MBP-10 snapshot for a single futures symbol.

    Async-native — integrates directly into the main asyncio event loop.

    Usage
    -----
        client = DatabentoFutureBookClient(api_key, symbol="ES.c.0")
        asyncio.create_task(client.run())
        # ... later ...
        snap = client.latest()
        if snap is not None:
            print(snap.mid_price, snap.bids[0])
    """

    def __init__(
        self,
        api_key: str,
        symbol: str = DEFAULT_SYMBOL,
        dataset: str = DEFAULT_DATASET,
        stype_in: str = DEFAULT_STYPE,
    ) -> None:
        self._api_key = api_key
        self._symbol = symbol
        self._dataset = dataset
        self._stype_in = stype_in
        self._latest: OrderBookSnapshot | None = None
        self._running = False
        # ts_event of the latest snapshot, for monotonicity check
        self._last_ts: int = 0

    def latest(self) -> OrderBookSnapshot | None:
        return self._latest

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Run the live client with auto-reconnect via natural refresh.

        On disconnect, we resubscribe without `start` — Databento sends the
        next live message which contains the current top-10 state, so we
        immediately re-sync. No replay needed for MBP-10.
        """
        self._running = True
        backoff = 1.0
        while self._running:
            client = None
            try:
                client = db.Live(key=self._api_key)
                client.subscribe(
                    dataset=self._dataset,
                    schema=DEFAULT_SCHEMA,
                    symbols=self._symbol,
                    stype_in=self._stype_in,
                )
                logger.info(
                    "Databento subscribed: dataset=%s symbol=%s schema=%s",
                    self._dataset, self._symbol, DEFAULT_SCHEMA,
                )
                backoff = 1.0  # reset on successful subscription

                async for record in client:
                    if not self._running:
                        break
                    self._handle_record(record)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Databento error, reconnecting in %.1fs", backoff,
                )
            finally:
                # Try to close the client cleanly
                if client is not None:
                    try:
                        client.terminate()
                    except Exception:
                        pass

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _handle_record(self, record) -> None:
        # MBP-10 record has .levels (list of 10 levels each with bid/ask px,sz,ct).
        # System messages (heartbeats, subscription acks) and symbol mapping
        # records do not have .levels — skip them.
        levels_attr = getattr(record, "levels", None)
        if levels_attr is None:
            return

        ts_event = getattr(record, "ts_event", None)
        if ts_event is None:
            return

        # Drop out-of-order records (rare, but possible on reconnect overlap)
        if ts_event < self._last_ts:
            return

        bids: list[BookLevel] = []
        asks: list[BookLevel] = []
        for lvl in levels_attr:
            # In Databento, missing levels can be represented with size 0
            # and price = INT64_MAX or 0. Filter to only real levels.
            if lvl.bid_sz > 0:
                bids.append(BookLevel(
                    price=lvl.bid_px / DATABENTO_PRICE_SCALE,
                    size=int(lvl.bid_sz),
                    count=int(lvl.bid_ct),
                ))
            if lvl.ask_sz > 0:
                asks.append(BookLevel(
                    price=lvl.ask_px / DATABENTO_PRICE_SCALE,
                    size=int(lvl.ask_sz),
                    count=int(lvl.ask_ct),
                ))

        # Defensive: ensure correct ordering even though Databento delivers
        # them sorted by depth (which equals price order for MBP-10).
        bids.sort(key=lambda b: b.price, reverse=True)
        asks.sort(key=lambda a: a.price)

        snapshot = OrderBookSnapshot(
            symbol=self._symbol,
            timestamp=datetime.fromtimestamp(ts_event / 1e9, tz=timezone.utc),
            bids=tuple(bids),
            asks=tuple(asks),
        )
        self._latest = snapshot
        self._last_ts = ts_event
