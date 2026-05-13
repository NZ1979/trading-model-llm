"""Tests for the v2 LLMAnalysis schema (docs/LLM_MODEL_V2_REFINEMENTS.md § A.1).

Covers:
  - All four enums (CatalystQuality, SetupType, TradeReadiness, PositionAction)
  - Required-field validation
  - Default values for optional fields
  - Defensive string truncation on invalid_if / counter_thesis /
    position_action_reasoning (mirrors LLMDecision's max_length-defeat
    pattern: Anthropic tool-use doesn't enforce maxLength, so we truncate
    at parse time rather than fail)
  - primary_concerns trimming (drop empties + cap at 5)
  - Type/enum validation rejection of bad input
  - LLMOutput wrapper combining LLMAnalysis + LLMDecision
"""
from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, ".")

from strategy.llm.analysis import (
    CatalystQuality,
    LLMAnalysis,
    LLMOutput,
    PositionAction,
    SetupType,
    TradeReadiness,
)
from strategy.llm.types import LLMDecision


# ---------------------------------------------------------------------------
# Fixtures: minimal valid payloads
# ---------------------------------------------------------------------------


def _valid_analysis_payload(**overrides) -> dict:
    """Return a minimal valid LLMAnalysis payload. Overrides merge on top."""
    base = {
        "catalyst_quality": "material",
        "setup_type": "gap_and_go",
        "trade_readiness": "ready",
        "invalid_if": "if SPY breaks below 5950 on a closing 5-min bar",
        "counter_thesis": "Mega-cap gap-ups often fade by 10:30 ET.",
    }
    base.update(overrides)
    return base


