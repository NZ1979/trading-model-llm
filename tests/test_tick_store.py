"""Tests for the tick corpus writer (spec v4 phase 2).

Covers the three properties that matter operationally:
  1. Nothing is lost silently — rows land, counts reconcile against the DB.
  2. Queue saturation drops loudly and never blocks the caller. Blocking would
     apply backpressure to the websocket read loop, and both vendors drop slow
     consumers, so blocking loses everything instead of some.
  3. WAL actually holds — a second connection can read while the writer is
     open. Without it the MCP server would block the daemon mid-session.

No network. Uses tmp_path, asyncio.run inside sync tests (no pytest-asyncio
dependency).
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from data.bar_types import MinuteBar
from data.tick_store import TickStore, is_last_eligible, session_date_et
from data.tick_types import Quote, Trade, TradingStatus


def _trade(sym="SNDK", ns=1000, px=1666.0, sz=100, cond=("@", "T")) -> Trade:
    return Trade(sym, ns, px, sz, "D", tuple(cond), "C", 1)


def _quote(sym="SNDK", ns=2000) -> Quote:
    return Quote(sym, ns, 1665.0, 100, "P", 1666.5, 200, "Q", (), "C")


# ------------------------------------------------------------- eligibility

@pytest.mark.parametrize("conditions,expected", [
    (("@", "T"), True),
    (("@", "T", "F"), True),
    (("@", "T", "I"), False),   # odd lot
    (("I",), False),
    ((), True),
])
def test_last_eligibility(conditions, expected):
    assert is_last_eligible(conditions) is expected


def test_session_date_is_eight_digits():
    d = session_date_et()
    assert len(d) == 8 and d.isdigit()


def test_session_date_is_et_not_utc():
    """23:30 UTC on the 14th is 19:30 ET the same day; 01:00 UTC on the 15th is
    still 21:00 ET on the 14th. A UTC-derived date would split one session
    across two files."""
    late = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)
    assert session_date_et(late) == "20260814"


# ------------------------------------------------------------- persistence

def test_all_message_kinds_persist_and_stats_reconcile(tmp_path):
    async def go() -> TickStore:
        s = TickStore(str(tmp_path), session_date="20260814",
                      batch_size=10, flush_interval_s=0.2)
        await s.open()
        w = asyncio.create_task(s.run(clock_skew_ms=1.6))
        for i in range(25):
            cond = ("@", "T") if i % 2 else ("@", "T", "I")
            s.enqueue_trade(_trade(ns=1000 + i, sz=100 if i % 2 else 1,
                                   cond=cond))
        for i in range(5):
            s.enqueue_quote(_quote(ns=2000 + i))
        s.enqueue_bar(MinuteBar(
            "SNDK", datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc),
            1, 2, 0.5, 1.5, 100, 1.2))
        s.enqueue_status(TradingStatus(
            "SNDK", 3000, "H", "Halted", "LUDP", "Vol", "C"))
        await asyncio.sleep(0.6)
        await s.close(w)
        return s

    store = asyncio.run(go())
    conn = sqlite3.connect(store.db_path)

    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 25
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM statuses").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ingest_health").fetchone()[0] >= 1

    # in-memory stats must agree with what actually landed
    assert store.stats.trades == 25
    assert store.stats.quotes == 5
    assert store.stats.dropped == 0


def test_eligibility_flag_and_conditions_round_trip(tmp_path):
    async def go() -> TickStore:
        s = TickStore(str(tmp_path), session_date="20260814", batch_size=2,
                      flush_interval_s=0.1)
        await s.open()
        w = asyncio.create_task(s.run())
        s.enqueue_trade(_trade(ns=1, cond=("@", "T", "I")))
        s.enqueue_trade(_trade(ns=2, cond=("@", "T", "F")))
        await asyncio.sleep(0.3)
        await s.close(w)
        return s

    store = asyncio.run(go())
    conn = sqlite3.connect(store.db_path)
    rows = conn.execute(
        "SELECT conditions, last_eligible FROM trades ORDER BY ts_ns"
    ).fetchall()
    assert rows == [("@,T,I", 0), ("@,T,F", 1)]


def test_clock_skew_is_recorded_in_health_ledger(tmp_path):
    """Skew must be persisted per flush, not just logged — a post-hoc reader
    has to be able to tell whether a window's timestamps were trustworthy."""
    async def go() -> TickStore:
        s = TickStore(str(tmp_path), session_date="20260814", batch_size=1,
                      flush_interval_s=0.1)
        await s.open()
        w = asyncio.create_task(s.run(clock_skew_ms=2410.0))
        s.enqueue_trade(_trade())
        await asyncio.sleep(0.3)
        await s.close(w)
        return s

    store = asyncio.run(go())
    conn = sqlite3.connect(store.db_path)
    assert conn.execute(
        "SELECT clock_skew_ms FROM ingest_health LIMIT 1"
    ).fetchone()[0] == 2410.0


# ---------------------------------------------------------------- overflow

def test_queue_overflow_drops_loudly_and_never_blocks(tmp_path):
    """Blocking here would push backpressure onto the websocket read loop.
    Both Alpaca and Schwab drop slow consumers, so blocking converts partial
    data loss into total disconnection."""
    async def go():
        s = TickStore(str(tmp_path), session_date="20260815", batch_size=5,
                      queue_max=10)
        await s.open()
        t0 = time.monotonic()
        for i in range(500):
            s.enqueue_trade(_trade(sym="X", ns=i))
        elapsed = time.monotonic() - t0
        await s.close(None)
        return s, elapsed

    store, elapsed = asyncio.run(go())
    assert elapsed < 0.5, f"enqueue blocked for {elapsed:.3f}s"
    assert store.stats.dropped == 490
    assert store.stats.drop_reasons["trades"] == 490


# --------------------------------------------------------------------- WAL

def test_reader_can_query_while_writer_is_open(tmp_path):
    """The MCP server reads this database while the daemon writes it. Without
    WAL the reader blocks the writer and the daemon stalls mid-session."""
    async def go() -> int:
        s = TickStore(str(tmp_path), session_date="20260816", batch_size=2,
                      flush_interval_s=0.1)
        await s.open()
        w = asyncio.create_task(s.run())
        for i in range(6):
            s.enqueue_trade(_trade(sym="Y", ns=i, sz=50))
        await asyncio.sleep(0.4)

        reader = sqlite3.connect(s.db_path)  # separate connection, writer live
        n = reader.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        reader.close()

        await s.close(w)
        return n

    assert asyncio.run(go()) == 6


def test_run_before_open_fails_loud(tmp_path):
    s = TickStore(str(tmp_path))
    with pytest.raises(RuntimeError):
        asyncio.run(s.run())
