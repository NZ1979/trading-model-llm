"""Tests for analysis/regime.py (ChatGPT review #2 integration, 2026-05-13).

The classifier is pure — three scalars in, one of five labels out. We
test:

  - every label is reachable with sensible inputs (crash, trending_up,
    trending_down, choppy)
  - the dispatch order: crash beats trending_down beats choppy when
    multiple barrels of evidence point different ways
  - VIX-None gracefully degrades to a VIX-less classification, never
    raises, never returns "unknown"
  - VIX-vetoed trending_up falls through to choppy, not trending_down
  - trending_down does NOT have a VIX veto (slow-bleed regimes)
  - the RegimeInputs.vix_ratio property handles missing / zero / negative
    denominators
  - boundary conditions on every threshold (the cutoff value itself is
    inclusive of the bullish/bearish bucket, per the >= / <= operators
    in classify_regime)

We import the threshold constants from the module rather than hard-coding
literals so any future re-tune in regime.py doesn't break tests silently
when the meaning of "crash threshold" shifts.

The classifier never produces "unknown" — that's the LLMContext default
that this whole module exists to eliminate. Test that property too.
"""
import sys

sys.path.insert(0, '.')

from analysis.regime import (
    BREADTH_BEARISH,
    BREADTH_BULLISH,
    RegimeInputs,
    SPY_RETURN_CRASH,
    SPY_RETURN_TRENDING_DOWN,
    SPY_RETURN_TRENDING_UP,
    VIX_CRASH_RATIO,
    VIX_ELEVATED_RATIO,
    classify_regime,
)


# ---------------------------------------------------------------------------
# Label reachability — at least one input combo for each non-unknown label
# ---------------------------------------------------------------------------

def test_trending_up_reachable():
    """Strong SPY return + bullish breadth + calm VIX → trending_up."""
    inputs = RegimeInputs(
        spy_return_20d=0.05,
        vix_level=14.0,
        vix_60d_median=16.0,
        breadth_proxy=0.04,
    )
    assert classify_regime(inputs) == "trending_up"
    return "trending_up label reachable"


def test_trending_down_reachable():
    """Negative SPY return + bearish breadth + calm VIX → trending_down."""
    inputs = RegimeInputs(
        spy_return_20d=-0.04,
        vix_level=18.0,
        vix_60d_median=16.0,
        breadth_proxy=-0.03,
    )
    assert classify_regime(inputs) == "trending_down"
    return "trending_down label reachable"


def test_choppy_reachable_as_default():
    """Inputs that match no other bucket fall through to choppy."""
    inputs = RegimeInputs(
        spy_return_20d=0.005,   # tiny positive, below trending_up
        vix_level=16.0,
        vix_60d_median=16.0,
        breadth_proxy=0.001,    # essentially flat
    )
    assert classify_regime(inputs) == "choppy"
    return "choppy reached as default when no bucket matches"


def test_crash_reachable():
    """Severe SPY drop + VIX spike → crash."""
    inputs = RegimeInputs(
        spy_return_20d=-0.10,
        vix_level=42.0,
        vix_60d_median=18.0,   # ratio = 2.33, > 1.75
        breadth_proxy=-0.08,
    )
    assert classify_regime(inputs) == "crash"
    return "crash label reachable"


# ---------------------------------------------------------------------------
# Dispatch precedence — crash beats trending_down when both could match
# ---------------------------------------------------------------------------

def test_crash_takes_precedence_over_trending_down():
    """When SPY return and breadth both qualify as trending_down AND
    VIX confirms crash, the more-specific crash label wins."""
    inputs = RegimeInputs(
        spy_return_20d=-0.10,   # qualifies for both trending_down and crash
        vix_level=40.0,
        vix_60d_median=18.0,   # ratio = 2.22, > VIX_CRASH_RATIO
        breadth_proxy=-0.05,   # bearish
    )
    assert classify_regime(inputs) == "crash"
    return "crash beats trending_down when VIX confirms"


def test_severe_drop_without_vix_spike_is_trending_down_not_crash():
    """Same -10% SPY return but VIX is normal → not crash. Falls to
    trending_down via the bearish-breadth path."""
    inputs = RegimeInputs(
        spy_return_20d=-0.10,
        vix_level=17.0,
        vix_60d_median=18.0,   # ratio ≈ 0.94, NOT a crash
        breadth_proxy=-0.05,
    )
    assert classify_regime(inputs) == "trending_down"
    return "severe drop without VIX spike is trending_down, not crash"


