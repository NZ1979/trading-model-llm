"""Tests for the policy.py foundation (2026-05-13).

Covers the first land of ``strategy/llm/policy.py``:

  - All four dataclass shapes (PolicyInput, MarketFeatures, BucketStats,
    AccountState, FinalTradeDecision) and PolicyConfig
  - The 9-step decision tree's first four early-Hold gates (steps 1-4)
  - The position-management mapping (6 PositionAction cases)
  - Internal helpers: _bucket_key_from_input shape,
    _time_of_day_bucket boundaries, _make_hold consistency
  - Safe-stub behavior of steps 5-8 (deferred): empty bucket → tiny,
    populated bucket → normal; advisory stop/TP passes through

Steps 5-8 (bucket fallback, tier sizing, red-flag downgrade, clamps)
land in the next session and will get their own dedicated tests.
ChatGPT review #2 additions (liquidity gate, EV, ranking) land after
that and will get their own tests too.

Fixtures: building a valid LLMContext requires a lot of fields. We use
a single _valid_policy_input() factory with sensible defaults and
**override**-style customization so each test only specifies the bits
it cares about.
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, ".")

from strategy.llm.analysis import (
    CatalystQuality,
    LLMAnalysis,
    PositionAction,
    SetupType,
    TradeReadiness,
)
from strategy.llm.policy import (
    AccountState,
    BucketStats,
    FinalTradeDecision,
    MarketFeatures,
    PolicyConfig,
    PolicyInput,
    _bucket_key_from_input,
    _make_hold,
    _time_of_day_bucket,
    _translate_position_action,
    decide,
)
from strategy.llm.types import LLMContext, LLMDecision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_analysis(**overrides) -> LLMAnalysis:
    """Minimal valid LLMAnalysis. Overrides merge by Pydantic constructor."""
    base = {
        "catalyst_quality": CatalystQuality.MATERIAL,
        "setup_type": SetupType.GAP_AND_GO,
        "trade_readiness": TradeReadiness.READY,
        "invalid_if": "if SPY breaks below 5950 on a closing 5-min bar",
        "counter_thesis": "Mega-cap gap-ups often fade by 10:30 ET.",
    }
    base.update(overrides)
    return LLMAnalysis(**base)


def _valid_decision(**overrides) -> LLMDecision:
    """Minimal valid LLMDecision payload."""
    base = {
        "action": "Buy",
        "confidence": 65,
        "setup_label": "gap_and_go",
        "reasoning": "Pre-market gap +3.2% on FDA approval with rising RVOL.",
    }
    base.update(overrides)
    return LLMDecision(**base)


def _valid_context(**overrides) -> LLMContext:
    """Minimal valid LLMContext. Most fields default sensibly on the
    dataclass; we override the ones the policy actually reads."""
    base = {
        "ticker": "AAPL",
        "timestamp_et": "2026-05-13T10:00:00",
        "prompt_version": "v0.1-ev-fields",
        "market_regime_label": "trending_up",
        "market_cap_bucket": "mega",
        "minutes_since_open": 30,
        "currently_holding": False,
        "position_qty": 0,
    }
    base.update(overrides)
    return LLMContext(**base)


def _valid_features(**overrides) -> MarketFeatures:
    base = {
        "rvol_percentile": 75.0,
        "spread_bps": 5.0,
        "distance_to_vwap_atr": 0.5,
        "distance_to_stop_atr": None,
        "distance_to_target_atr": None,
        "has_red_flag": False,
    }
    base.update(overrides)
    return MarketFeatures(**base)


def _valid_account(**overrides) -> AccountState:
    base = {
        "equity": 100_000.0,
        "total_exposure_pct": 0.0,
        "open_position_count": 0,
    }
    base.update(overrides)
    return AccountState(**base)


def _populated_bucket(**overrides) -> BucketStats:
    """A bucket with enough samples to graduate from tiny."""
    base = {
        "bucket_key": ("trending_up", "mega", "material", "open_drive", "long"),
        "sample_count": 50,
        "expected_r": 0.4,
        "expected_r_lower_ci": 0.2,
        "win_rate": 0.55,
        "avg_win_r": 1.5,
        "avg_loss_r": -1.0,
        "last_updated": datetime(2026, 5, 13),
    }
    base.update(overrides)
    return BucketStats(**base)


def _valid_policy_input(**overrides) -> PolicyInput:
    """Assemble a complete PolicyInput. Overrides apply to the input
    itself; sub-fixtures (analysis, advisory, etc.) need their own
    overrides via dedicated factories or direct construction."""
    base = {
        "ctx": _valid_context(),
        "analysis": _valid_analysis(),
        "advisory": _valid_decision(),
        "features": _valid_features(),
        "account": _valid_account(),
        "bucket_history": _populated_bucket(),
        "health_state": "healthy",
    }
    base.update(overrides)
    return PolicyInput(**base)


# ---------------------------------------------------------------------------
# PolicyConfig — Q3 defaults
# ---------------------------------------------------------------------------


def test_policy_config_defaults_match_q3_resolution():
    """Q3 resolution table values, pinned. If any tuner output diverges
    from this baseline without a documented bump, something has drifted."""
    config = PolicyConfig()
    # Tunable
    assert config.sample_min_for_normal_tier == 30
    assert config.sample_min_for_max_tier == 100
    assert config.expected_r_for_max_tier == 0.30
    assert config.spread_bps_red_flag == 50.0
    assert config.rvol_percentile_red_flag == 20.0
    assert config.stop_atr_clamp_choppy == 2.0
    assert config.min_reward_to_risk == 1.5
    assert config.escalation_confidence_low == 50
    assert config.escalation_confidence_high == 75
    assert config.trim_pct_default == 0.5
    # Untunable
    assert config.risk_per_trade_pct == 1.0
    assert config.max_drawdown_pct == 15.0
    assert config.single_day_loss_pct == 5.0
    assert config.total_exposure_pct == 90.0
    assert config.max_holding_days == 3
    # Version
    assert config.policy_version == "0.1.0"


# ---------------------------------------------------------------------------
# BucketStats.empty
# ---------------------------------------------------------------------------


def test_bucket_stats_empty_has_zero_samples():
    key = ("trending_up", "mega", "material", "open_drive", "long")
    b = BucketStats.empty(key)
    assert b.sample_count == 0
    assert b.expected_r_lower_ci == 0.0
    assert b.bucket_key == key


# ---------------------------------------------------------------------------
# _time_of_day_bucket — boundary checks
# ---------------------------------------------------------------------------


def test_time_of_day_bucket_open_drive():
    assert _time_of_day_bucket(0) == "open_drive"
    assert _time_of_day_bucket(30) == "open_drive"


def test_time_of_day_bucket_morning():
    assert _time_of_day_bucket(31) == "morning"
    assert _time_of_day_bucket(90) == "morning"


def test_time_of_day_bucket_midday():
    assert _time_of_day_bucket(91) == "midday"
    assert _time_of_day_bucket(270) == "midday"


def test_time_of_day_bucket_power_hour():
    assert _time_of_day_bucket(271) == "power_hour"
    assert _time_of_day_bucket(360) == "power_hour"


def test_time_of_day_bucket_close():
    assert _time_of_day_bucket(361) == "close"
    assert _time_of_day_bucket(390) == "close"


# ---------------------------------------------------------------------------
# _bucket_key_from_input
# ---------------------------------------------------------------------------


def test_bucket_key_shape_is_five_tuple():
    inp = _valid_policy_input()
    key = _bucket_key_from_input(inp)
    assert isinstance(key, tuple)
    assert len(key) == 5


def test_bucket_key_components_in_order():
    """Order matches the Q1 resolution: (regime, cap, catalyst, time, long_short)."""
    inp = _valid_policy_input(
        ctx=_valid_context(
            market_regime_label="choppy",
            market_cap_bucket="small",
            minutes_since_open=200,  # midday
        ),
        analysis=_valid_analysis(catalyst_quality=CatalystQuality.MAJOR),
        advisory=_valid_decision(action="Sell"),
    )
    key = _bucket_key_from_input(inp)
    assert key == ("choppy", "small", "major", "midday", "short")


# ---------------------------------------------------------------------------
# _make_hold consistency
# ---------------------------------------------------------------------------


def test_make_hold_sets_consistent_fields():
    inp = _valid_policy_input()
    config = PolicyConfig()
    result = _make_hold(inp, config, reason="some_reason")
    assert result.action == "Hold"
    assert result.qty_tier == "zero"
    assert result.rejection_reason == "some_reason"
    assert result.policy_version == config.policy_version
    # Stop/TP passed through from advisory
    assert result.stop_loss_atr_multiple == inp.advisory.stop_loss_atr_multiple
    assert result.take_profit_atr_multiple == inp.advisory.take_profit_atr_multiple


# ---------------------------------------------------------------------------
# Decision tree step 1: health gate
# ---------------------------------------------------------------------------


def test_health_tier1_down_holds():
    inp = _valid_policy_input(health_state="tier1_down")
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "health_not_ok"


def test_health_tier2_down_holds():
    inp = _valid_policy_input(health_state="tier2_down")
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "health_not_ok"


def test_health_degraded_holds():
    inp = _valid_policy_input(health_state="degraded")
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "health_not_ok"


def test_health_halt_holds():
    inp = _valid_policy_input(health_state="halt")
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "health_not_ok"


def test_health_healthy_does_not_short_circuit_to_hold():
    """healthy is the only state that lets the policy proceed past step 1."""
    inp = _valid_policy_input(health_state="healthy")
    result = decide(inp, PolicyConfig())
    # With a populated bucket and a Buy advisory, the policy should
    # approve at the safe-stub default of tier=normal.
    assert result.action == "Buy"
    assert result.rejection_reason is None


# ---------------------------------------------------------------------------
# Decision tree step 2: LLM AVOID veto
# ---------------------------------------------------------------------------


def test_avoid_readiness_holds():
    """AVOID is the LLM's hard veto — overrides any positive Buy advisory."""
    inp = _valid_policy_input(
        analysis=_valid_analysis(trade_readiness=TradeReadiness.AVOID),
        advisory=_valid_decision(action="Buy"),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "llm_avoid"


def test_avoid_holds_even_with_high_confidence_buy():
    """Confidence doesn't override the AVOID veto."""
    inp = _valid_policy_input(
        analysis=_valid_analysis(trade_readiness=TradeReadiness.AVOID),
        advisory=_valid_decision(action="Buy", confidence=95),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "llm_avoid"


# ---------------------------------------------------------------------------
# Decision tree step 3: clamp_anomaly
# ---------------------------------------------------------------------------


def test_clamp_anomaly_holds_when_attribute_present():
    """clamp_anomaly is a field that will land on LLMAnalysis later.
    Until then the getattr default of False is what runs. Simulate the
    eventual True case by monkey-attaching the field (frozen=False on
    BaseModel for this test only — Pydantic permits it under extra-allow,
    but the simpler path is to subclass)."""
    base_analysis = _valid_analysis()
    # Pydantic BaseModel: set via private bypass for the test
    object.__setattr__(base_analysis, "clamp_anomaly", True)
    inp = _valid_policy_input(analysis=base_analysis)
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "clamp_anomaly"


def test_clamp_anomaly_default_false_does_not_hold():
    """Default analysis has no clamp_anomaly attribute → getattr returns
    False → step 3 doesn't fire."""
    inp = _valid_policy_input()
    result = decide(inp, PolicyConfig())
    # Should NOT be a clamp_anomaly Hold (might be Buy or another Hold,
    # but not THIS rejection reason).
    assert result.rejection_reason != "clamp_anomaly"


# ---------------------------------------------------------------------------
# Decision tree step 4: advisory Hold
# ---------------------------------------------------------------------------


def test_advisory_hold_propagates_with_no_rejection_reason():
    """When the LLM says Hold and no upstream gate fired, policy agrees
    without flagging a rejection — this is the LLM and policy aligning,
    not the policy overriding."""
    inp = _valid_policy_input(
        advisory=_valid_decision(action="Hold", confidence=30),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason is None
    assert result.qty_tier == "zero"


# ---------------------------------------------------------------------------
# Decision tree step 4b: currently_holding triggers position_action
# ---------------------------------------------------------------------------


def test_currently_holding_triggers_position_action_translation():
    """When holding the name, the policy translates analysis.position_action,
    ignoring the advisory's open/close framing."""
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.EXIT),
        advisory=_valid_decision(action="Buy"),  # advisory ignored when holding
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Sell"  # EXIT → Sell
    assert result.qty_tier == "normal"


# ---------------------------------------------------------------------------
# Position-action mapping — all 6 cases
# ---------------------------------------------------------------------------


def test_position_action_hold_results_in_hold():
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.HOLD),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason is None  # not a rejection, just no-op
    assert result.qty_tier == "zero"


def test_position_action_no_opinion_results_in_hold():
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.NO_OPINION),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason is None


