"""Logic tests for scripts.gap_shape.analyse_session. No network.

These pin the definitions that decide the answer. The regular-hours filter is
the load-bearing one: on a gap-down session the pre-market low is almost always
below the regular-hours low, so failing to exclude it would report "LOW first"
on essentially every day by construction.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gap_shape import analyse_session  # noqa: E402

ET = ZoneInfo("America/New_York")


@dataclass
class FakeBar:
    ts_ns: int
    open: float
    high: float
    low: float
    close: float


def bar(hhmm, o, h, l, c, day="2026-07-16"):
    hh, mm = hhmm.split(":")
    dt = datetime.fromisoformat(f"{day}T{hh}:{mm}:00").replace(tzinfo=ET)
    return FakeBar(int(dt.timestamp() * 1e9), o, h, l, c)


def test_high_before_low_is_ran_up_then_sold():
    bars = [bar("09:30", 100, 101, 99, 100), bar("09:35", 100, 110, 100, 108),
            bar("10:00", 108, 108, 90, 92), bar("15:55", 92, 93, 92, 93)]
    r = analyse_session(bars)
    assert r["order"] == "HIGH first"
    assert (r["high"], r["low"]) == (110, 90)
    assert round(r["mins_to_high"]) == 5 and round(r["mins_to_low"]) == 30


def test_low_before_high_is_sold_then_bounced():
    bars = [bar("09:30", 100, 101, 99, 100), bar("09:35", 100, 100, 88, 90),
            bar("14:00", 90, 115, 90, 114), bar("15:55", 114, 114, 113, 113)]
    r = analyse_session(bars)
    assert r["order"] == "LOW first"
    assert round(r["mins_to_low"]) == 5 and round(r["mins_to_high"]) == 270


def test_one_bar_holding_both_extremes_is_not_forced_into_an_order():
    bars = [bar("09:30", 100, 120, 80, 100), bar("09:35", 100, 101, 99, 100)]
    assert analyse_session(bars)["order"] == "SAME"


def test_premarket_and_postmarket_are_excluded():
    """Without this the pre-market low wins on every gap-down day."""
    bars = [bar("07:00", 100, 100, 50, 60),
            bar("09:30", 100, 101, 99, 100), bar("10:00", 100, 105, 98, 104),
            bar("18:00", 104, 200, 104, 200)]
    r = analyse_session(bars)
    assert (r["high"], r["low"], r["bars"]) == (105, 98, 2)


def test_0930_is_included_and_1600_is_not():
    bars = [bar("09:30", 100, 101, 99, 100), bar("15:55", 100, 102, 100, 101),
            bar("16:00", 101, 150, 101, 150)]
    assert analyse_session(bars)["high"] == 102


def test_open_is_the_first_rth_bar_not_a_premarket_print():
    bars = [bar("08:00", 50, 50, 50, 50), bar("09:30", 100, 101, 99, 100),
            bar("10:00", 100, 102, 98, 101)]
    assert analyse_session(bars)["open"] == 100


def test_too_few_rth_bars_returns_none_rather_than_a_shape():
    assert analyse_session([bar("07:00", 100, 100, 100, 100)]) is None
    assert analyse_session([bar("09:30", 100, 100, 100, 100)]) is None


def test_percentages_are_measured_against_the_rth_open():
    bars = [bar("09:30", 100, 110, 90, 100), bar("10:00", 100, 105, 95, 102)]
    r = analyse_session(bars)
    assert round(r["o2h"], 2) == 10.0
    assert round(r["o2l"], 2) == -10.0
    assert round(r["o2c"], 2) == 2.0