# ---------------------------------------------------------------------------
# VIX veto on trending_up
# ---------------------------------------------------------------------------

def test_trending_up_vetoed_by_elevated_vix_falls_to_choppy():
    """SPY return + breadth would say trending_up, but VIX > 130% of
    median says nope — falls to choppy, not trending_down."""
    inputs = RegimeInputs(
        spy_return_20d=0.05,
        vix_level=30.0,
        vix_60d_median=18.0,   # ratio ≈ 1.67, > VIX_ELEVATED_RATIO
        breadth_proxy=0.04,
    )
    assert classify_regime(inputs) == "choppy"
    return "elevated VIX vetoes trending_up → choppy"


def test_trending_down_has_no_vix_veto():
    """Slow-bleed regime: SPY drifting down, breadth bearish, VIX calm.
    Still classified as trending_down — there is no VIX-too-low veto."""
    inputs = RegimeInputs(
        spy_return_20d=-0.04,
        vix_level=12.0,
        vix_60d_median=18.0,   # ratio ≈ 0.67, very calm
        breadth_proxy=-0.03,
    )
    assert classify_regime(inputs) == "trending_down"
    return "trending_down has no VIX veto (slow-bleed regimes pass)"


# ---------------------------------------------------------------------------
# VIX-missing fallback — degrades gracefully, never raises, never returns
# "unknown"
# ---------------------------------------------------------------------------

def test_vix_none_does_not_block_trending_up():
    """When VIX data is unavailable, trending_up classification still
    proceeds — there's no VIX value to veto against."""
    inputs = RegimeInputs(
        spy_return_20d=0.05,
        vix_level=None,
        vix_60d_median=None,
        breadth_proxy=0.04,
    )
    assert classify_regime(inputs) == "trending_up"
    return "VIX None allows trending_up (no veto possible)"


def test_vix_none_blocks_crash():
    """Crash REQUIRES VIX confirm. Without VIX we cannot upgrade a
    severe SPY drop to crash; falls to trending_down."""
    inputs = RegimeInputs(
        spy_return_20d=-0.10,
        vix_level=None,
        vix_60d_median=None,
        breadth_proxy=-0.05,
    )
    assert classify_regime(inputs) == "trending_down"
    return "VIX None prevents crash escalation"


def test_vix_none_returns_real_label_not_unknown():
    """Across the input space, VIX=None must never return the
    'unknown' label — that defeats the purpose of this module."""
    test_cases = [
        RegimeInputs(0.10, None, None, 0.05),    # very bullish
        RegimeInputs(-0.10, None, None, -0.05),  # very bearish
        RegimeInputs(0.0, None, None, 0.0),      # flat
        RegimeInputs(0.001, None, None, 0.001),  # essentially nothing
    ]
    for inputs in test_cases:
        label = classify_regime(inputs)
        assert label != "unknown", f"got unknown for {inputs}"
    return "VIX None never returns 'unknown'"


# ---------------------------------------------------------------------------
# vix_ratio property — handles missing / zero / negative denominators
# ---------------------------------------------------------------------------

def test_vix_ratio_with_both_values():
    inputs = RegimeInputs(0.0, 24.0, 12.0, 0.0)
    assert inputs.vix_ratio == 2.0
    return "vix_ratio = vix_level / median when both present"


def test_vix_ratio_none_when_vix_missing():
    inputs = RegimeInputs(0.0, None, 12.0, 0.0)
    assert inputs.vix_ratio is None
    return "vix_ratio is None when vix_level is None"


def test_vix_ratio_none_when_median_missing():
    inputs = RegimeInputs(0.0, 24.0, None, 0.0)
    assert inputs.vix_ratio is None
    return "vix_ratio is None when median is None"


def test_vix_ratio_none_when_median_zero():
    """Defensive: a zero or negative median would explode the ratio.
    Property returns None rather than raising or producing inf."""
    inputs = RegimeInputs(0.0, 24.0, 0.0, 0.0)
    assert inputs.vix_ratio is None
    return "vix_ratio is None when median is zero"