def _valid_decision_payload(**overrides) -> dict:
    """Minimal valid LLMDecision payload for the LLMOutput wrapper tests."""
    base = {
        "action": "Buy",
        "confidence": 65,
        "setup_label": "gap_and_go",
        "reasoning": "Pre-market gap +3.2% on FDA approval with rising RVOL.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


def test_catalyst_quality_values():
    assert {q.value for q in CatalystQuality} == {
        "major", "material", "minor", "ambiguous", "none",
    }


def test_setup_type_values():
    assert {s.value for s in SetupType} == {
        "gap_and_go", "pullback_in_trend", "breakout_confirm",
        "breakdown", "reversal", "consolidation", "no_setup",
    }


def test_trade_readiness_values():
    assert {r.value for r in TradeReadiness} == {
        "ready", "wait_pullback", "wait_breakout", "avoid",
    }


def test_position_action_values():
    assert {p.value for p in PositionAction} == {
        "hold", "scale_up", "take_partial", "trim",
        "exit", "tighten_stop", "no_opinion",
    }


# ---------------------------------------------------------------------------
# LLMAnalysis: required fields + defaults
# ---------------------------------------------------------------------------


def test_minimal_valid_payload_parses():
    a = LLMAnalysis(**_valid_analysis_payload())
    assert a.catalyst_quality is CatalystQuality.MATERIAL
    assert a.setup_type is SetupType.GAP_AND_GO
    assert a.trade_readiness is TradeReadiness.READY
    # Defaults
    assert a.suggested_horizon == "intraday"
    assert a.position_action is PositionAction.NO_OPINION
    assert a.position_action_reasoning == ""
    assert a.primary_concerns == []


def test_all_optional_fields_can_be_set():
    a = LLMAnalysis(**_valid_analysis_payload(
        primary_concerns=["VIX_low", "earnings_within_3d"],
        suggested_horizon="overnight",
        position_action="take_partial",
        position_action_reasoning="Up 4R; bank half, trail rest.",
    ))
    assert a.primary_concerns == ["VIX_low", "earnings_within_3d"]
    assert a.suggested_horizon == "overnight"
    assert a.position_action is PositionAction.TAKE_PARTIAL


def test_missing_required_field_raises():
    payload = _valid_analysis_payload()
    payload.pop("catalyst_quality")
    with pytest.raises(ValidationError) as exc:
        LLMAnalysis(**payload)
    assert "catalyst_quality" in str(exc.value)


def test_invalid_enum_value_raises():
    payload = _valid_analysis_payload(catalyst_quality="EARTH_SHATTERING")
    with pytest.raises(ValidationError):
        LLMAnalysis(**payload)


def test_invalid_suggested_horizon_raises():
    payload = _valid_analysis_payload(suggested_horizon="lunchtime")
    with pytest.raises(ValidationError):
        LLMAnalysis(**payload)


# ---------------------------------------------------------------------------
# Defensive truncation (mode="before" validators)
# ---------------------------------------------------------------------------


def test_invalid_if_truncated_when_over_200():
    long_str = "a" * 300
    a = LLMAnalysis(**_valid_analysis_payload(invalid_if=long_str))
    assert len(a.invalid_if) == 200
    assert a.invalid_if.endswith("...")
    assert a.invalid_if.startswith("a" * 197)


def test_invalid_if_unchanged_when_under_200():
    s = "if AAPL fails to hold above 175.00"
    a = LLMAnalysis(**_valid_analysis_payload(invalid_if=s))
    assert a.invalid_if == s


def test_counter_thesis_truncated_when_over_200():
    long_str = "b" * 250
    a = LLMAnalysis(**_valid_analysis_payload(counter_thesis=long_str))
    assert len(a.counter_thesis) == 200
    assert a.counter_thesis.endswith("...")


def test_position_action_reasoning_truncated_when_over_200():
    long_str = "c" * 300
    a = LLMAnalysis(**_valid_analysis_payload(position_action_reasoning=long_str))
    assert len(a.position_action_reasoning) == 200
    assert a.position_action_reasoning.endswith("...")


# ---------------------------------------------------------------------------
# primary_concerns trimming
# ---------------------------------------------------------------------------


def test_primary_concerns_empty_default():
    a = LLMAnalysis(**_valid_analysis_payload())
    assert a.primary_concerns == []


def test_primary_concerns_strips_whitespace():
    a = LLMAnalysis(**_valid_analysis_payload(
        primary_concerns=["  VIX_low  ", "earnings_within_3d "],
    ))
    assert a.primary_concerns == ["VIX_low", "earnings_within_3d"]


def test_primary_concerns_drops_empties_and_whitespace_only():
    a = LLMAnalysis(**_valid_analysis_payload(
        primary_concerns=["VIX_low", "", "   ", "earnings_within_3d"],
    ))
    assert a.primary_concerns == ["VIX_low", "earnings_within_3d"]


def test_primary_concerns_capped_at_five():
    a = LLMAnalysis(**_valid_analysis_payload(
        primary_concerns=[f"concern_{i}" for i in range(10)],
    ))
    assert len(a.primary_concerns) == 5
    assert a.primary_concerns == [f"concern_{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# LLMOutput wrapper
# ---------------------------------------------------------------------------


def test_llm_output_wraps_analysis_and_advisory():
    out = LLMOutput(
        analysis=LLMAnalysis(**_valid_analysis_payload()),
        advisory=LLMDecision(**_valid_decision_payload()),
        raw_response={"foo": "bar"},
    )
    assert out.analysis.setup_type is SetupType.GAP_AND_GO
    assert out.advisory.action == "Buy"
    assert out.advisory.confidence == 65
    assert out.raw_response == {"foo": "bar"}


def test_llm_output_raw_response_defaults_to_empty_dict():
    out = LLMOutput(
        analysis=LLMAnalysis(**_valid_analysis_payload()),
        advisory=LLMDecision(**_valid_decision_payload()),
    )
    assert out.raw_response == {}


def test_llm_output_rejects_wrong_nested_type():
    # advisory must be an LLMDecision-shaped payload, not arbitrary dict
    with pytest.raises(ValidationError):
        LLMOutput(
            analysis=LLMAnalysis(**_valid_analysis_payload()),
            advisory={"not": "a valid decision"},
            raw_response={},
        )