def test_position_action_trim_results_in_sell_normal():
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.TRIM),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Sell"
    assert result.qty_tier == "normal"


def test_position_action_take_partial_results_in_sell_normal():
    """TAKE_PARTIAL and TRIM both produce Sell at tier=normal; the
    orchestrator's sizing applies trim_pct_default for both."""
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.TAKE_PARTIAL),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Sell"
    assert result.qty_tier == "normal"


def test_position_action_exit_results_in_sell_normal():
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.EXIT),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Sell"
    assert result.qty_tier == "normal"


def test_position_action_tighten_stop_results_in_hold():
    """TIGHTEN_STOP intentionally returns Hold — the stop replacement
    is an out-of-band order the orchestrator issues directly."""
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.TIGHTEN_STOP),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason is None
    assert result.qty_tier == "zero"


def test_position_action_scale_up_long_results_in_buy():
    """SCALE_UP infers direction from position_qty sign. Long = +qty = Buy."""
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=100),
        analysis=_valid_analysis(position_action=PositionAction.SCALE_UP),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Buy"
    assert result.qty_tier == "normal"


def test_position_action_scale_up_short_results_in_sell():
    """SCALE_UP on a short position = Sell to add to the short."""
    inp = _valid_policy_input(
        ctx=_valid_context(currently_holding=True, position_qty=-100),
        analysis=_valid_analysis(position_action=PositionAction.SCALE_UP),
    )
    result = _translate_position_action(inp, PolicyConfig())
    assert result.action == "Sell"
    assert result.qty_tier == "normal"


