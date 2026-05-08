"""Tier 2 escalation rule.

The rule decides, given a Tier 1 decision and the context that produced
it, whether to consult Tier 2 (Sonnet) for a second opinion. Per
``docs/LLM_SIGNAL_INTERFACE.md`` § "Tier 2: Claude Sonnet — selective
escalation", escalation fires when ALL of:

1. Tier 1 confidence is in the uncertain middle ``[50, 75]``.
2. The candidate has a high-quality catalyst flag set (``catalyst_flags``
   non-empty AND any flag is one of: ``FDA_approval``, ``M&A``,
   ``earnings_beat_with_guidance_raise``, ``breakthrough_news``).
3. Pre-market RVOL > 3x.
4. The daily escalation budget has not been exhausted.

The budget check needs a counter that persists across calls, so this
module exposes a stateful ``EscalationBudget`` class.

Skeleton in this commit. Implementation in step 5 (alongside
signal_engine wiring).
"""
from __future__ import annotations

from .types import LLMContext, LLMDecision

HIGH_QUALITY_CATALYSTS: frozenset[str] = frozenset(
    {
        "FDA_approval",
        "M&A",
        "earnings_beat_with_guidance_raise",
        "breakthrough_news",
    }
)


class EscalationBudget:
    """Tracks Tier 2 escalation count against a daily cap.

    Reset by ``main.py`` at the start of each trading day. Thread-safe is
    not required (signal engine is single-threaded per cycle).
    """

    def __init__(self, max_per_day: int) -> None:
        self.max_per_day = max_per_day
        self._used = 0

    def has_capacity(self) -> bool:
        return self._used < self.max_per_day

    def record(self) -> None:
        self._used += 1

    def reset(self) -> None:
        self._used = 0

    @property
    def used(self) -> int:
        return self._used


def escalation_rule(
    ctx: LLMContext,
    t1_decision: LLMDecision,
    budget: EscalationBudget,
    *,
    confidence_floor: int = 50,
    confidence_ceiling: int = 75,
    pm_rvol_min: float = 3.0,
) -> bool:
    """Return True if the candidate should be escalated to Tier 2.

    All four gate conditions must hold:

    1. Daily escalation budget has capacity. Cheapest check, evaluated
       first to short-circuit on budget-exhausted days.
    2. Tier 1 confidence is in the uncertain middle ``[floor, ceiling]``.
       Defaults [50, 75]. Above 75 = T1 already confident; don't waste
       a Sonnet call. Below 50 = T1 weak signal that becomes Hold
       anyway in the merge step (or in the live engine's confidence
       floor); not worth a second opinion.
    3. Candidate has at least one high-quality catalyst flag set.
       Catalysts are the cases where Claude's domain reasoning has
       the most measurable lift over Qwen.
    4. Pre-market RVOL exceeds ``pm_rvol_min``. The setup must be
       liquid enough to trade; thin names get reverted regardless of
       whether the LLM agrees.

    The thresholds are kwargs (not hardcoded) so the signal_engine can
    pass them from config. Defaults match the design doc and the
    Phase 0 settings.yaml ``llm.t2`` block.
    """
    if not budget.has_capacity():
        return False

    if not (confidence_floor <= t1_decision.confidence <= confidence_ceiling):
        return False

    if not ctx.catalyst_flags:
        return False

    if not any(flag in HIGH_QUALITY_CATALYSTS for flag in ctx.catalyst_flags):
        return False

    if ctx.pm_rvol <= pm_rvol_min:
        return False

    return True
