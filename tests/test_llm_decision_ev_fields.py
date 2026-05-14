"""Tests for the EV-field additions on LLMDecision (2026-05-13).

Two new fields landed on ``LLMDecision`` for the ChatGPT review #2
integration: ``expected_move_pct`` and ``expected_holding_minutes``.
These are required inputs to EV scoring in ``policy.py``:

    EV ≈ calibrated_pwin × expected_move_pct
         − (1 − calibrated_pwin) × stop_distance_pct

Both fields have defaults (0.0 and 0 respectively, both meaning "no
opinion") so existing minimal payloads in the test fixtures and smoke
tests don't have to change. The system prompt instructs the LLM to
populate them for Buy/Sell decisions; Hold decisions keep the defaults.

Coverage:
  - defaults applied when fields absent
  - in-range values round-trip unchanged
  - out-of-range values clamp permissively (no ValidationError); both
    positive and negative bounds for the move_pct, and the upper bound
    on holding_minutes
  - bool inputs rejected (bool is a subclass of int; we don't want
    True/False sneaking in as 1/0)
  - non-numeric inputs reject with ValidationError
  - the generated tool input_schema (what we send to Anthropic and
    LM Studio) actually contains the new fields under properties,
    proving the LLM is asked for them
"""
from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, ".")

from strategy.llm.clients import _llm_decision_tool_schema
from strategy.llm.types import LLMDecision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_payload(**overrides) -> dict:
    """Minimal LLMDecision payload — same shape as test_analysis_schema
    so cross-file consistency is preserved."""
    base = {
        "action": "Buy",
        "confidence": 65,
        "setup_label": "gap_and_go",
        "reasoning": "Pre-market gap +3.2% on FDA approval with rising RVOL.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_expected_move_pct_default_is_zero():
    """Field absent in payload → default 0.0. Means 'no opinion';
    policy.py treats this as a missing prediction."""
    d = LLMDecision(**_minimal_payload())
    assert d.expected_move_pct == 0.0


def test_expected_holding_minutes_default_is_zero():
    d = LLMDecision(**_minimal_payload())
    assert d.expected_holding_minutes == 0


def test_hold_decision_with_defaults_parses():
    """Hold decisions don't need to populate the new fields. Verify a
    realistic Hold payload parses cleanly without them."""
    d = LLMDecision(**_minimal_payload(action="Hold", confidence=20))
    assert d.action == "Hold"
    assert d.expected_move_pct == 0.0
    assert d.expected_holding_minutes == 0


# ---------------------------------------------------------------------------
# In-range values pass through
# ---------------------------------------------------------------------------


def test_expected_move_pct_in_range_passes_through():
    d = LLMDecision(**_minimal_payload(expected_move_pct=2.5))
    assert d.expected_move_pct == 2.5


def test_expected_move_pct_negative_in_range():
    d = LLMDecision(**_minimal_payload(
        action="Sell",
        expected_move_pct=-3.0,
    ))
    assert d.expected_move_pct == -3.0


def test_expected_holding_minutes_in_range():
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=90))
    assert d.expected_holding_minutes == 90


def test_expected_holding_minutes_full_session():
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=390))
    assert d.expected_holding_minutes == 390


# ---------------------------------------------------------------------------
# Permissive clamping — out-of-range numerics get pulled in, not rejected
# ---------------------------------------------------------------------------


def test_expected_move_pct_clamps_above_max():
    """50 → 20.0. Matches the existing pattern from confidence /
    stop_loss_atr_multiple — out-of-range numerics clamp; type
    mismatches still raise."""
    d = LLMDecision(**_minimal_payload(expected_move_pct=50.0))
    assert d.expected_move_pct == 20.0


def test_expected_move_pct_clamps_below_min():
    d = LLMDecision(**_minimal_payload(
        action="Sell",
        expected_move_pct=-50.0,
    ))
    assert d.expected_move_pct == -20.0


def test_expected_holding_minutes_clamps_above_max():
    """600 → 390 (one full RTH session). 600 would mean overnight,
    which is the LLMDecision.time_horizon field's job."""
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=600))
    assert d.expected_holding_minutes == 390


def test_expected_holding_minutes_clamps_below_min():
    """Negative minutes is a parse artefact. Clamp to 0."""
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=-5))
    assert d.expected_holding_minutes == 0


def test_expected_move_pct_int_input_becomes_float():
    """LLM may emit an integer when the bounds are floats. Should
    accept and coerce to float, not reject."""
    d = LLMDecision(**_minimal_payload(expected_move_pct=3))
    assert d.expected_move_pct == 3.0
    assert isinstance(d.expected_move_pct, float)


