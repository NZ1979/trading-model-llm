"""Tests for the feed daemon wiring and clock-skew monitor (spec v4 phase 2).

The skew sign convention is the thing most worth pinning down. It was
documented backwards on first write, and a wrong sign in an alert sends an
operator the wrong way during an incident. These tests assert the convention
explicitly so it cannot drift again.

No network — callbacks are driven directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from data.feed_daemon import ClockSkewMonitor, FeedDaemon
from data.tick_types import Quote, Trade

MS = 1_000_000


# ------------------------------------------------------------ sign convention

def test_slow_local_clock_reads_negative():
    """Godzilla on 2026-08-14: clock 2.41s slow, events appeared to arrive
    from the future. That must read NEGATIVE."""
    m = ClockSkewMonitor()
    now = time.time_ns()
    for _ in range(40):
        m.observe(now + 2_410 * MS)   # event timestamped ahead of local now
    assert m.median_ms < 0
    assert -2500 < m.median_ms < -2300


def test_fast_local_clock_reads_positive():
    m = ClockSkewMonitor()
    now = time.time_ns()
    for _ in range(40):
        m.observe(now - 2_410 * MS)
    assert m.median_ms > 0
    assert 2300 < m.median_ms < 2500


def test_synchronised_clock_reads_near_zero():
    m = ClockSkewMonitor()
    for _ in range(40):
        m.observe(time.time_ns())
    assert abs(m.median_ms) < 50


def test_empty_monitor_is_zero_not_an_error():
    assert ClockSkewMonitor().median_ms == 0.0


def test_median_ignores_outliers():
    """One badly delayed packet must not move the estimate."""
    m = ClockSkewMonitor()
    now = time.time_ns()
    for _ in range(40):
        m.observe(now - 10 * MS)
    m.observe(now - 30_000 * MS)      # a 30s outlier
    assert 5 < m.median_ms < 20


# ------------------------------------------------------------------ alerting

def test_no_alert_below_sample_floor():
    """Alerting on three samples would fire on startup jitter."""
    m = ClockSkewMonitor(alert_ms=500.0)
    now = time.time_ns()
    for _ in range(10):
        m.observe(now - 9_000 * MS)
    m.check()
    assert m._alerted is False


def test_alerts_once_above_threshold(caplog):
    m = ClockSkewMonitor(alert_ms=500.0)
    now = time.time_ns()
    for _ in range(40):
        m.observe(now - 9_000 * MS)
    with caplog.at_level("ERROR"):
        m.check()
        m.check()
    assert m._alerted is True
    assert sum("CLOCK SKEW" in r.message for r in caplog.records) == 1


def test_alert_text_names_the_correct_direction(caplog):
    """A negative median must say BEHIND, not AHEAD."""
    m = ClockSkewMonitor(alert_ms=500.0)
    now = time.time_ns()
    for _ in range(40):
        m.observe(now + 9_000 * MS)     # local clock behind
    with caplog.at_level("ERROR"):
        m.check()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "BEHIND" in text and "AHEAD" not in text


def test_alert_threshold_is_below_the_quote_age_tag():
    """The 500ms confidence tag in spec §5.1 is meaningless once skew
    approaches it, so the alert must fire at or under that value."""
    assert ClockSkewMonitor()._alert_ms <= 500.0


# --------------------------------------------------------------- end-to-end

def test_callbacks_persist_and_counts_reconcile(tmp_path):
    async def go() -> FeedDaemon:
        fd = FeedDaemon("k", "s", {"SNDK"}, str(tmp_path), lock_path=None)
        await fd.store.open()
        w = asyncio.create_task(fd.store.run())
        base = time.time_ns()
        for i in range(30):
            cond = ("@", "T") if i % 2 else ("@", "T", "I")
            await fd._on_trade(
                Trade("SNDK", base + i, 1666.0 + i, 100, "D", cond, "C", i))
        for i in range(10):
            await fd._on_quote(Quote(
                "SNDK", base + i, 1665.0, 100, "P", 1666.5, 200, "Q", (), "C"))
        await asyncio.sleep(0.4)
        await fd.store.close(w)
        return fd

    fd = asyncio.run(go())
    conn = sqlite3.connect(fd.store.db_path)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 10
    assert fd._counts["trades"] == 30
    assert fd._counts["quotes"] == 10
    assert fd.store.stats.dropped == 0


def test_summary_shape():
    fd = FeedDaemon("k", "s", {"SNDK"}, "/tmp/unused", lock_path=None)
    assert set(fd.summary) == {"received", "persisted", "clock_skew_ms"}


def test_trades_feed_the_skew_monitor_but_quotes_do_not():
    """Skew is sampled from trades only. Quotes on a thin tape are sparse
    enough that including them would make the estimate lumpy."""
    async def go() -> FeedDaemon:
        fd = FeedDaemon("k", "s", {"SNDK"}, "/tmp/unused", lock_path=None)
        await fd._on_quote(Quote(
            "SNDK", time.time_ns(), 1.0, 1, "P", 2.0, 1, "Q", (), "C"))
        assert len(fd.skew._samples) == 0
        await fd._on_trade(
            Trade("SNDK", time.time_ns(), 1.0, 1, "D", ("@",), "C", 1))
        return fd

    fd = asyncio.run(go())
    assert len(fd.skew._samples) == 1
