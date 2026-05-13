"""LLM analysis schema (v2): classifications + risk tags + position action.

This module implements the schema split described in
``docs/LLM_MODEL_V2_REFINEMENTS.md § A.1``. In v1, the LLM emitted a single
``LLMDecision`` that combined classification (what is this setup?) with a
final action (Buy/Sell/Hold). v2 splits those concerns:

  - ``LLMAnalysis`` — what the LLM observes (catalyst quality, setup type,
    trade readiness, risks, counter-thesis, position action for held names).
    Pure classification. Becomes the deterministic ``TradePolicy``'s
    primary input.

  - ``LLMDecision`` (preserved from v1, lives in ``strategy.llm.types``) —
    the LLM's *advisory* action + confidence. The policy may agree, override,
    or ignore. Still useful as a sanity check and as a fallback when the
    policy is genuinely uncertain.

  - ``LLMOutput`` — the wire wrapper that combines both, plus the raw JSON
    for the audit trail.

Schema-invalid output falls through to ``Hold(reason='schema_invalid')`` at
the signal-engine layer, identical to v1. Field semantics here are pinned
to ``docs/LLM_MODEL_V2_REFINEMENTS.md § A.1``; any change requires bumping
``prompt_version`` in ``config/settings.yaml`` so cached responses don't get
mis-parsed.

Defensive truncation on string fields with ``max_length`` mirrors the
pattern in ``LLMDecision``: Anthropic's tool-use enforces required fields
and enum/type constraints but NOT numeric bounds or string maxLength. The
``mode="before"`` validators truncate over-long strings to valid in-range
values; type/enum mismatches still raise ``ValidationError`` as designed.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from strategy.llm.types import LLMDecision


# ---------------------------------------------------------------------------
# Enums — classification vocabularies the LLM must select from
# ---------------------------------------------------------------------------


class CatalystQuality(str, Enum):
    """Severity tier of any news/event catalyst in the prior 24h window."""

    MAJOR = "major"          # FDA approval, M&A confirmed, earnings beat + guidance raise
    MATERIAL = "material"    # Significant analyst action, regulatory item, secondary offering
    MINOR = "minor"          # Small product news, minor analyst note
    AMBIGUOUS = "ambiguous"  # Headlines exist but unclear impact (rumor without confirmation)
    NONE = "none"            # No qualifying news in the window


class SetupType(str, Enum):
    """Technical pattern the LLM identifies (or NO_SETUP if none qualifies)."""

    GAP_AND_GO = "gap_and_go"
    PULLBACK_IN_TREND = "pullback_in_trend"
    BREAKOUT_CONFIRM = "breakout_confirm"
    BREAKDOWN = "breakdown"
    REVERSAL = "reversal"
    CONSOLIDATION = "consolidation"
    NO_SETUP = "no_setup"


class TradeReadiness(str, Enum):
    """How actionable the setup is *right now*. AVOID overrides any positive
    setup_type; it is the LLM's hard veto for the policy layer."""

    READY = "ready"                  # All conditions aligned for entry now
    WAIT_PULLBACK = "wait_pullback"  # Setup good but current entry premium too high
    WAIT_BREAKOUT = "wait_breakout"  # Setup forming, not yet confirmed
    AVOID = "avoid"                  # Real red flags present


class PositionAction(str, Enum):
    """Refined action for an already-held position, evaluated every 5 min.

    Profit-aware action set under v2's max-profit framing. See
    ``docs/LLM_MODEL_V2_REFINEMENTS.md § B.1`` (Layer 3) for the full
    action -> order mapping the TradePolicy applies.

    NO_OPINION means "trust the bracket + trailing stop"; it is the
    default when the LLM has no strong refinement to offer, and it is
    also what gets returned for non-held tickers.
    """

    HOLD = "hold"                  # No change; trust bracket + trailing stop
    SCALE_UP = "scale_up"          # Add to a working position
    TAKE_PARTIAL = "take_partial"  # Sell 1/3 to 1/2 to bank profit, let rest run
    TRIM = "trim"                  # Sell 50% defensively (uncertainty rising)
    EXIT = "exit"                  # Close immediately (LLM call, not stop hit)
    TIGHTEN_STOP = "tighten_stop"  # LLM-driven stop tightening beyond ratchet
    NO_OPINION = "no_opinion"      # Defer to bracket + trailing stop


# ---------------------------------------------------------------------------
# LLMAnalysis — the v2 classification record
# ---------------------------------------------------------------------------


class LLMAnalysis(BaseModel):
    """Classification output from the LLM. Inputs to TradePolicy.

    Pinned to ``docs/LLM_MODEL_V2_REFINEMENTS.md § A.1``. Bump
    ``prompt_version`` in ``config/settings.yaml`` when changing fields.
    """

    # ---- Catalyst & setup classification (required) ----
    catalyst_quality: CatalystQuality
    setup_type: SetupType
    trade_readiness: TradeReadiness

    # ---- Risk identification ----
    invalid_if: str = Field(max_length=200)
    """Single plain-English condition that would void this thesis.

    Examples:
      - "if SPY breaks below 5950 on a closing 5-min bar"
      - "if NVDA fails to hold above PM low of 147.20"
    """

    primary_concerns: list[str] = Field(default_factory=list)
    """1-5 short tags for risks the operator should be aware of.

    Trimmed to <=5 non-empty entries by the validator below.

    Examples: ["mega_cap_gap_fade", "VIX_low", "earnings_within_3d"]
    """

    counter_thesis: str = Field(max_length=200)
    """The opposing argument in one sentence. Forces the LLM to consider
    both sides and gives the policy layer a perspective when the primary
    action seems weak."""

    suggested_horizon: Literal["intraday", "overnight", "multi_day"] = "intraday"

    # ---- Position management (only meaningful when ctx.currently_holding) ----
    position_action: PositionAction = PositionAction.NO_OPINION
    position_action_reasoning: str = Field(default="", max_length=200)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("primary_concerns")
    @classmethod
    def _trim_concerns(cls, v: list[str]) -> list[str]:
        """Cap primary_concerns at 5; drop empties / whitespace-only entries."""
        return [c.strip() for c in v if c and c.strip()][:5]

    # ---- Permissive normalization (mode="before") ----
    # Mirrors the pattern in LLMDecision. Anthropic's tool-use enforces
    # required fields and enum/type constraints but NOT string maxLength.
    # We truncate over-long strings to valid in-range values so an LLM
    # that produces a slightly-too-long invalid_if doesn't lose the entire
    # response to ValidationError. Type/enum violations still fail.

    @field_validator("invalid_if", mode="before")
    @classmethod
    def _truncate_invalid_if(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @field_validator("counter_thesis", mode="before")
    @classmethod
    def _truncate_counter_thesis(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v

    @field_validator("position_action_reasoning", mode="before")
    @classmethod
    def _truncate_position_action_reasoning(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 200:
            return v[:197] + "..."
        return v


# ---------------------------------------------------------------------------
# LLMOutput — the wire wrapper combining analysis + advisory + raw payload
# ---------------------------------------------------------------------------


class LLMOutput(BaseModel):
    """The full record returned by a tier client and persisted for audit.

    ``analysis`` and ``advisory`` are the parsed views; ``raw_response``
    keeps the untruncated, unvalidated JSON the LLM emitted so post-hoc
    debugging never loses information.
    """

    analysis: LLMAnalysis
    advisory: LLMDecision
    raw_response: dict[str, Any] = Field(default_factory=dict)