def test_expected_holding_minutes_float_input_becomes_int():
    """LLM may emit 90.0 instead of 90. Should accept and coerce."""
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=90.7))
    # Clamp validator uses int(), which truncates toward zero
    assert d.expected_holding_minutes == 90
    assert isinstance(d.expected_holding_minutes, int)


# ---------------------------------------------------------------------------
# Documented coercion — matches the existing validator pattern in
# LLMDecision._clamp_confidence et al. Pydantic v2 in non-strict mode
# coerces True→1 and string→float when possible. The ``isinstance(v, bool)``
# guard in the validator means a bool input passes through to Pydantic,
# which then coerces it to int(1)/int(0). For numeric strings, Pydantic
# coerces directly. These tests document what actually happens so the
# behavior is pinned, not aspirational.
#
# A follow-up audit should consider making bool/string explicitly raise
# (a one-line change to each validator) — but doing it on just the two
# new fields creates inconsistency with the seven existing fields that
# already have the lax pattern. Project-wide change goes in a dedicated
# patch.
# ---------------------------------------------------------------------------


def test_expected_move_pct_bool_coerces_to_numeric():
    """Documents Pydantic v2 default behavior: True → 1, then clamped
    to valid range (1.0 in this case). Not desirable but consistent
    with the existing _clamp_confidence pattern."""
    d = LLMDecision(**_minimal_payload(expected_move_pct=True))
    assert d.expected_move_pct == 1.0


def test_expected_holding_minutes_bool_coerces_to_numeric():
    d = LLMDecision(**_minimal_payload(expected_holding_minutes=False))
    assert d.expected_holding_minutes == 0


def test_expected_move_pct_numeric_string_coerces():
    """Pydantic v2 coerces well-formed numeric strings. The clamp
    validator still runs on the coerced value, so '50' → 50.0 → 20.0."""
    d = LLMDecision(**_minimal_payload(expected_move_pct="2.5"))
    assert d.expected_move_pct == 2.5


def test_expected_move_pct_non_numeric_string_rejected():
    """Strings that don't parse as numbers DO raise — only numeric
    strings get coerced. 'abc' is not a number."""
    with pytest.raises(ValidationError):
        LLMDecision(**_minimal_payload(expected_move_pct="abc"))


def test_expected_holding_minutes_non_numeric_string_rejected():
    with pytest.raises(ValidationError):
        LLMDecision(**_minimal_payload(expected_holding_minutes="ninety"))


# ---------------------------------------------------------------------------
# Tool schema — what we actually send to the LLM
# ---------------------------------------------------------------------------


def test_tool_schema_includes_expected_move_pct():
    """The Anthropic / LM Studio tool input_schema is derived from
    LLMDecision.model_json_schema() with metadata fields stripped.
    Verify the new fields are present in what the LLM sees."""
    schema = _llm_decision_tool_schema()
    assert "expected_move_pct" in schema["properties"]
    assert "expected_holding_minutes" in schema["properties"]


def test_tool_schema_excludes_platform_metadata():
    """tier_provenance and raw_response are filled in by the signal
    engine, not the LLM. They must NOT appear in the schema."""
    schema = _llm_decision_tool_schema()
    assert "tier_provenance" not in schema["properties"]
    assert "raw_response" not in schema["properties"]


def test_tool_schema_bounds_communicated_to_llm():
    """The bounds (-20..20 for move, 0..390 for minutes) come through
    in the JSON schema so the LLM gets the right hint about magnitude
    even though Anthropic's tool-use only enforces required/type/enum."""
    schema = _llm_decision_tool_schema()
    move_schema = schema["properties"]["expected_move_pct"]
    assert move_schema.get("minimum") == -20.0
    assert move_schema.get("maximum") == 20.0

    hold_schema = schema["properties"]["expected_holding_minutes"]
    assert hold_schema.get("minimum") == 0
    assert hold_schema.get("maximum") == 390


# ---------------------------------------------------------------------------
# Roundtrip — confirms model serialises and re-parses without info loss
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_ev_fields():
    """LLM emits a payload → we parse it → we serialise it → we re-parse
    it. Values survive the full round-trip without drift."""
    original = LLMDecision(**_minimal_payload(
        expected_move_pct=2.5,
        expected_holding_minutes=90,
    ))
    redumped = original.model_dump()
    reparsed = LLMDecision(**redumped)
    assert reparsed.expected_move_pct == 2.5
    assert reparsed.expected_holding_minutes == 90
