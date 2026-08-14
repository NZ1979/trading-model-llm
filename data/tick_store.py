"""Durable tick persistence: SQLite WAL, one database per session date.

Spec: docs/FEED_SPEC_V4.md §6.

Design constraints, in priority order:

1. NEVER BLOCK THE READ LOOP. Alpaca and Schwab both drop slow consumers
   (Schwab does it explicitly with response code 30 STOP_STREAMING). The
   stream callbacks call `enqueue_*`, which is non-blocking and returns
   immediately. All SQLite work happens on a separate task.

2. LOSE DATA LOUDLY, NEVER SILENTLY. If the queue saturates, rows are dropped
   and counted, and the drop count is exposed via `stats` for get_health to
   surface (Rule 18). The alternative — applying backpressure to the read loop
   — gets you disconnected, which loses everything rather than some. Dropping
   visibly is the lesser failure, but it must be visible.

3. Batched writes. One INSERT per print is hopeless on a real tape.
   `executemany` in batches, flushed on size or age, whichever comes first.

Storage is separate from trading.db deliberately: different write rates,
different retention, different blast radius. A corrupted tick corpus must not
be able to take the decisions/orders tables with it.

Session date is ET-anchored, not UTC — the trading session is an ET concept,
and a UTC date would split a single session across two files at 20:00 ET.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from data.bar_types import MinuteBar
from data.tick_types import Quote, Trade, TradingStatus

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS trades (
  symbol TEXT NOT NULL, ts_ns INTEGER NOT NULL, trade_id INTEGER,
  price REAL NOT NULL, size INTEGER NOT NULL,
  exchange TEXT, conditions TEXT, tape TEXT,
  last_eligible INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_sym_ts ON trades(symbol, ts_ns);

CREATE TABLE IF NOT EXISTS quotes (
  symbol TEXT NOT NULL, ts_ns INTEGER NOT NULL,
  bid REAL, bid_size INTEGER, bid_ex TEXT,
  ask REAL, ask_size INTEGER, ask_ex TEXT,
  conditions TEXT, tape TEXT
);
CREATE INDEX IF NOT EXISTS ix_quotes_sym_ts ON quotes(symbol, ts_ns);

CREATE TABLE IF NOT EXISTS bars_1m (
  symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v INTEGER, vw REAL
);
CREATE INDEX IF NOT EXISTS ix_bars_sym_ts ON bars_1m(symbol, ts_ms);

CREATE TABLE IF NOT EXISTS statuses (
  symbol TEXT NOT NULL, ts_ns INTEGER NOT NULL,
  status_code TEXT, status_message TEXT,
  reason_code TEXT, reason_message TEXT, tape TEXT, is_halt INTEGER
);

-- Health ledger. One row per flush, so a post-hoc reader can tell whether a
-- window's data is complete without trusting the daemon's own logs.
CREATE TABLE IF NOT EXISTS ingest_health (
  ts_ms INTEGER NOT NULL,
  trades_written INTEGER, quotes_written INTEGER,
  bars_written INTEGER, statuses_written INTEGER,
  dropped_total INTEGER, queue_depth INTEGER,
  clock_skew_ms REAL
);
"""

# Odd lots never update last on the consolidated tape. Observed live
# 2026-08-14; see docs/FEED_SPEC_V4.md §1a. Anything outside the known set is
# the caller's problem to surface — this module records the flag, it does not
# adjudicate unknown codes.
NOT_LAST_ELIGIBLE = frozenset({"I"})


def is_last_eligible(conditions: Iterable[str]) -> bool:
    return not any(c in NOT_LAST_ELIGIBLE for c in conditions)


