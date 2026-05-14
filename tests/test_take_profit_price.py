"""Tests for compute_take_profit_price in strategy/risk.py.

Layer 1 v2 take-profit activation landed 2026-05-13. The TP-price
computation lives as a pure helper in ``strategy/risk.py`` (parallel to
the existing ``_compute_stop``) so it's unit-testable independent of
``main.py``'s order-submission orchestration.

Coverage:
  - enabled=False short-circuits to None (preserves OTO behavior)
  - daily_atr=0 and daily_atr<0 short-circuit to None (Rule 18:
    fail-loud at caller log layer, don't fabricate a TP)
  - typical inputs: buy → TP above entry, sell → TP below entry
  - the offset equals tp_atr_multiple × daily_atr exactly (to rounding)
  - rounding to 2 decimals
  - boundary tp_atr_multiple values (1.0 floor, 5.0 ceiling per LLMDecision bounds)
  - asymmetric verification: buy with negative offset would mean TP
    below entry; the helper never produces that
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from strategy.risk import compute_take_profit_price


# ---------------------------------------------------------------------------
# Disabled paths — both return None and preserve v1 OTO behavior
# ---------------------------------------------------------------------------


def test_disabled_returns_none():
    """enabled=False is the default historical state; short-circuits to None."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=2.0,
        tp_atr_multiple=2.0,
        enabled=False,
    )
    assert tp is None


def test_disabled_returns_none_even_with_valid_inputs():
    """Disabled wins over otherwise-valid inputs; no TP computed."""
    tp = compute_take_profit_price(
        side="sell",
        entry_price=50.0,
        daily_atr=1.5,
        tp_atr_multiple=3.0,
        enabled=False,
    )
    assert tp is None


def test_daily_atr_zero_returns_none():
    """Warmup-incomplete ATR=0 short-circuits; caller logs the warning."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=0.0,
        tp_atr_multiple=2.0,
        enabled=True,
    )
    assert tp is None


def test_daily_atr_negative_returns_none():
    """Defensive: negative ATR is treated the same as zero. A negative
    value can only indicate a calc bug upstream; we don't propagate it
    into a backwards TP."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=-1.0,
        tp_atr_multiple=2.0,
        enabled=True,
    )
    assert tp is None


# ---------------------------------------------------------------------------
# Direction and magnitude — the core math
# ---------------------------------------------------------------------------


def test_buy_tp_above_entry():
    """Long bracket: TP must sit above the entry. entry=100, atr=2,
    mult=2 → distance 4 → tp=104.00."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=2.0,
        tp_atr_multiple=2.0,
        enabled=True,
    )
    assert tp == 104.0


def test_sell_tp_below_entry():
    """Short bracket: TP must sit below the entry. entry=100, atr=2,
    mult=2 → distance 4 → tp=96.00."""
    tp = compute_take_profit_price(
        side="sell",
        entry_price=100.0,
        daily_atr=2.0,
        tp_atr_multiple=2.0,
        enabled=True,
    )
    assert tp == 96.0


def test_offset_equals_atr_times_multiple():
    """The signed offset from entry to TP equals tp_atr_multiple ×
    daily_atr exactly (modulo 2-decimal rounding). Spot-check at a
    few non-round numbers."""
    tp_buy = compute_take_profit_price(
        side="buy",
        entry_price=147.83,
        daily_atr=3.27,
        tp_atr_multiple=1.5,
        enabled=True,
    )
    # 147.83 + 1.5 × 3.27 = 147.83 + 4.905 = 152.735 → 152.74 (banker's-rounding tolerant)
    assert tp_buy == 152.74

    tp_sell = compute_take_profit_price(
        side="sell",
        entry_price=147.83,
        daily_atr=3.27,
        tp_atr_multiple=1.5,
        enabled=True,
    )
    # 147.83 - 4.905 = 142.925 → 142.93 (banker's rounding for .5 case)
    # Both 142.92 and 142.93 are acceptable depending on rounding mode;
    # round() in Python uses banker's rounding so .925 → .92, but the
    # actual float value 142.92499... → .92. Check membership.
    assert tp_sell in (142.92, 142.93)


def test_rounding_to_two_decimals():
    """Output is rounded to 2 decimals for Alpaca's limit_price format."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=0.555,    # 0.555 × 2 = 1.11
        tp_atr_multiple=2.0,
        enabled=True,
    )
    assert tp == 101.11


def test_high_multiple_at_ceiling():
    """tp_atr_multiple=5.0 is the LLMDecision Pydantic ceiling. Helper
    still computes correctly at the bound."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=2.0,
        tp_atr_multiple=5.0,
        enabled=True,
    )
    assert tp == 110.0


def test_low_multiple_at_floor():
    """tp_atr_multiple=1.0 is the LLMDecision Pydantic floor."""
    tp = compute_take_profit_price(
        side="buy",
        entry_price=100.0,
        daily_atr=2.0,
        tp_atr_multiple=1.0,
        enabled=True,
    )
    assert tp == 102.0


# ---------------------------------------------------------------------------
# Invariants — properties the function must guarantee for downstream
# validation in execution/alpaca_orders.py::submit_bracket_order
# ---------------------------------------------------------------------------


def test_buy_tp_always_strictly_above_entry_when_enabled():
    """submit_bracket_order raises ValueError if buy TP <= limit_price.
    Verify across a grid that the helper never violates that contract."""
    for entry in (10.0, 50.0, 100.0, 250.0, 999.99):
        for atr in (0.01, 0.5, 2.0, 5.0):
            for mult in (1.0, 1.5, 2.0, 3.0, 5.0):
                tp = compute_take_profit_price(
                    side="buy",
                    entry_price=entry,
                    daily_atr=atr,
                    tp_atr_multiple=mult,
                    enabled=True,
                )
                assert tp is not None
                assert tp > entry, f"buy tp={tp} not > entry={entry} (atr={atr}, mult={mult})"


def test_sell_tp_always_strictly_below_entry_when_enabled():
    """Mirror invariant for short brackets."""
    for entry in (10.0, 50.0, 100.0, 250.0, 999.99):
        for atr in (0.01, 0.5, 2.0, 5.0):
            for mult in (1.0, 1.5, 2.0, 3.0, 5.0):
                tp = compute_take_profit_price(
                    side="sell",
                    entry_price=entry,
                    daily_atr=atr,
                    tp_atr_multiple=mult,
                    enabled=True,
                )
                assert tp is not None
                assert tp < entry, f"sell tp={tp} not < entry={entry} (atr={atr}, mult={mult})"


def test_default_config_multiple_two_yields_one_third_reward_risk():
    """Sanity: with tp_atr_multiple=2.0 (config default) and stop at
    1.5 × ATR, the reward/risk ratio is 4/3 ≈ 1.33. Verify the math
    holds for the documented config combo."""
    entry = 100.0
    atr = 2.0
    tp = compute_take_profit_price(
        side="buy", entry_price=entry, daily_atr=atr,
        tp_atr_multiple=2.0, enabled=True,
    )
    stop_distance = 1.5 * atr  # standard stop multiple
    reward = tp - entry        # 4.0
    risk = stop_distance       # 3.0
    rr = reward / risk
    assert abs(rr - 4 / 3) < 1e-9