def test_vix_ratio_none_when_median_negative():
    inputs = RegimeInputs(0.0, 24.0, -1.0, 0.0)
    assert inputs.vix_ratio is None
    return "vix_ratio is None when median is negative"


# ---------------------------------------------------------------------------
# Boundary conditions — the threshold value itself sits in the bullish/
# bearish bucket per the >= / <= operators in classify_regime
# ---------------------------------------------------------------------------

def test_spy_return_exactly_at_trending_up_threshold():
    inputs = RegimeInputs(
        spy_return_20d=SPY_RETURN_TRENDING_UP,
        vix_level=14.0,
        vix_60d_median=16.0,
        breadth_proxy=BREADTH_BULLISH,
    )
    assert classify_regime(inputs) == "trending_up"
    return "SPY_RETURN_TRENDING_UP cutoff is inclusive"


def test_spy_return_just_below_trending_up_falls_to_choppy():
    inputs = RegimeInputs(
        spy_return_20d=SPY_RETURN_TRENDING_UP - 0.0001,
        vix_level=14.0,
        vix_60d_median=16.0,
        breadth_proxy=BREADTH_BULLISH,
    )
    assert classify_regime(inputs) == "choppy"
    return "just-below trending_up threshold → choppy"


def test_breadth_exactly_at_bullish_threshold():
    inputs = RegimeInputs(
        spy_return_20d=SPY_RETURN_TRENDING_UP,
        vix_level=14.0,
        vix_60d_median=16.0,
        breadth_proxy=BREADTH_BULLISH,
    )
    assert classify_regime(inputs) == "trending_up"
    return "BREADTH_BULLISH cutoff is inclusive"


def test_breadth_below_bullish_blocks_trending_up():
    """Even with strong SPY return, weak breadth falls to choppy."""
    inputs = RegimeInputs(
        spy_return_20d=0.10,
        vix_level=14.0,
        vix_60d_median=16.0,
        breadth_proxy=BREADTH_BULLISH - 0.001,
    )
    assert classify_regime(inputs) == "choppy"
    return "weak breadth blocks trending_up even with strong SPY return"


def test_vix_exactly_at_crash_ratio_with_severe_drop_is_crash():
    inputs = RegimeInputs(
        spy_return_20d=SPY_RETURN_CRASH,
        vix_level=VIX_CRASH_RATIO * 18.0,
        vix_60d_median=18.0,
        breadth_proxy=-0.08,
    )
    assert classify_regime(inputs) == "crash"
    return "VIX_CRASH_RATIO cutoff is inclusive"


def test_vix_just_below_elevated_does_not_veto_trending_up():
    """The veto fires at vix_ratio > VIX_ELEVATED_RATIO (strict >).
    Exactly at the ratio still passes."""
    inputs = RegimeInputs(
        spy_return_20d=0.05,
        vix_level=VIX_ELEVATED_RATIO * 16.0,
        vix_60d_median=16.0,
        breadth_proxy=0.04,
    )
    assert classify_regime(inputs) == "trending_up"
    return "VIX_ELEVATED_RATIO exact value does NOT veto (strict-greater veto)"


# ---------------------------------------------------------------------------
# Invariant — never returns 'unknown' over a broad sweep
# ---------------------------------------------------------------------------

def test_classifier_never_returns_unknown_with_real_inputs():
    """Across a grid of plausible inputs, the classifier never returns
    'unknown'. This is the central guarantee of the module."""
    spy_returns = [-0.20, -0.10, -0.05, -0.01, 0.0, 0.01, 0.05, 0.10, 0.20]
    vix_levels = [None, 10.0, 16.0, 22.0, 35.0, 60.0]
    breadths = [-0.10, -0.05, -0.01, 0.0, 0.01, 0.05, 0.10]
    median = 18.0

    for r in spy_returns:
        for v in vix_levels:
            for b in breadths:
                inputs = RegimeInputs(
                    spy_return_20d=r,
                    vix_level=v,
                    vix_60d_median=median if v is not None else None,
                    breadth_proxy=b,
                )
                label = classify_regime(inputs)
                assert label in (
                    "trending_up",
                    "trending_down",
                    "choppy",
                    "crash",
                ), f"unexpected label {label!r} for inputs {inputs}"
    return "no 'unknown' returned over 9 × 6 × 7 = 378-input grid"
