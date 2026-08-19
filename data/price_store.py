"""Persistent OHLCV bar store, extended hours included.

`scripts/watch.py` keeps an IN-MEMORY deque only (`--history 1440`, about two
hours at 5s). When the watcher stops, the session is gone; while it runs, the
ring buffer silently drops the session's own extremes as it rolls. On
2026-08-19 `derived.watch_high` read 1640 against a true session high of
1698.9999 — a $59 error in a field named "high" — and the 1542.00 low had
already aged out. That is what this module exists to prevent.

    from data.price_store import PriceStore
    with PriceStore("data/bars") as store:
        store.write_bars(bars, timeframe="1Min")
        ext = store.session_extremes("SNDK", "2026-08-19")

WHY SESSION PHASE IS A STORED COLUMN, NOT A QUERY
-------------------------------------------------
Pre-market, regular-hours and post-market bars behave differently enough that
mixing them silently corrupts every statistic built on top. Pre-market volume
runs one to two orders of magnitude below regular hours, so an RVOL or pace
figure computed across the boundary is meaningless. A "session high" that
includes the 04:00 tape is a different number from the one a chart shows.

Phase is therefore computed once at write time from the bar's own ET timestamp
and stored, so every downstream query states which tape it is describing rather
than inheriting whatever the caller happened to filter.

    PRE      04:00 - 09:30 ET
    REGULAR  09:30 - 16:00 ET
    POST     16:00 - 20:00 ET
    CLOSED   everything else (rare; venues occasionally print outside)

WHAT THIS STORE DOES NOT KNOW
-----------------------------
Bars are an aggregate. They cannot answer questions that need the book or the
individual prints: whether a level broke on real size or a 5-share odd lot,
whether a print was last-sale eligible, or where the bid sat when price crossed
a strike. `data/tick_store.py` is the corpus for that. This store is for
structure — levels, ranges, gaps, volume profile — over days and weeks.

Bars are also revised. Alpaca corrects late prints; re-running a backfill over
the same window can change a stored bar. Writes are UPSERTs for exactly that
reason, and `fetched_at_ms` records when each row was last written.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

PRE_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
POST_CLOSE = time(20, 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol        TEXT NOT NULL,
  timeframe     TEXT NOT NULL,      -- Alpaca vocabulary: 1Min, 5Min, 1Day
  ts_ns         INTEGER NOT NULL,   -- bar OPEN time, UTC nanoseconds
  session_date  TEXT NOT NULL,      -- ET calendar date, YYYY-MM-DD
  phase         TEXT NOT NULL,      -- PRE | REGULAR | POST | CLOSED
  open   REAL NOT NULL,
  high   REAL NOT NULL,
  low    REAL NOT NULL,
  close  REAL NOT NULL,
  volume INTEGER NOT NULL,
  trade_count INTEGER,
  vwap   REAL,
  fetched_at_ms INTEGER NOT NULL,
  UNIQUE(symbol, timeframe, ts_ns)
);
CREATE INDEX IF NOT EXISTS bars_session
  ON bars(symbol, timeframe, session_date, phase);
"""


def phase_for(dt_et: datetime) -> str:
    """Session phase of an ET-localised timestamp."""
    t = dt_et.time()
    if PRE_OPEN <= t < REGULAR_OPEN:
        return "PRE"
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        return "REGULAR"
    if REGULAR_CLOSE <= t < POST_CLOSE:
        return "POST"
    return "CLOSED"


def et_of(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, timezone.utc).astimezone(NY)


@dataclass(frozen=True, slots=True)
class SessionExtremes:
    """High/low/volume for one symbol-session, split by phase.

    `regular_*` is what a chart shows. `session_*` spans PRE through POST and
    will differ, often materially — on 2026-08-19 SNDK the pre-market low was
    1597.70 while the regular-hours low was 1542.00. Quoting one as the other
    is the mistake this split exists to make impossible.
    """

    symbol: str
    session_date: str
    bars: int

    session_high: float | None
    session_low: float | None
    session_volume: int

    regular_open: float | None
    regular_high: float | None
    regular_low: float | None
    regular_close: float | None
    regular_volume: int

    pre_high: float | None
    pre_low: float | None
    pre_volume: int

    post_high: float | None
    post_low: float | None
    post_volume: int

    @property
    def regular_range_pct(self) -> float | None:
        if self.regular_high is None or not self.regular_low:
            return None
        return (self.regular_high - self.regular_low) / self.regular_low * 100