# ---------------------------------------------------------------------------
# Step 9 — safe-stub bucket sizing (until step 6 lands)
# ---------------------------------------------------------------------------


def test_happy_path_buy_with_populated_bucket_is_normal_tier():
    """Populated bucket + healthy gate + advisory Buy + no AVOID + no
    holding → approve at tier=normal.

    Note: default LLMDecision has stop=1.5, TP=2.0, R/R = 1.33 which is
    below the default min_reward_to_risk=1.5. The clamp (step 8) raises
    TP to 1.5 × 1.5 = 2.25; stop passes through (non-choppy regime).
    Dedicated step-8 tests cover the clamp in detail."""
    inp = _valid_policy_input()  # all defaults → populated bucket, trending_up regime
    config = PolicyConfig()
    result = decide(inp, config)
    assert result.action == "Buy"
    assert result.qty_tier == "normal"
    assert result.rejection_reason is None
    # Stop passes through (trending_up regime, no choppy clamp).
    assert result.stop_loss_atr_multiple == 1.5
    # TP clamped up to meet min_reward_to_risk (R/R = 2.25/1.5 = 1.5 exactly).
    assert result.take_profit_atr_multiple == 2.25


def test_happy_path_buy_with_empty_bucket_is_tiny_tier():
    """Empty bucket forces tier=tiny — safe stub until step 6 lands."""
    empty_bucket = BucketStats.empty(
        ("trending_up", "mega", "material", "open_drive", "long")
    )
    inp = _valid_policy_input(bucket_history=empty_bucket)
    result = decide(inp, PolicyConfig())
    assert result.action == "Buy"
    assert result.qty_tier == "tiny"
    assert result.rejection_reason is None


