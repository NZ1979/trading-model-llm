"""Logic tests for scripts.earnings_tape.bucket_tape. No network.

The distinction these pin down is the one that would otherwise silently produce
a wrong answer: an empty bucket because nothing traded versus an empty bucket
because the feed does not carry extended hours. Both render as no rows.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.earnings_tape import bucket_tape, BUCKETS  # noqa: E402

ET = ZoneInfo("America/New_York")
REPORT = date(2026, 5, 7)
REACTION = date(2026, 5, 8)


@dataclass
class FakeBar:
    ts_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: int


def bar(day, hhmm, o, h, l, c, v=1000):
    hh, mm = hhmm.split(":")
    dt = datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=ET)
    return FakeBar(int(dt.timestamp() * 1e9), o, h, l, c, v)


def by_label(res):
    return {b["label"]: b for b in res}


def test_every_bucket_is_present_even_when_empty():
    res = bucket_tape([bar(REPORT, "10:00", 37, 37, 37, 37)], REPORT, REACTION)
    assert [b["label"] for b in res] == [b[0] for b in BUCKETS]
    assert by_label(res)["POST +0-30min"]["empty"] is True


def test_the_first_thirty_post_market_minutes_are_isolated():
    bars = [bar(REPORT, "15:55", 37.6, 37.7, 37.5, 37.66),
            bar(REPORT, "16:05", 40.0, 41.5, 39.8, 41.0),
            bar(REPORT, "16:25", 41.0, 41.4, 40.9, 41.2),
            bar(REPORT, "16:35", 41.2, 41.9, 41.1, 41.8)]
    b = by_label(bucket_tape(bars, REPORT, REACTION))
    assert b["POST +0-30min"]["bars"] == 2
    assert b["POST +0-30min"]["last"] == 41.2
    assert b["POST +0-30min"]["high"] == 41.5
    assert b["POST +30-2h"]["bars"] == 1


def test_1600_starts_post_and_1600_is_not_in_rth():
    bars = [bar(REPORT, "15:55", 1, 1, 1, 1), bar(REPORT, "16:00", 2, 2, 2, 2)]
    b = by_label(bucket_tape(bars, REPORT, REACTION))
    assert b["RTH before print"]["bars"] == 1
    assert b["POST +0-30min"]["bars"] == 1


def test_premarket_and_rth_are_taken_from_the_REACTION_day():
    bars = [bar(REPORT, "08:00", 9, 9, 9, 9),      # report-day pre-market, no bucket
            bar(REACTION, "07:00", 40, 40, 40, 40),
            bar(REACTION, "10:00", 41, 42, 40, 41.5)]
    b = by_label(bucket_tape(bars, REPORT, REACTION))
    assert b["PRE-MARKET"]["bars"] == 1 and b["PRE-MARKET"]["last"] == 40
    assert b["RTH reaction day"]["bars"] == 1 and b["RTH reaction day"]["last"] == 41.5


def test_baseline_is_the_last_rth_close_before_the_print():
    bars = [bar(REPORT, "09:35", 36, 37, 36, 36.5),
            bar(REPORT, "15:55", 37.5, 37.7, 37.4, 37.66),
            bar(REPORT, "16:05", 41, 41, 41, 41)]
    b = by_label(bucket_tape(bars, REPORT, REACTION))
    assert b["RTH before print"]["last"] == 37.66


def test_volume_and_extremes_aggregate_within_a_bucket():
    bars = [bar(REPORT, "16:05", 40, 44, 39, 41, 5_000),
            bar(REPORT, "16:20", 41, 42, 38, 40, 3_000)]
    b = by_label(bucket_tape(bars, REPORT, REACTION))["POST +0-30min"]
    assert b["volume"] == 8_000 and b["high"] == 44 and b["low"] == 38


def test_bars_outside_every_window_are_simply_dropped():
    bars = [bar(REPORT, "03:00", 1, 1, 1, 1), bar(REACTION, "22:00", 2, 2, 2, 2)]
    res = bucket_tape(bars, REPORT, REACTION)
    assert all(b["empty"] for b in res)