class PriceStore:
    """SQLite bar store. Context manager; WAL; UPSERT on re-fetch."""

    def __init__(self, db_dir: str, *, filename: str = "bars.db") -> None:
        self._db_dir = db_dir
        self._filename = filename
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> str:
        return os.path.join(self._db_dir, self._filename)

    def open(self) -> "PriceStore":
        os.makedirs(self._db_dir, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=wal")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "PriceStore":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("PriceStore is not open; use it as a context manager")
        return self._conn

    # ------------------------------------------------------------- writing

    def write_bars(self, bars, *, timeframe: str = "1Min",
                   now_ms: int | None = None) -> int:
        """UPSERT bars. Returns the number of rows written.

        Accepts anything with symbol/ts_ns/open/high/low/close/volume, which
        `data.alpaca_rest.Bar` satisfies. Bars are revised by the vendor, so a
        re-fetch over the same window overwrites rather than duplicating.
        """
        conn = self._require()
        stamp = now_ms if now_ms is not None else int(
            datetime.now(timezone.utc).timestamp() * 1000)

        rows = []
        for b in bars:
            if b.ts_ns is None or b.close is None:
                continue  # fail visibly by dropping, counted in the return delta
            et = et_of(b.ts_ns)
            rows.append((
                b.symbol, timeframe, b.ts_ns,
                et.strftime("%Y-%m-%d"), phase_for(et),
                b.open, b.high, b.low, b.close, b.volume or 0,
                getattr(b, "trade_count", None), getattr(b, "vwap", None),
                stamp,
            ))

        if not rows:
            return 0

        conn.executemany(
            "INSERT INTO bars (symbol, timeframe, ts_ns, session_date, phase,"
            " open, high, low, close, volume, trade_count, vwap, fetched_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, timeframe, ts_ns) DO UPDATE SET"
            "   open=excluded.open, high=excluded.high, low=excluded.low,"
            "   close=excluded.close, volume=excluded.volume,"
            "   trade_count=excluded.trade_count, vwap=excluded.vwap,"
            "   fetched_at_ms=excluded.fetched_at_ms",
            rows,
        )
        conn.commit()
        return len(rows)

    # ------------------------------------------------------------- reading

    def sessions(self, symbol: str, *, timeframe: str = "1Min") -> list[str]:
        conn = self._require()
        return [r["session_date"] for r in conn.execute(
            "SELECT DISTINCT session_date FROM bars"
            " WHERE symbol=? AND timeframe=? ORDER BY session_date",
            (symbol, timeframe))]

    def bars(self, symbol: str, session_date: str, *,
             timeframe: str = "1Min", phase: str | None = None
             ) -> list[sqlite3.Row]:
        conn = self._require()
        sql = ("SELECT * FROM bars WHERE symbol=? AND timeframe=?"
               " AND session_date=?")
        args: list = [symbol, timeframe, session_date]
        if phase:
            sql += " AND phase=?"
            args.append(phase)
        return conn.execute(sql + " ORDER BY ts_ns", args).fetchall()

    def session_extremes(self, symbol: str, session_date: str, *,
                         timeframe: str = "1Min") -> SessionExtremes | None:
        """Phase-split extremes for one session. Never decays."""
        rows = self.bars(symbol, session_date, timeframe=timeframe)
        if not rows:
            return None

        def agg(subset):
            if not subset:
                return None, None, 0
            return (max(r["high"] for r in subset),
                    min(r["low"] for r in subset),
                    sum(r["volume"] for r in subset))

        pre = [r for r in rows if r["phase"] == "PRE"]
        reg = [r for r in rows if r["phase"] == "REGULAR"]
        post = [r for r in rows if r["phase"] == "POST"]

        s_hi, s_lo, s_vol = agg(rows)
        p_hi, p_lo, p_vol = agg(pre)
        r_hi, r_lo, r_vol = agg(reg)
        t_hi, t_lo, t_vol = agg(post)

        return SessionExtremes(
            symbol=symbol, session_date=session_date, bars=len(rows),
            session_high=s_hi, session_low=s_lo, session_volume=s_vol,
            regular_open=reg[0]["open"] if reg else None,
            regular_high=r_hi, regular_low=r_lo,
            regular_close=reg[-1]["close"] if reg else None,
            regular_volume=r_vol,
            pre_high=p_hi, pre_low=p_lo, pre_volume=p_vol,
            post_high=t_hi, post_low=t_lo, post_volume=t_vol,
        )

    def stats(self) -> dict:
        conn = self._require()
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) syms,"
            " COUNT(DISTINCT session_date) sessions,"
            " MIN(session_date) first, MAX(session_date) last FROM bars"
        ).fetchone()
        by_phase = {r["phase"]: r["n"] for r in conn.execute(
            "SELECT phase, COUNT(*) n FROM bars GROUP BY phase")}
        return {"rows": row["n"], "symbols": row["syms"],
                "sessions": row["sessions"], "first": row["first"],
                "last": row["last"], "by_phase": by_phase,
                "db_path": self.db_path}
