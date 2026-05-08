"""Tier 1 + Tier 2 merge logic.

When Tier 2 fires, the live decision is computed by merging the two
outputs. Per ``docs/LLM_SIGNAL_INTERFACE.md`` § "Tier 1 + Tier 2 merge
logic":

- Both agree on action: trust the action, take the higher confidence,
  use Tier 2's reasoning.
- Disagree on action: default to Hold. Disagreement = no edge,
  don't trade.

Both Tier 1 and Tier 2 raw outputs are recorded separately by the
signal_engine; the merge only produces the live_decision that drives
fills. Post-hoc analysis can still see what each tier said.

Skeleton in this commit. Implementation in step 5.
"""
from __future__ import annotations

from .types import LLMDecision


_REASONING_MAX = 280  # mirrors LLMDecision.reasoning Field(max_length=280)


def merge_tiers(t1: LLMDecision, t2: LLMDecision) -> LLMDecision:
    """Merge Tier 1 and Tier 2 decisions into the live decision.

    - Both agree on action: take the higher confidence, use T2's
      reasoning (T2 is the more carefully reasoned output), tag
      ``tier_provenance="t1_t2_agree"``.
    - Disagree on action: synthesize a Hold and tag
      ``tier_provenance="t1_t2_disagree"``. Disagreement = no edge,
      don't trade.

    Both T1 and T2 raw outputs are recorded separately by the signal
    engine; the merge produces only the live decision.
    """
    if t1.action == t2.action:
        merged_reasoning = f"[T1+T2 agree] {t2.reasoning}"
        return t2.model_copy(
            update={
                "confidence": max(t1.confidence, t2.confidence),
                "reasoning": merged_reasoning[:_REASONING_MAX],
                "tier_provenance": "t1_t2_agree",
            }
        )

    disagreement_reasoning = (
        f"T1={t1.action}({t1.confidence}); "
        f"T2={t2.action}({t2.confidence}). "
        f"Defaulting to Hold."
    )
    return LLMDecision(
        action="Hold",
        confidence=0,
        setup_label="tier_disagreement",
        reasoning=disagreement_reasoning[:_REASONING_MAX],
        tier_provenance="t1_t2_disagree",
    )
