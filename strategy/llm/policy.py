"""TradePolicy — the deterministic decision layer between the LLM and execution.

Pure-deterministic, fully unit-testable. The LLM emits classifications
(``LLMAnalysis``) and an advisory action (``LLMDecision``); this module
consumes them along with market features, account state, and historical
bucket performance, and produces a ``FinalTradeDecision`` that the risk
validator + order client execute.

Why a separate layer at all? Three reasons, in priority order:

1. **Containment.** An LLM that hallucinates a Buy on a clamp_anomaly,
   a low-liquidity name, or a bucket with negative expectancy must NOT
   move money. The policy is the deterministic firewall — it's where
   "fail-closed" is enforceable in code, not vibes.

2. **Reproducibility.** Given identical inputs, this module produces
   identical outputs across runs and across machine restarts. The M2
   replay harness depends on that property. The LLM tier's outputs
   are also temperature-pinned, but a deterministic policy gives us
   double-coverage on reproducibility.

3. **Tunability.** Parameters that affect live behavior (thresholds,
   tier cutoffs, clamps) live in ``PolicyConfig`` with documented
   bounds. ``scripts/tune_policy.py`` (Q3 resolution) does
   Optuna-driven Calmar-targeted walk-forward tuning against these
   parameters without touching prompt or model code.

## Design contract

Pinned to ``docs/LLM_MODEL_V2_REFINEMENTS.md § TradePolicy module spec``.
Any change to ``PolicyInput`` / ``MarketFeatures`` / ``BucketStats`` /
``FinalTradeDecision`` shapes or to ``PolicyConfig`` field names requires
bumping ``policy_version`` per Q4 SemVer semantics.

## Scope (as of 2026-05-13 — first land)

This first chunk lands the foundation:

- All four dataclasses (PolicyInput, MarketFeatures, BucketStats,
  FinalTradeDecision) with their pinned fields.
- ``PolicyConfig`` with the 10 tunable + 5 untunable parameters from
  Q3 (defaults match the table; bounds documented inline for the
  eventual tuner).
- ``decide(input, config) -> FinalTradeDecision`` public function.
- Decision-tree steps 1-4 (the four early-Hold gates: health, llm_avoid,
  clamp_anomaly, llm_hold) and step 9 (final-decision construction).
- Position-management mapping for the 6 ``PositionAction`` cases when
  ``ctx.currently_holding``.

Deferred to follow-up sessions:

- Steps 5-6: hierarchical bucket lookup + sample-count tier sizing.
- Step 7: red-flag downgrade (spread / RVOL).
- Step 8: stop/target clamping in choppy regime + min reward/risk.
- ChatGPT review #2 additions: liquidity gate, EV scoring,
  cross-sectional ranking. (Calibrator is upstream — blocked on
  shadow data.)

Until those land, ``decide()`` defaults the tier to ``"normal"`` and
takes the advisory's stop/TP multiples as-is when it approves a trade.
Buckets with ``sample_count == 0`` get a ``"tiny"`` tier. This is the
"safe stub" behavior — never more aggressive than the eventual logic,
sometimes less.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from strategy.llm.analysis import LLMAnalysis, PositionAction, TradeReadiness
from strategy.llm.types import LLMContext, LLMDecision


# ============================================================================
# Placeholder types
# ============================================================================

# Until ``A.3 Health state machine`` lands as its own module
# (``strategy/llm/health.py``), the policy accepts a string-typed health
# state. The values mirror the planned ``HealthState`` enum so the
# eventual swap-in is a type-only change.
HealthState = Literal[
    "healthy",        # Tier 1 serving normally
    "tier1_down",     # T1 unavailable; gap-and-go fallback is active
    "tier2_down",     # T2 escalation broken; T1-only mode
    "degraded",       # Latency/error rate elevated but serving
    "halt",           # Operator pulled the brake or sustained anomaly fired
]
"""Operational health summary the policy reads to decide whether to
approve any LLM action at all. Anything but ``"healthy"`` is an
unconditional Hold gate; A.3's full HealthState dataclass will preserve
this semantics."""


QtyTier = Literal["zero", "tiny", "normal", "max"]
"""Size envelope applied at risk-validation time.

  - ``zero``  : action becomes Hold regardless of advisory
  - ``tiny``  : 0.25 × normal (paper-trading exploration / new bucket)
  - ``normal``: configured ``risk_per_trade_pct`` (1.0% default)
  - ``max``   : 1.5 × normal, only after 100+ samples in a bucket whose
                lower-95-CI expected_r exceeds the max-tier threshold.