def session_date_et(now: datetime | None = None) -> str:
    """YYYYMMDD for the current ET calendar date."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ET).strftime("%Y%m%d")


@dataclass
class StoreStats:
    trades: int = 0
    quotes: int = 0
    bars: int = 0
    statuses: int = 0
    dropped: int = 0
    flushes: int = 0
    last_flush_ms: int = 0
    db_path: str = ""
    queue_depth: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "trades": self.trades, "quotes": self.quotes, "bars": self.bars,
            "statuses": self.statuses, "dropped": self.dropped,
            "flushes": self.flushes, "last_flush_ms": self.last_flush_ms,
            "db_path": self.db_path, "queue_depth": self.queue_depth,
            "drop_reasons": dict(self.drop_reasons),
        }


class TickStore:
    """Queue-fed batching writer for the tick corpus.

    Usage
    -----
        store = TickStore(r"C:\\trading\\LLM model\\data\\ticks")
        await store.open()
        task = asyncio.create_task(store.run())
        ...
        store.enqueue_trade(trade)     # non-blocking, safe on the read loop
        ...
        await store.close(task)
    """

    def __init__(
        self,
        db_dir: str,
        *,
        session_date: str | None = None,
        batch_size: int = 500,
        flush_interval_s: float = 2.0,
        queue_max: int = 100_000,
    ) -> None:
        self._db_dir = db_dir
        self._session_date = session_date or session_date_et()
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self._conn: sqlite3.Connection | None = None
        self._running = False
        self.stats = StoreStats()
        self._warned_full = False

    # -------------------------------------------------------------- lifecycle

    @property
    def db_path(self) -> str:
        return os.path.join(self._db_dir, f"ticks_{self._session_date}.db")

    async def open(self) -> None:
        os.makedirs(self._db_dir, exist_ok=True)
        path = self.db_path
        # check_same_thread=False: the connection is created here but used from
        # the worker task; asyncio runs both on the same thread, so this is
        # safe, but sqlite3's default guard is thread-identity based.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            # Fail loud: without WAL, a reader (the MCP server) blocks the
            # writer and the daemon stalls mid-session.
            raise RuntimeError(
                f"Expected WAL journal mode on {path}, got {mode!r}. "
                f"WAL is required so the MCP server can read while the daemon "
                f"writes."
            )
        self.stats.db_path = path
        logger.info("TickStore open: %s (journal_mode=%s)", path, mode)

    async def close(self, worker: asyncio.Task | None = None) -> None:
        self._running = False
        if worker is not None:
            await self._queue.put(None)  # sentinel; drains remaining rows
            try:
                await asyncio.wait_for(worker, timeout=30.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning("TickStore worker did not drain cleanly")
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None
        logger.info("TickStore closed: %s", self.stats.as_dict())

    # --------------------------------------------------------------- ingress

    def _put(self, kind: str, row: tuple) -> None:
        try:
            self._queue.put_nowait((kind, row))
        except asyncio.QueueFull:
            self.stats.dropped += 1
            self.stats.drop_reasons[kind] = (
                self.stats.drop_reasons.get(kind, 0) + 1
            )
            if not self._warned_full:
                self._warned_full = True
                logger.error(
                    "TickStore queue FULL (max=%d). Dropping rows. This is "
                    "DATA LOSS, not backpressure — the writer cannot keep up "
                    "with the tape. Every affected window is incomplete.",
                    self._queue.maxsize,
                )

    def enqueue_trade(self, t: Trade) -> None:
        self._put("trades", (
            t.symbol, t.ts_ns, t.trade_id, t.price, t.size, t.exchange,
            ",".join(t.conditions), t.tape, int(is_last_eligible(t.conditions)),
        ))

    def enqueue_quote(self, q: Quote) -> None:
        self._put("quotes", (
            q.symbol, q.ts_ns, q.bid_price, q.bid_size, q.bid_exchange,
            q.ask_price, q.ask_size, q.ask_exchange,
            ",".join(q.conditions), q.tape,
        ))

    def enqueue_bar(self, b: MinuteBar) -> None:
        self._put("bars_1m", (
            b.symbol, int(b.timestamp.timestamp() * 1000),
            b.open, b.high, b.low, b.close, b.volume, b.vwap,
        ))

    def enqueue_status(self, s: TradingStatus) -> None:
        self._put("statuses", (
            s.symbol, s.ts_ns, s.status_code, s.status_message,
            s.reason_code, s.reason_message, s.tape, int(s.is_halt),
        ))

    # ---------------------------------------------------------------- worker

    _SQL = {
        "trades": "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
        "quotes": "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)",
        "bars_1m": "INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?)",
        "statuses": "INSERT INTO statuses VALUES (?,?,?,?,?,?,?,?)",
    }

    async def run(self, clock_skew_ms: float = 0.0) -> None:
        """Drain the queue into SQLite until close() is called."""
        if self._conn is None:
            raise RuntimeError("TickStore.run() before open()")
        self._running = True
        pending: dict[str, list[tuple]] = {k: [] for k in self._SQL}
        count = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._flush_interval_s

        while True:
            timeout = max(0.0, deadline - loop.time())
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout)
            except asyncio.TimeoutError:
                item = ()  # flush tick
            if item is None:  # sentinel from close()
                self._flush(pending, clock_skew_ms)
                break
            if item:
                kind, row = item
                pending[kind].append(row)
                count += 1
            if count >= self._batch_size or loop.time() >= deadline:
                if count:
                    self._flush(pending, clock_skew_ms)
                    count = 0
                deadline = loop.time() + self._flush_interval_s
            if not self._running and self._queue.empty():
                self._flush(pending, clock_skew_ms)
                break

    def _flush(self, pending: dict[str, list[tuple]], skew_ms: float) -> None:
        if self._conn is None:
            return
        wrote = {k: 0 for k in self._SQL}
        try:
            for kind, rows in pending.items():
                if not rows:
                    continue
                self._conn.executemany(self._SQL[kind], rows)
                wrote[kind] = len(rows)
                rows.clear()
            if any(wrote.values()):
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                self._conn.execute(
                    "INSERT INTO ingest_health VALUES (?,?,?,?,?,?,?,?)",
                    (now_ms, wrote["trades"], wrote["quotes"], wrote["bars_1m"],
                     wrote["statuses"], self.stats.dropped,
                     self._queue.qsize(), skew_ms),
                )
                self._conn.commit()
                self.stats.trades += wrote["trades"]
                self.stats.quotes += wrote["quotes"]
                self.stats.bars += wrote["bars_1m"]
                self.stats.statuses += wrote["statuses"]
                self.stats.flushes += 1
                self.stats.last_flush_ms = now_ms
        except sqlite3.Error:
            # Do not swallow. A failed flush is data loss and the operator
            # must see it (Rule 18). Rows are dropped rather than retried
            # forever, which would stall the queue behind a poison batch.
            logger.exception("TickStore flush FAILED — rows lost: %s",
                             {k: len(v) for k, v in pending.items()})
            for rows in pending.values():
                self.stats.dropped += len(rows)
                rows.clear()
        finally:
            self.stats.queue_depth = self._queue.qsize()