def test_happy_path_sell_with_populated_bucket():
    """Mirror of the Buy test — short-side advisory passes through."""
    inp = _valid_policy_input(
        advisory=_valid_decision(action="Sell", expected_move_pct=-2.0),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Sell"
    assert result.qty_tier == "normal"
    assert result.rejection_reason is None


def test_final_decision_records_bucket_key_used():
    """FinalTradeDecision.bucket_key is populated even on Hold so the
    audit row has the data that informed the decision."""
    inp = _valid_policy_input(health_state="halt")
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert isinstance(result.bucket_key, tuple)
    assert len(result.bucket_key) == 5


def test_policy_version_populated_in_every_decision():
    """policy_version on every FinalTradeDecision so audit queries can
    filter by SemVer (Q4)."""
    config = PolicyConfig()
    for inp in (
        _valid_policy_input(),  # happy path
        _valid_policy_input(health_state="tier1_down"),  # gate-1 Hold
        _valid_policy_input(
            analysis=_valid_analysis(trade_readiness=TradeReadiness.AVOID)
        ),  # gate-2 Hold
        _valid_policy_input(advisory=_valid_decision(action="Hold")),  # gate-4 Hold
    ):
        result = decide(inp, config)
        assert result.policy_version == config.policy_version


# ---------------------------------------------------------------------------
# Gate precedence — earlier gates win
# ---------------------------------------------------------------------------


def test_health_gate_beats_avoid_when_both_fire():
    """Step 1 runs before step 2; health_not_ok wins."""
    inp = _valid_policy_input(
        health_state="halt",
        analysis=_valid_analysis(trade_readiness=TradeReadiness.AVOID),
    )
    result = decide(inp, PolicyConfig())
    assert result.rejection_reason == "health_not_ok"


def test_avoid_beats_advisory_hold_when_both_fire():
    """Step 2 runs before step 4; llm_avoid wins over the silent Hold-
    pass-through."""
    inp = _valid_policy_input(
        analysis=_valid_analysis(trade_readiness=TradeReadiness.AVOID),
        advisory=_valid_decision(action="Hold"),
    )
    result = decide(inp, PolicyConfig())
    # AVOID is loud (rejection reason set); Hold would be silent (None)
    assert result.rejection_reason == "llm_avoid"


# ===========================================================================
# Steps 5-8 — added 2026-05-13 (second slice)
# ===========================================================================


from strategy.llm.policy import (  # noqa: E402
    _apply_red_flag_downgrade,
    _clamp_stop_target,
    _collapse_cap_size,
    _collapse_time_of_day,
    _tier_from_bucket,
    hierarchical_lookup,
)


# ---------------------------------------------------------------------------
# Step 5 — hierarchical_lookup
# ---------------------------------------------------------------------------


def test_hierarchical_lookup_returns_base_when_threshold_met():
    """Base key has enough samples → no collapse, return it as-is."""
    base = ("trending_up", "mega", "material", "open_drive", "long")
    populated = _populated_bucket(bucket_key=base, sample_count=50)
    table = {base: populated}
    result = hierarchical_lookup(base, table.get, sample_min=30)
    assert result.sample_count == 50
    assert result.bucket_key == base


def test_hierarchical_lookup_collapses_time_of_day_when_base_too_thin():
    """Base has <threshold; time-collapsed bucket has enough → use that."""
    base = ("trending_up", "mega", "material", "open_drive", "long")
    collapsed_time = ("trending_up", "mega", "material", "morning", "long")
    table = {
        base: _populated_bucket(bucket_key=base, sample_count=5),
        collapsed_time: _populated_bucket(bucket_key=collapsed_time, sample_count=80),
    }
    result = hierarchical_lookup(base, table.get, sample_min=30)
    assert result.sample_count == 80
    assert result.bucket_key == collapsed_time


def test_hierarchical_lookup_collapses_both_dims_when_needed():
    """Base + time-collapsed both thin; cap-collapsed has enough."""
    base = ("trending_up", "mega", "material", "open_drive", "long")
    time_only = ("trending_up", "mega", "material", "morning", "long")
    both = ("trending_up", "large_cap", "material", "morning", "long")
    table = {
        base: _populated_bucket(bucket_key=base, sample_count=3),
        time_only: _populated_bucket(bucket_key=time_only, sample_count=8),
        both: _populated_bucket(bucket_key=both, sample_count=120),
    }
    result = hierarchical_lookup(base, table.get, sample_min=30)
    assert result.sample_count == 120
    assert result.bucket_key == both


def test_hierarchical_lookup_returns_most_collapsed_when_all_empty():
    """Total cold start — no bucket meets threshold. Return the most-
    collapsed result; caller's tier logic routes to tiny on the
    sample_count == 0 gate."""
    base = ("trending_up", "mega", "material", "open_drive", "long")

    def empty_table(key: tuple) -> BucketStats | None:
        return None

    result = hierarchical_lookup(base, empty_table, sample_min=30)
    assert result.sample_count == 0
    # bucket_key reflects the most-collapsed (both_collapsed) variant
    assert result.bucket_key[1] == "large_cap"   # cap collapsed
    assert result.bucket_key[3] == "morning"     # time collapsed


def test_collapse_time_of_day_mapping():
    """Each of the 5 time labels collapses to the right 3-bucket label."""
    base = ("any", "any", "any", "OPEN", "any")
    for raw, expected in [
        ("open_drive", "morning"),
        ("morning", "morning"),
        ("midday", "midday"),
        ("power_hour", "afternoon"),
        ("close", "afternoon"),
    ]:
        k = (base[0], base[1], base[2], raw, base[4])
        assert _collapse_time_of_day(k)[3] == expected


def test_collapse_cap_size_mapping():
    """Each of the 5 cap labels collapses to the right 2-bucket label."""
    for raw, expected in [
        ("mega", "large_cap"),
        ("large", "large_cap"),
        ("mid", "small_cap"),
        ("small", "small_cap"),
        ("micro", "small_cap"),
        ("unknown", "unknown"),
    ]:
        k = ("any", raw, "any", "any", "any")
        assert _collapse_cap_size(k)[1] == expected


# ---------------------------------------------------------------------------
# Step 6 — _tier_from_bucket
# ---------------------------------------------------------------------------


def test_tier_from_bucket_zero_samples_is_tiny():
    """No samples at all → tiny (the bucket needs paper exploration)."""
    bucket = BucketStats.empty(("any",) * 5)
    tier, reason = _tier_from_bucket(bucket, PolicyConfig())
    assert tier == "tiny"
    assert reason is None


def test_tier_from_bucket_below_normal_threshold_is_tiny():
    bucket = _populated_bucket(sample_count=10, expected_r_lower_ci=0.5)
    tier, reason = _tier_from_bucket(bucket, PolicyConfig())
    assert tier == "tiny"
    assert reason is None


def test_tier_from_bucket_negative_lower_ci_rejects():
    """Enough samples to interpret the CI, but the CI lower bound is
    negative → reject. The caller converts this to a Hold."""
    bucket = _populated_bucket(sample_count=50, expected_r_lower_ci=-0.05)
    tier, reason = _tier_from_bucket(bucket, PolicyConfig())
    assert tier == "zero"
    assert reason == "bucket_negative_expectancy"


def test_tier_from_bucket_normal_default():
    """Above sample_min but below max conditions → normal."""
    bucket = _populated_bucket(sample_count=50, expected_r_lower_ci=0.2)
    tier, reason = _tier_from_bucket(bucket, PolicyConfig())
    assert tier == "normal"
    assert reason is None


def test_tier_from_bucket_max_tier_requires_both_conditions():
    """Needs BOTH expected_r_lower_ci > threshold AND sample_count > max_threshold."""
    config = PolicyConfig()
    # Lower-CI high enough but sample_count too low
    b1 = _populated_bucket(sample_count=80, expected_r_lower_ci=0.5)
    tier, _ = _tier_from_bucket(b1, config)
    assert tier == "normal"
    # Sample count high but lower-CI too low
    b2 = _populated_bucket(sample_count=150, expected_r_lower_ci=0.1)
    tier, _ = _tier_from_bucket(b2, config)
    assert tier == "normal"
    # Both conditions met → max
    b3 = _populated_bucket(sample_count=150, expected_r_lower_ci=0.5)
    tier, _ = _tier_from_bucket(b3, config)
    assert tier == "max"


# ---------------------------------------------------------------------------
# Step 7 — _apply_red_flag_downgrade
# ---------------------------------------------------------------------------


def test_red_flag_clean_features_no_downgrade():
    features = _valid_features()  # rvol=75, spread=5, no red flag
    config = PolicyConfig()
    assert _apply_red_flag_downgrade("max", features, config) == "max"
    assert _apply_red_flag_downgrade("normal", features, config) == "normal"
    assert _apply_red_flag_downgrade("tiny", features, config) == "tiny"


def test_red_flag_high_spread_downgrades_one_tier():
    features = _valid_features(spread_bps=100.0)  # > 50 default
    config = PolicyConfig()
    assert _apply_red_flag_downgrade("max", features, config) == "normal"
    assert _apply_red_flag_downgrade("normal", features, config) == "tiny"
    assert _apply_red_flag_downgrade("tiny", features, config) == "zero"


def test_red_flag_low_rvol_downgrades_one_tier():
    features = _valid_features(rvol_percentile=15.0)  # < 20 default
    config = PolicyConfig()
    assert _apply_red_flag_downgrade("normal", features, config) == "tiny"


def test_red_flag_composite_downgrades_one_tier():
    """has_red_flag composite fires independently of spread/RVOL."""
    features = _valid_features(has_red_flag=True)
    config = PolicyConfig()
    assert _apply_red_flag_downgrade("normal", features, config) == "tiny"


def test_red_flag_zero_stays_zero():
    """Once at zero, no further downgrade possible."""
    features = _valid_features(spread_bps=200.0, has_red_flag=True)
    assert _apply_red_flag_downgrade("zero", features, PolicyConfig()) == "zero"


# ---------------------------------------------------------------------------
# Step 8 — _clamp_stop_target
# ---------------------------------------------------------------------------


def test_clamp_choppy_regime_widens_stop():
    """In choppy regime, stop below clamp_choppy threshold gets widened."""
    advisory = _valid_decision(
        stop_loss_atr_multiple=1.0,  # below 2.0 clamp
        take_profit_atr_multiple=3.0,  # plenty of R/R headroom
    )
    stop, tp = _clamp_stop_target(advisory, "choppy", PolicyConfig())
    assert stop == 2.0  # clamped to stop_atr_clamp_choppy
    assert tp == 3.0    # unchanged; R/R = 3.0/2.0 = 1.5 = min_reward_to_risk


def test_clamp_non_choppy_regime_no_stop_widening():
    """trending_up, trending_down, crash regimes — stop passes through."""
    advisory = _valid_decision(
        stop_loss_atr_multiple=1.2,
        take_profit_atr_multiple=2.5,
    )
    for regime in ("trending_up", "trending_down", "crash", "unknown"):
        stop, _ = _clamp_stop_target(advisory, regime, PolicyConfig())
        assert stop == 1.2, f"regime={regime} unexpectedly widened stop"


def test_clamp_min_reward_to_risk_raises_tp():
    """R/R below 1.5 default → TP scaled up to meet it."""
    advisory = _valid_decision(
        stop_loss_atr_multiple=2.0,
        take_profit_atr_multiple=2.5,  # R/R = 1.25 — below 1.5
    )
    stop, tp = _clamp_stop_target(advisory, "trending_up", PolicyConfig())
    assert stop == 2.0  # unchanged
    assert tp == 3.0    # 2.0 × 1.5 = 3.0


def test_clamp_min_rr_only_raises_tp_never_tightens_stop():
    """Stop is the risk-control side; we never make it tighter to fit R/R."""
    advisory = _valid_decision(
        stop_loss_atr_multiple=3.0,    # wide stop
        take_profit_atr_multiple=1.5,  # tight TP — R/R = 0.5
    )
    stop, tp = _clamp_stop_target(advisory, "trending_up", PolicyConfig())
    assert stop == 3.0  # NEVER tightened
    assert tp == 4.5    # raised to 3.0 × 1.5


def test_clamp_passes_through_when_already_compliant():
    """R/R above min, non-choppy regime — both passthrough."""
    advisory = _valid_decision(
        stop_loss_atr_multiple=1.5,
        take_profit_atr_multiple=3.0,  # R/R = 2.0
    )
    stop, tp = _clamp_stop_target(advisory, "trending_up", PolicyConfig())
    assert stop == 1.5
    assert tp == 3.0


# ---------------------------------------------------------------------------
# decide() — integration tests for the full path
# ---------------------------------------------------------------------------


def test_decide_negative_bucket_holds_with_reason():
    """Step 6 → negative-CI bucket converts to Hold even when LLM
    advised Buy and gates 1-4 cleared."""
    inp = _valid_policy_input(
        bucket_history=_populated_bucket(
            sample_count=50, expected_r_lower_ci=-0.1
        ),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "bucket_negative_expectancy"


def test_decide_red_flag_from_tiny_holds():
    """Step 7 downgrade from tiny lands at zero → Hold with reason."""
    inp = _valid_policy_input(
        bucket_history=BucketStats.empty(
            ("trending_up", "mega", "material", "open_drive", "long")
        ),  # → tiny
        features=_valid_features(spread_bps=200.0),  # → downgrade
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Hold"
    assert result.rejection_reason == "red_flag_downgrade_to_zero"


def test_decide_red_flag_from_max_lands_at_normal():
    """Step 7 downgrade from max tier produces a normal-tier Buy."""
    inp = _valid_policy_input(
        bucket_history=_populated_bucket(
            sample_count=200, expected_r_lower_ci=0.5
        ),  # → max
        features=_valid_features(spread_bps=100.0),  # → downgrade one tier
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Buy"
    assert result.qty_tier == "normal"
    assert result.rejection_reason is None


def test_decide_choppy_regime_clamps_stop():
    """Step 8 — in choppy regime, advisory stop below clamp gets widened."""
    inp = _valid_policy_input(
        ctx=_valid_context(market_regime_label="choppy"),
        advisory=_valid_decision(
            stop_loss_atr_multiple=1.0,
            take_profit_atr_multiple=3.0,
        ),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Buy"
    assert result.stop_loss_atr_multiple == 2.0  # clamped


def test_decide_max_tier_path_end_to_end():
    """Populated bucket at max tier, clean features, trending_up regime,
    compliant R/R → max tier Buy with no clamping."""
    inp = _valid_policy_input(
        bucket_history=_populated_bucket(
            sample_count=200, expected_r_lower_ci=0.5
        ),
        advisory=_valid_decision(
            stop_loss_atr_multiple=1.5,
            take_profit_atr_multiple=3.0,
        ),
    )
    result = decide(inp, PolicyConfig())
    assert result.action == "Buy"
    assert result.qty_tier == "max"
    assert result.stop_loss_atr_multiple == 1.5
    assert result.take_profit_atr_multiple == 3.0
    assert result.rejection_reason is None
