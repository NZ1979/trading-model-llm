"""Shared types for the LLM signal generator.

``LLMContext`` is what the platform constructs and feeds to a tier client;
``LLMDecision`` is what every tier returns. Both shapes are pinned to
``docs/LLM_SIGNAL_INTERFACE.md`` (the contract between the platform and
Claude/Qwen). Changes here MUST update that doc and bump
``prompt_version`` so cached responses don't get mis-parsed.

Style choices:
- ``LLMContext`` is a frozen dataclass: we construct it from production
  data, immutability matters more than parser-level validation.
- ``LLMDecision`` is a Pydantic model: it is parsed from JSON returned
  by an LLM, so strict validation at parse time is the whole point —
  schema-invalid output falls through to ``Hold(reason='schema_invalid')``
  per the failure-modes table in the design doc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


@dataclass(frozen=True, slots=True)
class LLMContext:
    """Per-candidate input shape for any tier.

    All three tiers see identical context — Tier 1 must not see Tier 2's
    output, Tier 3 must not see Tier 1's output, etc. The harness and
    live signal engine both construct one of these per (ticker,
    timestamp) and pass it to each enabled tier independently.

    Field semantics and units are pinned in
    ``docs/LLM_SIGNAL_INTERFACE.md`` § "Input context structure". When
    adding a field here, also add it to the prompt template and bump
    ``prompt_version``.

    The full field set will be filled in during M3 implementation. This
    skeleton declares only the shape and the meta fields; concrete data
    fields land alongside the prompt-template work in step 5+.
    """

    ticker: str
    timestamp_et: str
    prompt_version: str

    # Fields needed by escalation_rule (step 5). Defaults make the
    # context still constructable from incomplete data during plumbing
    # tests; production build_context() in step 6+ will populate these
    # from the news classifier and pre-market context.
    catalyst_flags: tuple[str, ...] = ()
    pm_rvol: float = 0.0

    # ... remaining fields populated in M3 per the design doc.
    # Skeleton intentionally minimal so this module stays importable
    # without forcing every consumer to construct a full context.


class LLMDecision(BaseModel):
    """Schema-validated output from any tier.

    Mirrors the JSON schema in ``docs/LLM_SIGNAL_INTERFACE.md`` §
    "Output schema". Validated on parse: any field out of range or any
    missing required field raises ``ValidationError``, which the
    signal engine converts into ``Hold(reason='schema_invalid')``.

    The ``tier_provenance`` field is set by ``signal_engine.evaluate``
    after merging Tier 1 and Tier 2 results. Direct LLM responses set
    it to ``None``; the merge step replaces it with the appropriate
    enum value.
    """

    action: Literal["Buy", "Sell", "Hold"]
    confidence: int = Field(ge=0, le=100)
    setup_label: str = Field(max_length=50)
    reasoning: str = Field(max_length=280)

    stop_loss_atr_multiple: float = Field(ge=1.0, le=3.0, default=1.5)
    take_profit_atr_multiple: float = Field(ge=1.0, le=5.0, default=2.0)
    time_horizon: Literal["intraday", "overnight", "multi_day"] = "intraday"

    concerns: list[str] = Field(default_factory=list)
    alternative_view: str = Field(default="", max_length=140)

    # Provenance fields — populated by the signal_engine, not the LLM.
    # Values:
    #   t1_only: Tier 1 succeeded; escalation rule did not fire.
    #   t1_t2_agree: T1 + T2 both succeeded with the same action; merge
    #     took the higher confidence and T2's reasoning.
    #   t1_t2_disagree: T1 + T2 actions differed; live decision is Hold.
    #   t1_fallback_t2: escalation fired but T2 raised; live decision
    #     uses T1 alone.
    #   t1_only_budget_exhausted: gates passed but daily T2 cap reached;
    #     live decision uses T1 alone.
    #   t1_failed: T1 itself raised; live decision is a synthetic Hold
    #     with the failure mode in setup_label (e.g. "schema_invalid_t1").
    tier_provenance: (
        Literal[
            "t1_only",
            "t1_t2_agree",
            "t1_t2_disagree",
            "t1_fallback_t2",
            "t1_only_budget_exhausted",
            "t1_failed",
        ]
        | None
    ) = None
    raw_response: dict[str, Any] | None = None

    @field_validator("concerns")
    @classmethod
    def _trim_concerns(cls, v: list[str]) -> list[str]:
        """Cap concerns list at 5 items; drop empties."""
        return [c.strip() for c in v if c and c.strip()][:5]
