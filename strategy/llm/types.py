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

    # ---- Meta ----
    ticker: str
    timestamp_et: str
    prompt_version: str

    # ---- Pre-market (catalyst_flags + pm_rvol added in step 5 for escalation_rule) ----
    catalyst_flags: tuple[str, ...] = ()
    pm_rvol: float = 0.0
    gap_pct: float = 0.0
    pm_high: float | None = None
    pm_low: float | None = None
    pm_volume: int = 0

    # ---- Market context (same for all tickers within a cycle) ----
    spy_change_pct: float = 0.0
    spy_rvol: float = 1.0
    vix_level: float | None = None  # None when Polygon index data unavailable
    market_regime_label: str = "unknown"  # trending_up | trending_down | choppy | crash | unknown

    # ---- Ticker fundamentals (slowly-changing) ----
    sector: str = "Unknown"
    market_cap_bucket: str = "unknown"  # mega | large | mid | small | micro | unknown
    avg_daily_volume: int = 0

    # ---- Daily regime context ----
    daily_regime: str = "neutral"  # bull | bear | neutral
    daily_adx_14: float = 0.0
    daily_atr_14: float = 0.0
    sma_200: float = 0.0
    last_5_daily_closes: tuple[float, ...] = ()  # most recent 5 closes, oldest first

    # ---- Intraday context ----
    current_close: float = 0.0
    current_volume: int = 0
    current_5min_bar_count: int = 0  # so the LLM knows how warm indicators are
    # last_10_5min_bars: tuple of dicts shaped {ts, o, h, l, c, v}
    last_10_5min_bars: tuple[dict[str, Any], ...] = ()
    rsi_14: float = 50.0
    macd_hist: float = 0.0
    macd_hist_3bar_trend: str = "flat"  # rising | falling | flat
    vwap: float = 0.0
    distance_to_vwap_pct: float = 0.0
    bollinger_position: float = 0.0  # -1 (lower band) to +1 (upper band)
    volume_ratio_vs_20bar: float = 1.0  # current bar volume vs 20-bar mean

    # ---- News & sentiment ----
    # news_items: tuple of dicts shaped {ts, headline, sentiment_score, source};
    # filtered to last 24h, capped at 5 items per the design doc.
    news_items: tuple[dict[str, Any], ...] = ()
    has_earnings_today: bool = False
    has_earnings_within_3d: bool = False

    # ---- Position state ----
    currently_holding: bool = False
    position_qty: int = 0  # 0 if flat; positive=long, negative=short
    position_avg_price: float | None = None
    position_unrealized_pl_pct: float | None = None
    has_active_stop: bool = False

    # ---- Decision history ----
    # todays_prior_decisions: tuple of dicts shaped
    # {ts, action, setup_label, confidence}; capped at last 5 evaluations
    # of this ticker today.
    todays_prior_decisions: tuple[dict[str, Any], ...] = ()

    # ---- Time-of-day context ----
    minutes_since_open: int = 0
    minutes_until_close: int = 0
    in_gap_and_go_window: bool = False  # 09:35-10:00 ET window for gap-and-go setups


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

    # ---- Forward predictions (added 2026-05-13, ChatGPT review #2) ----
    # Required by EV scoring in policy.py:
    #   EV ≈ calibrated_pwin × expected_move_pct
    #        − (1 − calibrated_pwin) × stop_distance_pct
    # Both default to 0.0 / 0 = "no opinion" so a Hold decision (which has
    # no forward move or holding-time prediction by construction) doesn't
    # need to populate them. The system prompt directs the LLM to fill
    # these for Buy / Sell decisions; a 0 expected_move_pct on a non-Hold
    # action is treated by policy.py as a missing prediction and will
    # downweight the decision in cross-sectional ranking rather than be
    # mis-read as "predicted flat."
    expected_move_pct: float = Field(ge=-20.0, le=20.0, default=0.0)
    """LLM's point estimate of price move from entry over the trade
    horizon, as a signed percent. Positive = up, negative = down. The
    sign should agree with action (Buy → positive, Sell → negative);
    a sign disagreement gets logged by policy.py as a sanity flag."""

    expected_holding_minutes: int = Field(ge=0, le=390, default=0)
    """LLM's estimate of how long the trade should hold before
    re-evaluation, in minutes. 0 = no opinion (defaults to time_horizon
    semantics). 390 = full RTH session. Used by policy.py to set
    re-evaluation cadence per position and to flag trades whose horizon
    exceeds their stated time_horizon (e.g., 30-min for an 'overnight'
    label is inconsistent)."""

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

    # ---- Permissive normalization (mode="before") ----
    # Anthropic's tool-use enforces required fields and enum/type
    # constraints but NOT numeric bounds or string maxLength. So we
    # normalize the LLM's output before the Pydantic constraints fire:
    # clamp numerics to bounds, truncate strings to max length. The
    # constraints in Field(...) above stay so the JSON schema sent to
    # the LLM still signals intended bounds, and so genuinely
    # malformed input (wrong type, wrong enum) still raises
    # ValidationError → SchemaInvalidError. Out-of-range values
    # become valid in-range values; out-of-shape values still fail.

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v: Any) -> Any:
        if isinstance(v, bool):  # bool is subclass of int; reject
            return v
        if isinstance(v, (int, float)):
            return max(0, min(100, int(v)))
        return v

    @field_validator("stop_loss_atr_multiple", mode="before")
    @classmethod
    def _clamp_stop_loss(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return max(1.0, min(3.0, float(v)))
        return v

    @field_validator("take_profit_atr_multiple", mode="before")
    @classmethod
    def _clamp_take_profit(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return max(1.0, min(5.0, float(v)))
        return v

    @field_validator("expected_move_pct", mode="before")
    @classmethod
    def _clamp_expected_move(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return max(-20.0, min(20.0, float(v)))
        return v

    @field_validator("expected_holding_minutes", mode="before")
    @classmethod
    def _clamp_expected_holding(cls, v: Any) -> Any:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return max(0, min(390, int(v)))
        return v

    @field_validator("setup_label", mode="before")
    @classmethod
    def _truncate_setup_label(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 50:
            return v[:47] + "..."
        return v

    @field_validator("reasoning", mode="before")
    @classmethod
    def _truncate_reasoning(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 280:
            return v[:277] + "..."
        return v

    @field_validator("alternative_view", mode="before")
    @classmethod
    def _truncate_alt_view(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) > 140:
            return v[:137] + "..."
        return v
