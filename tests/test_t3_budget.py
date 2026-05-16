"""Tests for data/replay/t3_budget.py (M2.2 sub-task #20)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, '.')

from data.replay.t3_budget import T3Budget


def test_has_capacity_true_under_cap():
    b = T3Budget(cap_dollars=1.0, per_call_estimate=0.05)
    assert b.has_capacity() is True


def test_has_capacity_false_when_next_call_exceeds_cap():
    b = T3Budget(cap_dollars=0.10, per_call_estimate=0.05)
    b.record_call()  # used 0.05
    b.record_call()  # used 0.10
    # Next call would push to 0.15 > 0.10
    assert b.has_capacity() is False


def test_record_call_advances_used_and_n_calls():
    b = T3Budget(cap_dollars=1.0, per_call_estimate=0.07)
    b.record_call()
    b.record_call()
    b.record_call()
    assert b.n_calls == 3
    assert b.used_dollars == pytest.approx(0.21)


def test_record_skip_counters_independent():
    b = T3Budget(cap_dollars=1.0)
    b.record_skip_budget()
    b.record_skip_budget()
    b.record_skip_sample()
    assert b.n_skipped_budget == 2
    assert b.n_skipped_sample == 1
    # Skips don't bump used or n_calls
    assert b.used_dollars == 0.0
    assert b.n_calls == 0


def test_cap_dollars_zero_has_no_capacity_from_start():
    b = T3Budget(cap_dollars=0.0, per_call_estimate=0.05)
    assert b.has_capacity() is False