The risk validator (``strategy/risk.py``) reads the tier and the
account equity to translate to a share count. Policy only assigns
the tier; it never computes shares directly.
"""


# ============================================================================
# Inputs
# ============================================================================


@dataclass(frozen=True, slots=True)
class MarketFeatures:
    """Live market-microstructure features that gate the policy.

    Built per-candidate by the signal-engine caller from ``LLMContext``
    + the latest bar + the order-book snapshot. NOT computed by the
    LLM itself — the LLM sees coarser fields like ``volume_ratio_vs_20bar``
    on ``LLMContext``, but the policy needs spread + RVOL percentile +
    distance metrics that the LLM doesn't get to interpret.

    ``has_red_flag`` is the composite signal the policy reads in step 7
    (red-flag downgrade — DEFERRED). It's intentionally a single bool so
    the policy's red-flag logic stays trivial; the composition is the
    builder's job, not the policy's. The conditions per design doc:

        has_red_flag = (
            features.spread_bps > spread_bps_red_flag
            OR features.rvol_percentile < rvol_percentile_red_flag
            OR features.volume_ratio_vs_20bar < 0.7
        )

    Holding-specific fields (``distance_to_stop_atr``,
    ``distance_to_target_atr``) are ``None`` when not holding the name.
    """

    rvol_percentile: float
    """Where the current bar's volume falls in the trailing 20-bar
    distribution, as a percentile [0, 100]. Low values indicate
    thin tape; this is the breadth signal for entry quality."""

    spread_bps: float
    """Current top-of-book spread in basis points (10 × percent of mid).
    Conservative liquidity proxy. The policy's review-#2 liquidity
    gate (deferred) will reject names with spread above a tunable
    threshold; the red-flag composition above uses a softer cutoff."""

    distance_to_vwap_atr: float
    """Signed distance from current price to session VWAP in daily-ATR
    units. Positive = price above VWAP, negative = below."""

    distance_to_stop_atr: float | None
    """How far the current price sits from the active stop, in
    daily-ATR units. ``None`` when not holding. Negative would mean
    the stop has already been violated and a flush is in motion;
    the policy treats those as `TIGHTEN_STOP`-only territory."""

    distance_to_target_atr: float | None
    """Mirror of the above for the take-profit target. ``None`` when
    not holding or when no TP leg is attached to the bracket."""

    has_red_flag: bool
    """Composite liquidity / participation warning. See class docstring
    for the composition. Consumed by step 7 (deferred)."""


@dataclass(frozen=True, slots=True)
class BucketStats:
    """Historical realized P&L for the matching bucket.

    The nightly aggregation job (``scripts/analyze_shadow_outcomes.py``
    eventually paired with ``position_trace`` event aggregation per Q2)
    produces one of these per bucket. The signal-engine caller picks
    the right one via hierarchical lookup before invoking the policy.

    ``bucket_key`` is the 5-tuple of dimensions Q1 resolved:
    ``(regime, cap_size, catalyst_quality, time_of_day_bucket, long_short)``.
    Hierarchical fallback (deferred to next session) replaces the most
    granular bucket with progressively-collapsed versions when sample
    counts are low. The bucket actually used is recorded in
    ``FinalTradeDecision.bucket_key`` so post-hoc analysis can see
    when the fallback fired.

    ``expected_r_lower_ci`` is the lower bound of a 95% confidence
    interval over realized R (return / risk). Using the CI lower bound
    rather than the mean is what makes new buckets default to ``tiny``
    rather than ``normal`` — the interval is wide when sample_count
    is small, so the lower bound is conservative.
    """

    bucket_key: tuple
    """5-tuple: (regime, cap_size, catalyst_quality, time_of_day_bucket,
    long_short). Stored as a tuple for hashability + cheap comparison."""

    sample_count: int
    """Number of historical decisions in this bucket. Drives tier
    selection: 0 → tiny, < normal_threshold → tiny, >= normal_threshold
    AND lower_ci > max_threshold → max, else → normal."""

    expected_r: float
    """Mean realized R across the bucket's history. Informational;
    the tier logic gates on lower_ci, not the point estimate."""

    expected_r_lower_ci: float
    """Lower bound of 95% CI on expected_r. Negative → reject (the
    bucket loses money over realistic uncertainty). Positive →
    candidate for normal tier."""

    win_rate: float
    """Fraction of decisions in the bucket that closed positive R.
    Useful for the calibrator (deferred review-#2 item) but the
    policy itself doesn't gate on it directly."""

    avg_win_r: float
    """Mean realized R conditional on winning. With avg_loss_r this
    gives the reward/risk distribution character; the calibrator
    will use both."""

    avg_loss_r: float
    """Mean realized R conditional on losing (typically near -1.0
    for stop-hit cases; further negative for slippage / gap-down)."""

    last_updated: datetime
    """When this aggregation was last refreshed. Stale stats are an
    operational concern: the policy doesn't gate on age but the
    EOD report flags buckets where ``last_updated`` is > 1 day old."""

    @classmethod
    def empty(cls, bucket_key: tuple) -> BucketStats:
        """Return a BucketStats with sample_count=0 for an unknown bucket.

        Used when hierarchical fallback exhausts all collapse paths and
        no historical data exists for any matching bucket. The policy
        then defaults to the ``tiny`` tier — paper-trading exploration
        until enough samples accumulate to graduate."""
        return cls(
            bucket_key=bucket_key,
            sample_count=0,
            expected_r=0.0,
            expected_r_lower_ci=0.0,
            win_rate=0.0,
            avg_win_r=0.0,
            avg_loss_r=0.0,
            last_updated=datetime.fromtimestamp(0),  # epoch = "never"
        )


@dataclass(frozen=True, slots=True)
class AccountState:
    """Snapshot of account-side state the policy needs.

    Decoupled from ``LLMContext`` because ``LLMContext`` only carries
    per-ticker position info; the policy also needs total exposure
    and equity to enforce the untunable ``total_exposure_pct`` constraint.
    Built by the signal-engine caller before each policy invocation.
    """

    equity: float
    """Total account equity in dollars."""

    total_exposure_pct: float
    """Sum of open-position notionals as a percent of equity. Hard
    cap of 90% is enforced by ``strategy/risk.py::validate_order``;
    the policy doesn't re-check, but it can see the value for
    optional exposure-aware sizing in future revisions."""

    open_position_count: int
    """Count of distinct symbols currently held. Used by the
    cross-sectional ranking (deferred review-#2 item) for portfolio
    heat accounting; not gated on directly today."""


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """The full input bundle. One of these per (ticker, timestamp).

    Constructed by the signal-engine caller after the LLM has emitted
    its ``LLMOutput`` and the market features + bucket stats + account
    state have been gathered for that candidate.
    """

    ctx: LLMContext
    analysis: LLMAnalysis
    advisory: LLMDecision
    features: MarketFeatures
    account: AccountState
    bucket_history: BucketStats
    health_state: HealthState = "healthy"
    """Operational gate — see HealthState above. Default is healthy
    so unit tests don't have to provide it; production callers
    populate from the live health-state machine (A.3, not yet built)."""


# ============================================================================
# Output
# ============================================================================


@dataclass(frozen=True, slots=True)
class FinalTradeDecision:
    """The policy's verdict. Pure data — no side effects.

    Passed to ``strategy/risk.py::validate_order`` (which resolves
    ``qty_tier`` to a share count given account equity) and from there
    to the order client. Logged with all fields populated so post-hoc
    analysis can attribute every decision to a specific policy path.
    """

    action: Literal["Buy", "Sell", "Hold"]
    """Final action. May differ from ``advisory.action`` when the
    policy overrides — e.g., LLM said Buy but bucket lower-CI is
    negative, so policy says Hold with rejection_reason set."""

    qty_tier: QtyTier
    """Size envelope. Hold actions always get ``"zero"``; Buy/Sell
    get tiny / normal / max per the bucket / red-flag logic."""

    stop_loss_atr_multiple: float
    """Final stop multiple. Either passes through from advisory or
    is clamped (e.g., choppy regime, deferred). Always in
    [stop_atr_min, stop_atr_max] per Pydantic bounds on LLMDecision."""

    take_profit_atr_multiple: float
    """Final TP multiple. Same pass-through-or-clamp semantics as
    stop_loss_atr_multiple."""

    rejection_reason: str | None
    """Populated when the policy overrides an LLM Buy/Sell to Hold.
    Values are stable strings the EOD report aggregates over:
        "health_not_ok" | "llm_avoid" | "clamp_anomaly"
        | "bucket_negative_expectancy" | "red_flag_downgrade_to_zero"
        | ... (more land with steps 5-8)
    ``None`` when the policy approved the LLM's action."""

    bucket_key: tuple
    """The bucket the policy actually used (post-fallback). Recorded
    even on Hold so the audit trail captures what data informed the
    decision."""

    policy_version: str
    """SemVer per Q4. Bumped on any change to the decision logic or
    the tunable thresholds. Independent from prompt_version: a prompt
    tweak doesn't bump policy_version, and vice versa."""

    ev_score: float | None = None
    """Uncalibrated expected-value score, logged for shadow analytics.
    Computed only on Buy/Sell decisions (None on Hold):

        ev_score = (advisory.confidence / 100) * advisory.expected_move_pct

    The pwin proxy is ``advisory.confidence / 100`` until the calibrator
    (``analysis/calibration.py``, blocked on populated shadow_outcomes)
    lands isotonic regression on T1 confidence → empirical win rate.
    Then this becomes ``calibrated_pwin × expected_move_pct - risk_cost``
    and gating on negative EV becomes safe. Until then it's a logged
    quantity only — no policy gate reads it."""

    liquidity_rejected: bool = False
    """Whether the liquidity hard-reject gate (review #2 addition)
    fired. Set when spread or RVOL crosses the absolute thresholds in
    PolicyConfig. Logged for post-hoc analysis of how often the
    liquidity gate catches trades the red-flag downgrade misses."""


# ============================================================================
# Tunable + untunable configuration
# ============================================================================


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Tunable policy parameters + untunable risk constraints.

    The 10 tunable params land in this dataclass with the Q3 defaults
    and the comment shows their tuning bounds. The 5 untunable
    constraints sit alongside so the policy doesn't have to load
    them from ``settings.yaml`` separately, but they should NEVER
    appear in ``config/policy_candidate.yaml`` (Optuna doesn't touch
    them; the tuner script must filter).

    Bumping ``policy_version`` when constructing this dataclass is
    the operator's responsibility — typically via a PR that updates
    both ``config/policy.yaml`` AND the policy_version string. CI
    can later enforce that pair (deferred; see
    ``scripts/check_version_bumps.py`` in Phase 3).
    """

    # ---- Tunable: bucket-size thresholds (Q3) ----
    sample_min_for_normal_tier: int = 30
    """Below this sample count, qty_tier = tiny. Bounds [20, 100]."""

    sample_min_for_max_tier: int = 100
    """Above this sample count AND positive lower-CI, max tier
    eligible. Bounds [50, 200]."""

    expected_r_for_max_tier: float = 0.30
    """Lower-CI expected_r threshold for max tier eligibility.
    Bounds [0.20, 1.00]."""

    # ---- Tunable: liquidity hard-reject thresholds (review #2 addition) ----
    # Distinct from the red-flag thresholds: red flag downgrades a tier,
    # this rejects the trade outright. Defaults are deliberately looser
    # than the red-flag values so the red-flag downgrade fires FIRST on
    # marginal liquidity and this gate only catches the genuinely-untradeable.
    liquidity_max_spread_bps: float = 100.0
    """Spread above this rejects the trade to Hold (liquidity_reject).
    Bounds [50, 300]. Wider than spread_bps_red_flag so red-flag downgrade
    catches marginal cases first; this gate only fires when slippage
    assumptions fundamentally break down."""

    liquidity_min_rvol_percentile: float = 10.0
    """RVOL percentile below this rejects the trade. Bounds [5, 25].
    Below 10 = bottom decile of recent activity for the symbol; trades
    in this thinness regime have unreliable fills regardless of
    bucket history."""

    # ---- Tunable: red-flag thresholds (Q3) ----
    spread_bps_red_flag: float = 50.0
    """Spread above this triggers a tier downgrade in step 7. Bounds [25, 200]."""

    rvol_percentile_red_flag: float = 20.0
    """RVOL percentile below this triggers a tier downgrade in step 7.
    Bounds [10, 40]."""

    # ---- Tunable: stop/target clamps (Q3) ----
    stop_atr_clamp_choppy: float = 2.0
    """In choppy regime, clamp stop ATR multiple to at least this.
    Wider stops survive chop better. Bounds [1.5, 3.0]."""

    min_reward_to_risk: float = 1.5
    """Take-profit ATR multiple / stop ATR multiple must be >= this.
    Bounds [1.2, 2.5]."""

    # ---- Tunable: T2 escalation gates (Q3) ----
    # These don't appear in step 1-9 of the decision tree but are part
    # of the same tunable set — the escalation rule reads them.
    escalation_confidence_low: int = 50
    """Lower bound of the T1-confidence band that triggers T2.
    Bounds [40, 60]."""

    escalation_confidence_high: int = 75
    """Upper bound of the T1-confidence band that triggers T2.
    Bounds [65, 85]."""

    # ---- Tunable: position management (Q3) ----
    trim_pct_default: float = 0.5
    """Fraction sold on PositionAction.TRIM. Bounds [0.33, 0.67]."""

    # ---- Untunable risk constraints (Q3 — these are HARD STOPS) ----
    risk_per_trade_pct: float = 1.0
    """Account-protection rule. NEVER auto-tuned. Override only
    via deliberate human PR + smoke verification."""

    max_drawdown_pct: float = 15.0
    """Hard stop. Hit, the system halts."""

    single_day_loss_pct: float = 5.0
    """Hard stop. Hit, the system halts for the rest of the day."""

    total_exposure_pct: float = 90.0
    """Hard constraint enforced by ``strategy/risk.py``. Policy
    doesn't re-check; sits here as the documented value."""

    max_holding_days: int = 3
    """Q2 hard cap on multi-day holds. MAX_DURATION_FLATTEN fires
    at day-3 close regardless of LLM opinion."""

    # ---- Version pin ----
    policy_version: str = "0.1.0"
    """SemVer per Q4. 0.x while the module is under construction;
    bumps to 1.0.0 when all 9 decision-tree steps + review-#2
    additions are landed and validated against M2 replay."""


# ============================================================================
# Internal helpers
# ============================================================================


# Stable rejection-reason strings. Aggregated by the EOD report; if you
# rename one, the historical query has to be updated to UNION the old
# and new values. Don't rename — add new ones.
_REASON_HEALTH_NOT_OK = "health_not_ok"
_REASON_LLM_AVOID = "llm_avoid"
_REASON_CLAMP_ANOMALY = "clamp_anomaly"
_REASON_BUCKET_NEGATIVE = "bucket_negative_expectancy"
_REASON_RED_FLAG_DOWNGRADE = "red_flag_downgrade_to_zero"
_REASON_LIQUIDITY_REJECT = "liquidity_reject"


# Tier downgrade ladder. Used by step 7 (red-flag downgrade) to step
# down one rung. Hitting "zero" converts to a Hold at the caller.
_TIER_DOWNGRADE: dict[QtyTier, QtyTier] = {
    "max": "normal",
    "normal": "tiny",
    "tiny": "zero",
    "zero": "zero",
}


def _tier_from_bucket(
    bucket: BucketStats, config: PolicyConfig
) -> tuple[QtyTier, str | None]:
    """Step 6 — sample-count tier sizing.

    Returns (tier, rejection_reason). When rejection_reason is non-None
    the caller must convert to a Hold; the negative-expectancy case
    is the only one in this function that does so.

    Logic (per design doc step 6):
      - sample_count == 0 OR < sample_min_for_normal_tier → tiny
      - expected_r_lower_ci <= 0 (with samples) → reject as negative
      - lower_ci > expected_r_for_max_tier AND samples > max_threshold → max
      - otherwise → normal

    The empty-bucket case routes to ``tiny`` rather than rejecting,
    because new buckets need to accumulate paper-trading samples
    before the policy can decide whether they're worth deploying.
    Rejecting on zero-samples would mean no bucket ever graduated
    from cold-start, which is the failure mode the deployment-gate
    framework is designed to avoid.
    """
    if bucket.sample_count == 0:
        return "tiny", None
    if bucket.sample_count < config.sample_min_for_normal_tier:
        return "tiny", None
    # Enough samples to interpret the CI. Negative lower-CI means even
    # under realistic uncertainty the bucket loses money; reject.
    if bucket.expected_r_lower_ci <= 0.0:
        return "zero", _REASON_BUCKET_NEGATIVE
    # Eligible for max tier?
    if (
        bucket.expected_r_lower_ci > config.expected_r_for_max_tier
        and bucket.sample_count > config.sample_min_for_max_tier
    ):
        return "max", None
    return "normal", None


def _apply_red_flag_downgrade(
    tier: QtyTier,
    features: MarketFeatures,
    config: PolicyConfig,
) -> QtyTier:
    """Step 7 — downgrade one tier if a microstructure red flag fires.

    Fires when:
      - spread_bps > spread_bps_red_flag, OR
      - rvol_percentile < rvol_percentile_red_flag, OR
      - features.has_red_flag is True (composite; built by caller)

    A downgrade from tiny lands at zero, which the caller converts to
    a Hold with rejection_reason. The composite ``has_red_flag`` is
    independent of the spread / RVOL checks here because the builder
    that produces it may use additional inputs (e.g., volume_ratio)
    that the policy doesn't see directly.
    """
    if (
        features.spread_bps > config.spread_bps_red_flag
        or features.rvol_percentile < config.rvol_percentile_red_flag
        or features.has_red_flag
    ):
        return _TIER_DOWNGRADE[tier]
    return tier


def _clamp_stop_target(
    advisory: LLMDecision,
    regime: str,
    config: PolicyConfig,
) -> tuple[float, float]:
    """Step 8 — clamp the advisory stop/TP multiples for regime + R/R.

    Two clamps:

      1. In a ``choppy`` regime, force stop to at least
         ``stop_atr_clamp_choppy`` (default 2.0). Tight stops in chop
         get noised out; widening pre-emptively reduces stop-hit churn
         on price action that doesn't actually invalidate the thesis.

      2. Enforce ``min_reward_to_risk`` (default 1.5). If the advisory
         emitted a TP-to-stop ratio below this, scale the TP UP to
         meet it — never scale the stop DOWN, because stops are the
         risk-control side of the bracket. The capped TP may slightly
         exceed the advisory's intent; that's a known artifact and
         less harmful than letting a sub-threshold R/R trade fire.

    Returns (stop_atr_multiple, take_profit_atr_multiple) after both
    clamps. The advisory's Pydantic bounds (stop in [1.0, 3.0], TP in
    [1.0, 5.0]) still apply at the LLMDecision boundary; this function
    operates within those.
    """
    stop = advisory.stop_loss_atr_multiple
    tp = advisory.take_profit_atr_multiple

    if regime == "choppy" and stop < config.stop_atr_clamp_choppy:
        stop = config.stop_atr_clamp_choppy

    # Enforce min reward/risk by raising TP if needed. Stop never moves
    # in this direction — clamping stops looser is one thing (above)
    # but tighter would violate the risk-control invariant.
    if stop > 0 and tp / stop < config.min_reward_to_risk:
        tp = stop * config.min_reward_to_risk

    return stop, tp


def bucket_key_for(
    *,
    market_regime_label: str,
    market_cap_bucket: str,
    catalyst_quality_value: str,
    minutes_since_open: int,
    action: str,
) -> tuple:
    """Derive the 5-tuple bucket key from its raw components.

    Public version of ``_bucket_key_from_input`` so callers that need
    the key BEFORE constructing a full PolicyInput (e.g., to drive
    hierarchical_lookup which produces the BucketStats that GOES into
    the PolicyInput) can derive it without instantiating placeholder
    objects. The signature is keyword-only to keep the call sites
    self-documenting at the source.

    Returns the most-granular key (5-tuple). Hierarchical_lookup is
    what walks the collapse ladder from here.
    """
    return (
        market_regime_label,
        market_cap_bucket,
        catalyst_quality_value,
        _time_of_day_bucket(minutes_since_open),
        "long" if action == "Buy" else "short",
    )


def _bucket_key_from_input(input: PolicyInput) -> tuple:
    """Derive the 5-tuple bucket key from a PolicyInput.

    Pre-fallback — this is the MOST granular key. Thin wrapper over
    ``bucket_key_for`` for callers that already have a PolicyInput.

    Long/short is encoded as a string so the tuple stays hashable and
    debuggable in log output.
    """
    return bucket_key_for(
        market_regime_label=input.ctx.market_regime_label,
        market_cap_bucket=input.ctx.market_cap_bucket,
        catalyst_quality_value=input.analysis.catalyst_quality.value,
        minutes_since_open=input.ctx.minutes_since_open,
        action=input.advisory.action,
    )


def _time_of_day_bucket(minutes_since_open: int) -> str:
    """Collapse minutes-since-open into the 5-bucket time_of_day dim.

    Per Q1 resolution. The default-collapse order Q1 chose collapses
    this to 3 buckets at fallback time; here we always emit the
    5-bucket native version, and the hierarchical lookup is what does
    the 5→3 collapse.

        0–30   : open_drive (first 30 min volatility)
        31–90  : morning   (post-open through mid-morning)
        91–270 : midday    (~11:00 ET through ~14:30 ET)
        271–360: power_hour (~14:30 ET through ~15:30 ET)
        361+   : close     (last 30 min)
    """
    if minutes_since_open < 31:
        return "open_drive"
    if minutes_since_open < 91:
        return "morning"
    if minutes_since_open < 271:
        return "midday"
    if minutes_since_open < 361:
        return "power_hour"
    return "close"


# ----------------------------------------------------------------------
# Hierarchical bucket lookup (Q1 resolution)
# ----------------------------------------------------------------------
#
# The bucket key is the 5-tuple (regime, cap_size, catalyst_quality,
# time_of_day, long_short). At default granularity that's
# 5 × 5 × 5 × 5 × 2 = 1,250 candidate buckets across the entire input
# space, of which a typical strategy might populate maybe 100-200 with
# any real sample volume.
#
# Q1 resolves the cold-start problem by collapsing the least-informative
# dimensions when a bucket has too few samples. Default collapse order:
# time_of_day (5 → 3) first, then cap_size (5 → 2). Max-granular bucket
# count drops from 1,250 to 144 after both collapses. The
# `bucket_key_used` recorded in FinalTradeDecision makes the inheritance
# chain auditable.

_TIME_OF_DAY_COLLAPSE_5_TO_3 = {
    "open_drive": "morning",
    "morning": "morning",
    "midday": "midday",
    "power_hour": "afternoon",
    "close": "afternoon",
}
"""5 → 3 collapse for time_of_day. open_drive folds into morning;
power_hour folds into afternoon (with close). midday stays standalone
as the lowest-noise time window in a typical session."""


_CAP_SIZE_COLLAPSE_5_TO_2 = {
    "mega": "large_cap",
    "large": "large_cap",
    "mid": "small_cap",
    "small": "small_cap",
    "micro": "small_cap",
    "unknown": "unknown",
}
"""5 → 2 collapse for market cap. mega + large vs mid + small + micro.
unknown stays unknown so it never silently joins either bucket."""


def _collapse_time_of_day(key: tuple) -> tuple:
    """Return the key with time_of_day collapsed via the 5→3 mapping.

    Index 3 of the 5-tuple is time_of_day per
    ``_bucket_key_from_input``. If the input label isn't in the
    5→3 map (e.g., the LLMContext default sentinel ever leaks
    through), it passes through unchanged — better to keep the
    label visible than silently rewrite it."""
    new_time = _TIME_OF_DAY_COLLAPSE_5_TO_3.get(key[3], key[3])
    return (key[0], key[1], key[2], new_time, key[4])


def _collapse_cap_size(key: tuple) -> tuple:
    """Return the key with cap_size collapsed via the 5→2 mapping.

    Index 1 of the 5-tuple is cap_size. Same pass-through behavior
    for unmapped labels."""
    new_cap = _CAP_SIZE_COLLAPSE_5_TO_2.get(key[1], key[1])
    return (key[0], new_cap, key[2], key[3], key[4])


def hierarchical_lookup(
    base_key: tuple,
    get_bucket: Callable[[tuple], BucketStats | None],
    sample_min: int,
) -> BucketStats:
    """Walk through progressively-collapsed bucket keys until a bucket
    with enough samples is found.

    Walk order (Q1 default):
      1. Try the base key as-is.
      2. If sample_count < sample_min, collapse time_of_day and retry.
      3. If still < sample_min, also collapse cap_size and retry.
      4. If still < sample_min, return whatever bucket was found at
         the most-collapsed key — even with insufficient samples — so
         the caller can still see the (probably-noisy) stats and tier
         the trade as ``tiny`` on the sample-count gate.

    If at any level ``get_bucket`` returns ``None`` (the bucket has
    no rows at all in the aggregation table), we treat it as
    ``BucketStats.empty(key)`` so the walk continues. After all
    collapses, an all-empty result is what bubbles up; the caller
    sees ``sample_count == 0`` and routes to the ``tiny`` tier.

    Returns the BucketStats actually selected (after walk). The
    returned ``bucket_key`` reflects the level at which it was
    selected, so post-hoc audits can see whether the fallback fired.
    """
    # 1. Most granular
    bucket = get_bucket(base_key) or BucketStats.empty(base_key)
    if bucket.sample_count >= sample_min:
        return bucket

    # 2. Collapse time_of_day
    time_collapsed_key = _collapse_time_of_day(base_key)
    if time_collapsed_key != base_key:  # collapse actually changed something
        b2 = get_bucket(time_collapsed_key) or BucketStats.empty(time_collapsed_key)
        if b2.sample_count >= sample_min:
            return b2
        # remember the best-so-far in case all collapses fail
        bucket = b2

    # 3. Also collapse cap_size
    both_collapsed_key = _collapse_cap_size(time_collapsed_key)
    if both_collapsed_key != time_collapsed_key:
        b3 = get_bucket(both_collapsed_key) or BucketStats.empty(both_collapsed_key)
        if b3.sample_count >= sample_min:
            return b3
        bucket = b3

    # 4. Out of collapses. Return whatever we ended at; caller's tier
    # logic will route to ``tiny`` on sample_count < sample_min.
    return bucket


def _make_hold(
    input: PolicyInput,
    config: PolicyConfig,
    reason: str,
    *,
    liquidity_rejected: bool = False,
) -> FinalTradeDecision:
    """Construct a Hold FinalTradeDecision with the standard fields.

    Used by every early-Hold gate so the shape is consistent. The
    stop/TP multiples are taken from the advisory even though Hold
    doesn't use them — keeps the audit row complete and prevents
    schema-mismatch surprises in the decisions log.

    ``liquidity_rejected`` is True only when the step 7.5 liquidity
    hard-reject gate fires; other Hold paths leave it False. Recorded
    so post-hoc analysis can count how often the liquidity gate
    triggered without grepping rejection_reason strings.
    """
    return FinalTradeDecision(
        action="Hold",
        qty_tier="zero",
        stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
        take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
        rejection_reason=reason,
        bucket_key=_bucket_key_from_input(input),
        policy_version=config.policy_version,
        ev_score=None,
        liquidity_rejected=liquidity_rejected,
    )


def _translate_position_action(
    input: PolicyInput,
    config: PolicyConfig,
) -> FinalTradeDecision:
    """Map analysis.position_action to a FinalTradeDecision when holding.

    Per the design doc § "Position-management mapping":

        HOLD          → no order (action=Hold, no rejection_reason)
        TRIM          → market sell qty × trim_pct (action=Sell, tier=normal)
        EXIT          → market close position (action=Sell, tier=normal)
        TIGHTEN_STOP  → replace stop (action=Hold; stop adjustment is
                        a separate execution path the orchestrator handles)
        SCALE_UP      → bracket order additional qty (action=Buy, tier=normal)
        NO_OPINION    → no order (action=Hold)

    Important nuance: SCALE_UP returns Buy with tier=normal but the
    risk validator still applies position/exposure caps. If the cap
    blocks the scale-up, validate_order returns approved=False and
    the orchestrator simply does not submit. The policy doesn't
    re-check exposure here.

    TIGHTEN_STOP is intentionally action=Hold even though it produces
    a side effect — the side effect is a stop-replacement order, not
    a trade. The orchestrator's holding-management path reads the
    PositionAction directly off ``input.analysis.position_action`` to
    know whether to issue the stop-replacement; the FinalTradeDecision
    surfaces nothing because there's no new bracket.
    """
    pa = input.analysis.position_action

    if pa == PositionAction.HOLD or pa == PositionAction.NO_OPINION:
        return FinalTradeDecision(
            action="Hold",
            qty_tier="zero",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    if pa == PositionAction.TIGHTEN_STOP:
        # Action=Hold; the actual stop replacement is the orchestrator's
        # job. See class docstring above.
        return FinalTradeDecision(
            action="Hold",
            qty_tier="zero",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    if pa == PositionAction.TRIM or pa == PositionAction.TAKE_PARTIAL:
        # Partial exit. The orchestrator multiplies position_qty by
        # config.trim_pct_default to size the sell order; the policy
        # doesn't compute the share count.
        return FinalTradeDecision(
            action="Sell",
            qty_tier="normal",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    if pa == PositionAction.EXIT:
        # Full close. Same shape as TRIM; orchestrator sizes 100%.
        return FinalTradeDecision(
            action="Sell",
            qty_tier="normal",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    if pa == PositionAction.SCALE_UP:
        # Add to working position. Direction inferred from existing
        # position_qty sign on the LLMContext.
        side: Literal["Buy", "Sell"] = (
            "Buy" if input.ctx.position_qty > 0 else "Sell"
        )
        return FinalTradeDecision(
            action=side,
            qty_tier="normal",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    # Defensive default — an unknown PositionAction should never reach
    # here (Pydantic enum constraints prevent it), but if a future
    # enum member is added without updating this mapping, we fail
    # safe to Hold rather than execute on stale logic.
    return _make_hold(input, config, reason=f"unknown_position_action:{pa.value}")


# ============================================================================
# Public entry point
# ============================================================================


def decide(input: PolicyInput, config: PolicyConfig) -> FinalTradeDecision:
    """Apply the policy. Pure function — no I/O, no async, no global state.

    Decision tree:

        1. Health not "healthy"        → Hold (health_not_ok)
        2. Analysis = TradeReadiness.AVOID → Hold (llm_avoid)
        3. clamp_anomaly == True       → Hold (clamp_anomaly)
        4. advisory.action == "Hold"   → Hold (no rejection_reason —
                                                 policy agreed with LLM)
        4b. ctx.currently_holding && advisory.action != Hold:
                                       → translate analysis.position_action
                                          (see _translate_position_action)
        5. Hierarchical bucket lookup  → DEFERRED next session
        6. Tier sizing                 → DEFERRED next session
        7. Red-flag downgrade          → DEFERRED next session
        8. Stop/target clamps          → DEFERRED next session
        9. Construct FinalTradeDecision with bucket_key + policy_version

    Until steps 5-8 land, an LLM Buy/Sell that clears the four
    early-Hold gates gets approved at tier=normal with the advisory's
    stop/TP multiples taken as-is. Buckets with sample_count == 0 are
    downgraded to tier=tiny — see step-9 logic — so a brand-new
    bucket can't trade at full size by accident. This is intentionally
    safer than the eventual full logic.
    """
    # Step 1 — Health gate.
    if input.health_state != "healthy":
        return _make_hold(input, config, reason=_REASON_HEALTH_NOT_OK)

    # Step 2 — LLM hard veto.
    if input.analysis.trade_readiness == TradeReadiness.AVOID:
        return _make_hold(input, config, reason=_REASON_LLM_AVOID)

    # Step 3 — Clamp anomaly (validator detected an LLM-output sanity
    # violation; treat as unreliable for this cycle).
    # Note: clamp_anomaly is a field on LLMAnalysis per the design doc;
    # if the attribute doesn't exist yet on the model we land here, the
    # getattr default protects us until A.1 lands the field.
    if getattr(input.analysis, "clamp_anomaly", False):
        return _make_hold(input, config, reason=_REASON_CLAMP_ANOMALY)

    # Step 4 — LLM said Hold; policy agrees by default. No rejection
    # reason since this isn't a policy override.
    if input.advisory.action == "Hold":
        return FinalTradeDecision(
            action="Hold",
            qty_tier="zero",
            stop_loss_atr_multiple=input.advisory.stop_loss_atr_multiple,
            take_profit_atr_multiple=input.advisory.take_profit_atr_multiple,
            rejection_reason=None,
            bucket_key=_bucket_key_from_input(input),
            policy_version=config.policy_version,
        )

    # Step 4b — Holding the name; translate position_action regardless
    # of the advisory (the advisory talks about whether to OPEN a
    # position; position_action talks about MANAGING an existing one).
    if input.ctx.currently_holding:
        return _translate_position_action(input, config)

    # Step 5 — bucket lookup is the CALLER's responsibility (the
    # hierarchical_lookup() function in this module is the helper they
    # use). The PolicyInput carries the pre-resolved bucket_history.
    # We record the bucket_key from the LLMContext as the LOGICAL
    # bucket, even if the caller resolved a collapsed variant — the
    # caller can override via FinalTradeDecision construction if it
    # wants to log the actually-used bucket.
    base_bucket_key = _bucket_key_from_input(input)

    # Step 6 — sample-count tier sizing.
    tier, rejection = _tier_from_bucket(input.bucket_history, config)
    if rejection is not None:
        return _make_hold(input, config, reason=rejection)

    # Step 7 — red-flag downgrade.
    tier = _apply_red_flag_downgrade(tier, input.features, config)
    if tier == "zero":
        return _make_hold(input, config, reason=_REASON_RED_FLAG_DOWNGRADE)

    # Step 7.5 — Liquidity hard reject (review #2 addition). Distinct
    # from the red-flag downgrade above: this rejects outright rather
    # than stepping down a tier. Fires when liquidity is so poor that
    # slippage assumptions break down regardless of bucket history.
    if (
        input.features.spread_bps > config.liquidity_max_spread_bps
        or input.features.rvol_percentile < config.liquidity_min_rvol_percentile
    ):
        return _make_hold(
            input, config, reason=_REASON_LIQUIDITY_REJECT,
            liquidity_rejected=True,
        )

    # Step 8 — stop/target clamps for regime + min reward/risk.
    stop_mul, tp_mul = _clamp_stop_target(
        input.advisory, input.ctx.market_regime_label, config
    )

    # Step 8.5 — EV scoring (review #2 addition). Logged only; no gate
    # until the calibrator is wired (blocked on shadow_outcomes data).
    # Pwin proxy is advisory.confidence / 100; will become
    # calibrated_pwin once analysis/calibration.py lands.
    ev_score = (
        (input.advisory.confidence / 100.0)
        * input.advisory.expected_move_pct
    )

    # Step 9 — Construct the decision.
    return FinalTradeDecision(
        action=input.advisory.action,
        qty_tier=tier,
        stop_loss_atr_multiple=stop_mul,
        take_profit_atr_multiple=tp_mul,
        rejection_reason=None,
        bucket_key=base_bucket_key,
        policy_version=config.policy_version,
        ev_score=ev_score,
        liquidity_rejected=False,
    )


__all__ = [
    # Types
    "HealthState",
    "QtyTier",
    "MarketFeatures",
    "BucketStats",
    "AccountState",
    "PolicyInput",
    "FinalTradeDecision",
    "PolicyConfig",
    # Public entry points
    "decide",
    "hierarchical_lookup",
    "bucket_key_for",
]
