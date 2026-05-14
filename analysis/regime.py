"""Market regime classifier — populates LLMContext.market_regime_label.

The platform exposes a single market-level regime label that every per-ticker
LLMContext carries (see ``strategy/llm/types.py``). Until now that field has
defaulted to ``"unknown"`` because no code ever set it. This module is the
classifier that finally fills it in.

Design choices, with rationale:

1. **Pure function on three scalar inputs.** ``classify_regime`` takes a
   ``RegimeInputs`` dataclass of pre-computed numbers and returns a label.
   No I/O, no pandas, no Polygon calls. That makes it trivially unit-testable
   and lets the same logic run in the live daily routine, in the M2 replay
   harness, and in ``scripts/verify_regime.py`` — three callers, one function.

2. **Hard thresholds, no ML.** The audit (``docs/audits/2026-05-13-chatgpt-review-2-evaluation.md``)
   explicitly rejected the XGBoost/LightGBM/GARCH ensemble that ChatGPT
   review #2 proposed. Five-bucket deterministic rules over three inputs
   are good enough to remove the hardcoded ``"unknown"`` default, which is
   the actual bar to clear. Tunable thresholds live as module-level
   constants so they can be revised without touching the dispatch logic.

3. **Five labels, aligned with the existing field comment.** ``LLMContext``
   already documents the label set as
   ``trending_up | trending_down | choppy | crash | unknown``. We honor
   that taxonomy rather than introducing a parallel one. The mapping from
   the audit's three-bucket framing (``risk_on_momentum`` / ``chop`` /
   ``risk_off``) is documented in the threshold table below.

4. **Three inputs, deliberately small.**

   - ``spy_return_20d``: SPY's trailing-20-trading-day return. The slow
     momentum signal — captures whether we're in a sustained move.
   - ``vix_level`` and ``vix_60d_median``: VIX level and its 60-day median.
     We classify on the ratio, not the absolute value, so the thresholds
     stay valid across volatility regimes (a VIX of 18 means very different
     things in 2017 vs 2022).
   - ``breadth_proxy``: SPY's current close as a fraction above (or below)
     its own 50-day SMA. Chosen as the lightest-weight breadth indicator
     (one ticker, two scalars) that still carries directional information.
     Upgrade path: replace with % of SP500 above 50-day SMA, or RSP/SPY
     20-day return ratio. The classifier signature does not change; only
     the data-fetching layer does. See ``fetch_regime_inputs`` below.

5. **VIX-missing fallback.** Polygon Stocks Starter does not always return
   VIX index data (it's an index, not a stock; vendor coverage varies).
   When ``vix_level`` is ``None`` we degrade to a three-bucket
   ``trending_up`` / ``trending_down`` / ``choppy`` classifier driven by
   SPY return + breadth alone — never to ``unknown``, because
   ``"unknown"`` is the empty-input fallback we are explicitly trying to
   eliminate.

Verification gate (per Rule 14 in ``CLAUDE_PREFLIGHT.md``):
``scripts/verify_regime.py`` walks ~250 trading days of recent history and
reports the label distribution + transition counts. Eyeball check against
known events (e.g., the April-2025 risk-off, the Q4-2024 melt-up) gates
deployment. No threshold is committed until the verifier output looks
sane.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Label type — aligned with LLMContext.market_regime_label field comment.
# Do NOT extend this without also updating:
#   - strategy/llm/types.py (LLMContext.market_regime_label docstring)
#   - docs/LLM_SIGNAL_INTERFACE.md § "Input context structure"
#   - the prompt templates that mention regime labels
# ---------------------------------------------------------------------------

MarketRegime = Literal[
    "trending_up",     # audit's "risk_on_momentum"
    "trending_down",   # bearish trend short of crash
    "choppy",          # audit's "chop"; the default mid-range outcome
    "crash",           # audit's extreme "risk_off"; VIX spike + steep SPY drop
    "unknown",         # explicit empty-input fallback; should be rare
]


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------
# These are the dispatch knobs. Revising them is the supported way to tune
# the classifier. They are deliberately conservative starting points; the
# verifier output (scripts/verify_regime.py) is what justifies any change.

# SPY 20-day trailing return cutoffs (decimals, not percentage points).
SPY_RETURN_TRENDING_UP = 0.03    # +3% in 20d → momentum candidate
SPY_RETURN_TRENDING_DOWN = -0.03  # -3% in 20d → bearish trend candidate
SPY_RETURN_CRASH = -0.07         # -7% in 20d → crash candidate when VIX confirms

# VIX-vs-60d-median ratio cutoffs.
VIX_BELOW_NORMAL_RATIO = 0.90    # VIX < 90% of 60d median → calm regime confirm
VIX_ELEVATED_RATIO = 1.30        # VIX > 130% of 60d median → stress confirm
VIX_CRASH_RATIO = 1.75           # VIX > 175% of 60d median → crash confirm

# Breadth proxy cutoffs (SPY close / SPY 50-day SMA - 1.0).
BREADTH_BULLISH = 0.02           # SPY > 2% above its 50-day SMA
BREADTH_BEARISH = -0.02          # SPY > 2% below its 50-day SMA


# ---------------------------------------------------------------------------
# Inputs dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RegimeInputs:
    """Pre-computed scalars the classifier consumes.

    Built by ``fetch_regime_inputs`` (or any equivalent producer — the M2
    replay harness will construct one of these directly from historical
    bars at each as-of date).

    All fields are point-in-time as of some reference date. Computing
    them against future data invalidates the regime label and silently
    biases everything downstream — see ``M2_REPLAY_HARNESS_DESIGN.md``
    § "Point-in-time correctness."
    """

    spy_return_20d: float
    """SPY's return over the trailing 20 trading days, as a decimal
    (e.g. 0.025 = +2.5%)."""

    vix_level: float | None
    """VIX index close on the reference date. ``None`` when Polygon
    does not return VIX data — triggers the no-VIX fallback path."""

    vix_60d_median: float | None
    """Median VIX close over the trailing 60 trading days. ``None`` when
    the VIX series itself is unavailable or has fewer than 30 observations
    in the lookback window."""

    breadth_proxy: float
    """SPY close as a fraction above (-1.0) or below its 50-day SMA. The
    default proxy until full SP500-breadth data is wired up. ``0.0``
    indicates SPY sits exactly on its 50-day SMA."""

    @property
    def vix_ratio(self) -> float | None:
        """Convenience: VIX / VIX 60d median, or None if either input
        is missing or the denominator is non-positive."""
        if (
            self.vix_level is None
            or self.vix_60d_median is None
            or self.vix_60d_median <= 0
        ):
            return None
        return self.vix_level / self.vix_60d_median


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------

def classify_regime(inputs: RegimeInputs) -> MarketRegime:
    """Map RegimeInputs → MarketRegime label.

    Dispatch order matters: ``crash`` is the most specific bucket and is
    checked first; ``choppy`` is the most general and is the default
    return. The order intentionally privileges the more-extreme labels
    so a single day that breaches a crash threshold cannot be silently
    classified as ``trending_down``.

    Logic table:

    +-----------------+----------------------------------------------+
    | Output label    | Conditions                                    |
    +=================+==============================================+
    | crash           | spy_return_20d ≤ SPY_RETURN_CRASH **AND**     |
    |                 | vix_ratio ≥ VIX_CRASH_RATIO                   |
    +-----------------+----------------------------------------------+
    | trending_up     | spy_return_20d ≥ SPY_RETURN_TRENDING_UP       |
    |                 | **AND** breadth_proxy ≥ BREADTH_BULLISH       |
    |                 | **AND** (vix_ratio missing or ≤               |
    |                 | VIX_ELEVATED_RATIO)                           |
    +-----------------+----------------------------------------------+
    | trending_down   | spy_return_20d ≤ SPY_RETURN_TRENDING_DOWN     |
    |                 | **AND** breadth_proxy ≤ BREADTH_BEARISH       |
    +-----------------+----------------------------------------------+
    | choppy          | default — no other condition triggered        |
    +-----------------+----------------------------------------------+
    | unknown         | NEVER produced by this function. ``"unknown"``|
    |                 | only appears as the field default in          |
    |                 | LLMContext before ``classify_regime`` runs.   |
    +-----------------+----------------------------------------------+

    The ``unknown`` label is intentionally not a return path — the
    caller knows whether it has inputs at all, and if it does, we owe
    it a real label. Field default ``"unknown"`` in ``LLMContext``
    only persists when the daily routine failed to call us.
    """
    vix_ratio = inputs.vix_ratio  # property; computed once

    # 1. Crash — most specific, must clear both barrels.
    if (
        inputs.spy_return_20d <= SPY_RETURN_CRASH
        and vix_ratio is not None
        and vix_ratio >= VIX_CRASH_RATIO
    ):
        return "crash"

    # 2. Trending up — momentum + breadth confirm; VIX may veto if
    # elevated, otherwise pass through.
    if (
        inputs.spy_return_20d >= SPY_RETURN_TRENDING_UP
        and inputs.breadth_proxy >= BREADTH_BULLISH
    ):
        vix_vetoes = vix_ratio is not None and vix_ratio > VIX_ELEVATED_RATIO
        if not vix_vetoes:
            return "trending_up"

    # 3. Trending down — bearish momentum + bearish breadth.
    # No VIX veto here: a sustained drift down with calm VIX is still
    # ``trending_down`` (think slow-bleed regimes like Sep-Oct 2023).
    if (
        inputs.spy_return_20d <= SPY_RETURN_TRENDING_DOWN
        and inputs.breadth_proxy <= BREADTH_BEARISH
    ):
        return "trending_down"

    # 4. Choppy — the default. No directional momentum, or momentum
    # without breadth confirm, or VIX-vetoed momentum.
    return "choppy"


__all__ = [
    "MarketRegime",
    "RegimeInputs",
    "classify_regime",
    # Threshold constants exported so tests can import them rather than
    # hard-coding the literals (keeps tests in sync with any future tune).
    "SPY_RETURN_TRENDING_UP",
    "SPY_RETURN_TRENDING_DOWN",
    "SPY_RETURN_CRASH",
    "VIX_BELOW_NORMAL_RATIO",
    "VIX_ELEVATED_RATIO",
    "VIX_CRASH_RATIO",
    "BREADTH_BULLISH",
    "BREADTH_BEARISH",
]
