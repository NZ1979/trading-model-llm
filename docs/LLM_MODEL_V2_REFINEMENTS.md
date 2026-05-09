# LLM Model V2 Refinements — Design Spec

This is the design contract for the next phase of the LLM trading model.
It supersedes v1's "LLM as direct trader" pattern with "LLM as analyst +
deterministic TradePolicy" pattern, plus shadow analytics, fail-closed
hierarchy, and schema splits. After sign-off here, implementation work
begins; v1 code paths remain in place until the v2 paths are validated
end-to-end via the M2 replay harness.

Reference docs:
- `LLM_SIGNAL_INTERFACE.md` — v1 contract (output schema, prompt template, tier orchestration)
- `M2_REPLAY_HARNESS_DESIGN.md` — v1 replay design
- `HARDWARE_PLATFORM.md` — workstation context
- `LLM_MODEL_CHARTER.md` — project charter

## Why v2

V1 has the LLM emit a final `action` + `confidence` + `reasoning`, validates
the schema, runs the result through the existing risk validator, and sends
to execution. The structure is clean and conservative, but it makes the LLM
the trader. Three problems with that:

1. **LLMs are better at classification than at decisions.** Asking Claude
   "should I buy NVDA?" is asking for a numerical judgment that depends on
   inputs the LLM cannot weigh quantitatively (current portfolio exposure,
   historical realized P&L by bucket, liquidity, slippage). Asking the LLM
   "what kind of catalyst is this, what's the setup type, what would
   invalidate this thesis" is a classification problem the LLM is good at.

2. **Calibrating an LLM's confidence directly is a research project.**
   Calibrating a deterministic scorer that consumes LLM classifications
   plus realized P&L by bucket is straightforward.

3. **Audit and tuning are coupled to prompt edits.** When the LLM is the
   trader, changing how the system trades requires a prompt change, which
   forces a re-validation of every downstream behavior. When the LLM is
   the analyst, prompt changes affect classification quality only; the
   policy layer can be tuned independently against historical data.

V2 keeps the LLM where it adds value (catalyst interpretation, setup
classification, risk identification, alternative-thesis surfacing) and
moves trade decisions to a deterministic, auditable, independently-tunable
TradePolicy module.

## Core architectural change

```
v1 (current):
  Pre-filter --> LLM (Tier 1/2/3) --> LLMDecision --> Risk validator --> Execution

v2 (this doc):
  Pre-filter --> LLM (Tier 1/2/3)
                    |
                    v
              LLMAnalysis (classifications)  +  LLMDecision (advisory)
                                                            |
                                                            v
                                              TradePolicy (deterministic)
                                                            |
                                                            v
                                              FinalTradeDecision --> Risk validator --> Execution
```

The LLM no longer decides what trade to place. It classifies the setup,
flags risks, and emits an *advisory* action. A new `strategy/llm/policy.py`
module (deterministic, tested in isolation, independently tunable) consumes:

- `LLMAnalysis` (classifications + risk tags)
- The advisory `LLMDecision` (LLM's own action + confidence)
- Market features (RVOL percentile, ATR distance, VWAP relation, spread, ADV)
- Account state (current exposure, position-by-symbol, daytrade count)
- Historical realized P&L for the matching bucket

and produces a `FinalTradeDecision` (action, qty, stop, target, sizing tier).
That decision goes through the existing risk validator and bracket-order
placer, unchanged.

## Tier A — architectural, do first

### A.1 Schema split: LLMAnalysis + advisory LLMDecision

New module: `strategy/llm/analysis.py`

```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, field_validator


class CatalystQuality(str, Enum):
    MAJOR = "major"          # FDA approval, M&A confirmed, earnings beat + guidance raise
    MATERIAL = "material"    # Significant analyst action, regulatory item, secondary
    MINOR = "minor"          # Small product news, minor analyst note
    AMBIGUOUS = "ambiguous"  # Headlines exist but unclear impact (rumor without confirmation)
    NONE = "none"            # No qualifying news in window


class SetupType(str, Enum):
    GAP_AND_GO = "gap_and_go"
    PULLBACK_IN_TREND = "pullback_in_trend"
    BREAKOUT_CONFIRM = "breakout_confirm"
    BREAKDOWN = "breakdown"
    REVERSAL = "reversal"
    CONSOLIDATION = "consolidation"
    NO_SETUP = "no_setup"


class TradeReadiness(str, Enum):
    READY = "ready"                  # All conditions aligned for entry now
    WAIT_PULLBACK = "wait_pullback"  # Setup good but current entry premium too high
    WAIT_BREAKOUT = "wait_breakout"  # Setup forming, not yet confirmed
    AVOID = "avoid"                  # Real red flags present


class PositionAction(str, Enum):
    """For decisions on already-held positions, evaluated every 5 min."""
    HOLD = "hold"                    # No change
    TRIM = "trim"                    # Reduce size (e.g. take 1/2 off into strength)
    EXIT = "exit"                    # Close immediately
    TIGHTEN_STOP = "tighten_stop"    # Move stop closer to current price
    SCALE_UP = "scale_up"            # Add to position
    NO_OPINION = "no_opinion"        # LLM cannot judge; defer to bracket stop


class LLMAnalysis(BaseModel):
    """Classification output from the LLM. Inputs to TradePolicy."""

    # ---- Catalyst & setup classification ----
    catalyst_quality: CatalystQuality
    setup_type: SetupType
    trade_readiness: TradeReadiness

    # ---- Risk identification ----
    invalid_if: str = Field(max_length=200)
    # The single condition that would void this thesis. Plain English.
    # Example: "if SPY breaks below 5950 on a closing 5-min bar"
    # Example: "if NVDA fails to hold above PM low of 147.20"

    primary_concerns: list[str] = Field(default_factory=list)
    # 1-5 short tags for risks the operator should be aware of.
    # Example: ["mega_cap_gap_fade", "VIX_low", "earnings_within_3d"]

    counter_thesis: str = Field(max_length=200)
    # The opposing argument. One sentence. Forces the LLM to consider both
    # sides and gives the policy layer a perspective when the primary
    # action seems weak.

    suggested_horizon: Literal["intraday", "overnight", "multi_day"] = "intraday"

    # ---- Position management (only meaningful when ctx.currently_holding) ----
    position_action: PositionAction = PositionAction.NO_OPINION
    position_action_reasoning: str = Field(default="", max_length=200)

    @field_validator("primary_concerns")
    @classmethod
    def _trim_concerns(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()][:5]
```

`LLMDecision` is preserved unchanged from v1, but its role narrows: it
becomes the LLM's *advisory* opinion, not the final decision. The LLM still
emits action + confidence; the TradePolicy may agree, override, or ignore.

The combined LLM output is a wrapper:

```python
class LLMOutput(BaseModel):
    analysis: LLMAnalysis
    advisory: LLMDecision  # the existing v1 schema
    raw_response: dict
```

### A.2 Shadow-mode analytics infrastructure

The single biggest blocker to measuring whether v2 works. Add a
`shadow_outcomes` SQLite table populated asynchronously by a follower
process that subscribes to the bar feed and joins back to recorded
decisions.

```sql
CREATE TABLE shadow_outcomes (
    decision_id INTEGER PRIMARY KEY,
    -- Forward returns (price change since decision, by horizon)
    return_5m_pct REAL,
    return_15m_pct REAL,
    return_30m_pct REAL,
    return_60m_pct REAL,
    return_eod_pct REAL,

    -- Maximum Adverse / Favorable Excursion
    mae_pct REAL,    -- Worst price excursion against the decision direction
    mfe_pct REAL,    -- Best price excursion in the decision direction
    mae_at_minutes INTEGER,
    mfe_at_minutes INTEGER,

    -- Would-have-stopped / would-have-target-hit
    stop_would_hit BOOLEAN,
    stop_hit_at_minutes INTEGER,
    target_would_hit BOOLEAN,
    target_hit_at_minutes INTEGER,
    first_touch TEXT,    -- "stop" | "target" | "neither" (eod) | "n/a" (Hold)

    -- Liquidity proxy
    avg_spread_bps REAL,  -- Mean bid-ask spread during the eval window
    estimated_slippage_bps REAL,

    -- Computed at populate time
    populated_at TEXT NOT NULL,
    horizon_complete TEXT NOT NULL,  -- The latest horizon whose data is final

    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
```

The follower:

1. Subscribes to the same bar feed the live trader uses
2. Maintains a queue of recently-recorded decisions awaiting outcome data
3. After each new 5-min bar, computes incremental forward returns for queued decisions whose horizon has just become measurable
4. Computes MAE/MFE against decision-time price for each horizon
5. Simulates would-stop-or-target-first using the decision's stop_loss_atr_multiple and take_profit_atr_multiple, applied to daily ATR(14) at decision time
6. Estimates spread/slippage from the bar's high-low range and the ratio of decision price to subsequent bar's open

Critical: this must be populated for **every** evaluated candidate, including
Holds and including decisions the policy layer rejected. A Hold with
strongly-positive forward returns is a missed-opportunity datapoint and
informs threshold tuning.

The `shadow_outcomes` table feeds the bucket expectancy calculation that
TradePolicy uses (see A.3 below).

### A.3 Fail-closed hierarchy

Replace v1's "Tier 2 picks up everything when Tier 1 dies" with a graded
degradation hierarchy. Each level has a specific health check and a
specific response.

| Health state | Detection | Response |
|---|---|---|
| Tier 1 healthy | Successful T1 call within last 5 min | Normal: T1 always, T2 on escalation |
| Tier 1 slow (>8s) | Two consecutive T1 timeouts | Narrow universe to top-30 by RVOL + catalyst flag; defer rest |
| Tier 1 down | Three consecutive T1 failures | Degrade to gap-and-go rule-based fork's logic; flag operator |
| Tier 2 down (escalation needed) | Anthropic 5xx after retries on a T2 call | Use T1 alone, log; do not promote to all-T2 |
| Anthropic budget exhausted | Daily $ tracker | Hold-only for rest of day; alert |
| News feed stale (>5 min no msgs) | News WS heartbeat | Block catalyst-dependent setups; flag in shadow logs |
| Bar feed stale | Alpaca SIP heartbeat | Hold-only |
| Clock skew (NTP > 30s drift) | Periodic NTP check | Hold-only |

The key change from v1: **Tier 1 outage degrades to the gap-and-go fork's
deterministic logic, not to cloud-everywhere.** The rule-based fork is
already running, already proven, and free. Cloud-everywhere during an
outage is the worst-case combination (high cost + high latency + cloud
dependency at exactly the moment something else is broken).

Implementation: a `HealthState` dataclass tracked by main.py; the signal
engine reads it and selects its evaluation path accordingly.

## Tier B — quality, do alongside

### B.1 Position management evaluator (5-min cadence)

Held positions get re-evaluated every 5 min, same cadence as new candidate
evaluation. Cost is acceptable; we are not constrained on calls per cycle.

When `ctx.currently_holding == True`:

- The prompt template includes the position state block (already in v1)
- The LLM emits `LLMAnalysis.position_action` (HOLD | TRIM | EXIT | TIGHTEN_STOP | SCALE_UP | NO_OPINION)
- The TradePolicy maps the action to a concrete order:
  - HOLD: no order
  - TRIM: market sell N% of qty (default 50%; configurable)
  - EXIT: market close position
  - TIGHTEN_STOP: replace existing stop with a tighter stop computed from current price minus 0.5 ATR
  - SCALE_UP: bracket order for additional qty if exposure cap allows; else HOLD
  - NO_OPINION: no order; bracket stop continues to govern

Rationale for keeping 5-min and not moving to 15: held positions are *more*
important than candidate evaluations. We do not want a faster eval cadence
on potential entries than on actual capital at risk.

### B.2 Multi-condition escalation

Replace v1's single confidence-band gate with OR-of-triggers:

```python
def should_escalate(ctx, t1_decision, t1_analysis, budget, features) -> tuple[bool, str]:
    """Return (escalate, reason). reason recorded in decisions for analysis."""
    if not budget.has_capacity():
        return (False, "budget_exhausted")

    # Trigger 1: confidence in uncertain band + real catalyst (v1 rule)
    if (50 <= t1_decision.confidence <= 75
        and t1_analysis.catalyst_quality in {CatalystQuality.MAJOR, CatalystQuality.MATERIAL}
        and ctx.pm_rvol > 3.0):
        return (True, "uncertain_with_catalyst")

    # Trigger 2: high confidence but deterministic features red-flag
    # (e.g. T1 says Buy with 85 confidence, but spread is wide or volume is dying)
    if (t1_decision.confidence > 75
        and t1_decision.action != "Hold"
        and features.has_red_flag()):
        return (True, "high_conf_red_flag")

    # Trigger 3: fresh material catalyst regardless of confidence band
    # (catches the case where T1 was conservative on a real event)
    if (t1_analysis.catalyst_quality == CatalystQuality.MAJOR
        and ctx.news_freshness_min < 30
        and t1_decision.action == "Hold"):
        return (True, "missed_major_catalyst")

    # Trigger 4: open position approaching stop or target
    if (ctx.currently_holding
        and (features.distance_to_stop_atr < 0.5 or features.distance_to_target_atr < 0.5)):
        return (True, "position_near_decision_point")

    # Trigger 5: market regime changed since position was entered
    if (ctx.currently_holding
        and ctx.regime_at_entry != ctx.market_regime_label):
        return (True, "regime_shift_with_position")

    return (False, "no_trigger")
```

The reason string is recorded in the decisions table so we can later
measure: which escalation triggers actually produced wins or losses?
Triggers that show negative expectancy after 30+ samples get dropped.

### B.3 Schema clamping observability

Continue to clamp/truncate at parse time per v1, but emit metrics and tag
the decision:

```python
class LLMDecision(BaseModel):
    # ... existing fields ...
    clamp_anomaly: bool = False
    clamp_details: list[str] = Field(default_factory=list)
    # Examples of clamp_details entries:
    #   "confidence: 150 -> 100"
    #   "reasoning: truncated 312 -> 280"
    #   "stop_loss_atr_multiple: 5.0 -> 3.0"
```

Validators set `clamp_anomaly=True` and append a description string when
they fire. The signal engine logs a counter metric per clamp event.

Threshold rule: if more than 5% of decisions in any rolling 100-call window
have `clamp_anomaly=True`, switch the entire model to Hold-only and alert.
Persistent clamping means the LLM is producing structurally bad output and
the underlying classifications are not trustworthy either.

### B.4 Regime-stratified deployment gates

Bucket decisions on five dimensions:

- Daily regime: bull | bear | neutral
- Cap size: mega | large | mid | small
- Catalyst presence: major | material | minor | none
- Time of day: gap_and_go_window | morning | midday | afternoon | close
- Long vs short: long | short

That is 3 × 4 × 4 × 5 × 2 = 480 buckets. Most will have insufficient
samples. We deploy decisions only from buckets that have:

- At least 30 historical samples in the M2 replay window
- Positive expected R after slippage
- A confidence interval on expected R that does not cross zero

Buckets that fail any condition stay in shadow mode (decisions logged, no
orders placed). The replay report includes a per-bucket breakdown so the
operator can see which buckets are profitable and which need more data.

## Tier C — hardening, do before live paper orders

### C.1 Determinism pinning

Local Tier 1 inference parameters:

- `temperature: 0.0`
- `top_p: 1.0`
- `top_k: 1`
- `seed`: pinned to a fixed value per prompt_version

Verify with a "same prompt 10x = same output 10x" assertion in the M2
harness. LM Studio does not always pass sampling parameters through to the
underlying model identically; the assertion catches this.

If the assertion fails, document the non-determinism in the M2 reports and
do not claim "deterministic replay." Aim for "byte-identical 95%+ of the
time" as the realistic target for local 4-bit quantized inference.

### C.2 Survivorship-bias-free historical universe

The iShares IWM CSV endpoint we use returns *current* holdings only. A
30-day replay against the current holdings excludes any names that were
delisted or replaced during the window. This is survivorship bias.

Two paths:

- **Pragmatic** (do this first): start snapshotting the IWM holdings file
  daily into `data/iwm_holdings/YYYY-MM-DD.csv` going forward. Replays
  inside the snapshot window use the as-of-date holdings. Replays before
  Phase D (which is when snapshotting starts) carry a documented
  survivorship-bias caveat.

- **Comprehensive** (later): integrate a CRSP or Polygon Reference Data
  feed that includes delisted symbols and historical index constituents.
  Cost is meaningful (~$50-200/mo for the relevant tier); defer until
  pragmatic snapshots prove the methodology.

### C.3 Reason field expansion

Bump `LLMDecision.reasoning` from 280 to 500 chars. Observed Haiku
behavior in the smoke test was to truncate at the boundary, often cutting
off the "but do not trade if..." caveat at the end. Storing the full
pre-truncation text in `raw_response` for the audit trail.

`LLMAnalysis.invalid_if` gets its own dedicated 200-char field (already
specified above) so the critical "what would void this thesis" never
gets truncated alongside the body.

### C.4 Network-split protection

The workstation hosts Tier 1 inference; the VPS hosts execution. They
communicate over the public internet. A network split means the trader is
deciding without LLM input.

- Heartbeat: workstation publishes a `/health` endpoint; VPS pings every 30s
- Stale-decision: any LLMDecision older than 6 minutes (slightly past one
  evaluation cycle) is treated as stale; the candidate is re-evaluated or
  Hold is returned
- Fail-closed: on three consecutive missed heartbeats, the trader switches
  to the rule-based fork's logic via the Tier 1 down branch in A.3

## TradePolicy module spec

`strategy/llm/policy.py` — new module. Pure-deterministic, fully unit-testable.

### Inputs

```python
@dataclass(frozen=True, slots=True)
class PolicyInput:
    ctx: LLMContext
    analysis: LLMAnalysis
    advisory: LLMDecision
    features: MarketFeatures
    account: AccountState
    bucket_history: BucketStats  # Historical realized P&L for matching bucket


@dataclass(frozen=True, slots=True)
class MarketFeatures:
    rvol_percentile: float       # Where is current bar's volume in 20-bar history?
    spread_bps: float            # Current bid-ask spread in basis points
    distance_to_vwap_atr: float
    distance_to_stop_atr: float | None    # If holding
    distance_to_target_atr: float | None  # If holding
    has_red_flag: bool           # Composite: spread > 50bps OR volume_ratio < 0.7 OR rvol_percentile < 30


@dataclass(frozen=True, slots=True)
class BucketStats:
    bucket_key: tuple              # (regime, cap, catalyst, time_of_day, long_short)
    sample_count: int
    expected_r: float              # Mean realized R (return / risk)
    expected_r_lower_ci: float     # Lower 95% CI bound
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    last_updated: datetime
```

### Output

```python
@dataclass(frozen=True, slots=True)
class FinalTradeDecision:
    action: Literal["Buy", "Sell", "Hold"]
    qty_tier: Literal["zero", "tiny", "normal", "max"]
    # zero  = 0 shares (action becomes Hold)
    # tiny  = 0.25 * normal (validation / new bucket)
    # normal = configured risk_per_trade_pct
    # max   = 1.5 * normal (only after 100+ samples in bucket with positive expectancy)

    stop_loss_atr_multiple: float
    take_profit_atr_multiple: float
    rejection_reason: str | None   # Set when policy rejects an LLM Buy/Sell
    bucket_key: tuple              # For shadow logging
    policy_version: str            # Independently versioned from prompt
```

### Decision logic

```
1. If health state is not "Tier 1 healthy" → Hold (with reason from health state)
2. If analysis.trade_readiness == AVOID → Hold (rejection_reason="llm_avoid")
3. If clamp_anomaly == True → Hold (rejection_reason="clamp_anomaly")
4. If advisory.action == "Hold" → Hold (consistent with LLM)

5. Look up bucket_stats for (regime, cap, catalyst, time_of_day, long_short)
6. If bucket_stats.sample_count < 30:
       qty_tier = "tiny"   (paper-trading exploration only)
   Else if bucket_stats.expected_r_lower_ci <= 0:
       Hold (rejection_reason="bucket_negative_expectancy")
   Else if bucket_stats.expected_r_lower_ci > 0.30 AND sample_count > 100:
       qty_tier = "max"
   Else:
       qty_tier = "normal"

7. If features.spread_bps > 50 OR features.rvol_percentile < 20:
       qty_tier = downgrade by one tier (or Hold if already tiny)

8. Stop / target multiples:
       Use advisory's stop_loss_atr_multiple and take_profit_atr_multiple,
       BUT clamp to max 2.0 ATR stop in choppy regime,
       BUT enforce minimum 1.5 reward-to-risk ratio (target/stop ≥ 1.5).

9. Return FinalTradeDecision
```

### Position-management mapping

When `ctx.currently_holding`, the policy translates `analysis.position_action`:

```
HOLD          → no order
TRIM          → market sell qty * trim_pct (default 0.5)
EXIT          → market close position
TIGHTEN_STOP  → replace stop with current_price - 0.5 ATR (long) or +0.5 ATR (short)
SCALE_UP      → bracket order for additional qty IF exposure cap allows; ELSE HOLD
NO_OPINION    → no order
```

All position-management orders go through the same risk validator as new
entries.

## Bucket history calculation

The `BucketStats` lookup table is built from historical decisions joined to
`shadow_outcomes`:

```sql
-- Bucket expectancy (run nightly; cached in memory at trader boot)
SELECT
    regime, cap_size, catalyst_quality, time_of_day_bucket, long_short,
    COUNT(*) as sample_count,
    AVG(realized_r) as expected_r,
    AVG(realized_r) - 1.96 * STDDEV(realized_r) / SQRT(COUNT(*)) as expected_r_lower_ci,
    AVG(CASE WHEN realized_r > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
    AVG(CASE WHEN realized_r > 0 THEN realized_r END) as avg_win_r,
    AVG(CASE WHEN realized_r <= 0 THEN realized_r END) as avg_loss_r
FROM (
    SELECT
        d.id,
        d.regime, d.cap_size, d.catalyst_quality, d.time_of_day_bucket, d.long_short,
        -- Realized R = (return at first-touch) / (stop_distance_pct)
        CASE
            WHEN s.stop_would_hit AND s.first_touch = 'stop' THEN -1.0
            WHEN s.target_would_hit AND s.first_touch = 'target' THEN
                d.take_profit_atr_multiple / d.stop_loss_atr_multiple
            ELSE s.return_eod_pct / d.stop_loss_pct
        END as realized_r
    FROM decisions d
    JOIN shadow_outcomes s ON s.decision_id = d.id
    WHERE d.action != 'Hold'
      AND d.created_at > date('now', '-90 days')
) bucketed
GROUP BY regime, cap_size, catalyst_quality, time_of_day_bucket, long_short
HAVING COUNT(*) >= 5;  -- Even small buckets get logged for analysis
```

This is the loop that closes the system: live decisions create
shadow_outcomes; shadow_outcomes inform bucket expectancy; bucket
expectancy gates future decisions. Without shadow_outcomes infrastructure
(A.2), no other v2 work is meaningful.

## Sequencing

1. **A.2 Shadow analytics** (1-2 days). Database schema, follower process,
   first M2 replay populating shadow_outcomes for the last 30 days against
   v1 decisions already in the DB. Without this, every other improvement
   is unmeasurable.

2. **A.1 Schema split** (2 days). LLMAnalysis Pydantic class, prompt
   template additions to elicit the new fields, parser updates. Run
   alongside v1 LLMDecision in shadow mode for a week. Verify Haiku's
   classification quality before relying on it.

3. **A.3 Health state machine** (1 day). HealthState dataclass, periodic
   probes, the degradation hierarchy. Initial wiring: gap-and-go fork as
   T1-down fallback (which means the LLM model fork imports from gap-and-go
   for that path).

4. **TradePolicy module + tests** (2 days). Pure deterministic logic; full
   unit-test coverage. Wired into M2 replay for evaluation but not yet
   into live signal engine.

5. **M2 replay with v2** (3 days). Replay 60 days using the new schema and
   policy module. Generate per-bucket expectancy report. Identify which
   buckets show positive realized R.

6. **Tier B improvements** (2-3 days each, parallel). B.1 position
   management, B.2 multi-trigger escalation, B.3 clamp observability,
   B.4 deployment gates wired to bucket stats.

7. **Tier C hardening** (1 day each). C.1-C.4. All before live paper.

8. **Live paper-only deploy** of the buckets that passed the gate in step 5.
   Tiny qty_tier for the first month while shadow_outcomes accumulate at
   real-time pace. Promote bucket-by-bucket as sample counts grow.

Total estimated effort: 15-20 days of focused work to reach "first live
paper trade with v2 architecture." The schedule depends on what the M2
replay reveals; if no buckets show positive expectancy after 30 days of
replay, that itself is a finding and the question becomes "is the LLM
adding value at all, or do we need a different prompt / model?"

## Open questions (need answers before implementation)

1. **Bucket dimension count.** Five dimensions × default categorizations
   produces 480 buckets; most will be empty. Should some dimensions
   collapse (e.g. cap_size: just small_or_other vs mega_or_large)?
   Initial proposal: keep all five but report by sample count and let the
   data tell us which dimensions matter.

2. **Realized R calculation when neither stop nor target hits.** The query
   above falls back to `return_eod_pct / stop_loss_pct`. This assumes the
   position was held to flatten. For overnight or multi_day suggestions
   this is wrong, but those are out of scope today (system flattens at
   15:55 ET for all positions). Revisit when overnight is enabled.

3. **TradePolicy parameter tuning approach.** The thresholds (30 samples
   minimum, 0.30 expected_r for max tier, 50 bps spread red flag) are
   pulled from intuition. After M2 replay we should grid-search these
   against historical buckets and pick the values that maximize
   out-of-sample Sharpe. Until then, document the hand-picked values and
   review monthly.

4. **Policy versioning lifecycle.** When we change a TradePolicy
   threshold, every prior decision's recorded `policy_version` becomes
   ambiguous: was it produced by the old or new policy? Proposal: bump
   the policy_version string and treat it as a backtest cutover boundary.
   Decisions before the bump are evaluated against old policy expectancy;
   after, against new.

5. **LLM classification ground-truth.** How do we measure whether the
   LLM is correctly classifying catalyst_quality? Options:
   - Human spot-check 50 random LLM classifications per week
   - Cross-check against Tier 3 Opus on the same context
   - Use the realized P&L of "MAJOR catalyst" decisions as a proxy
     (true MAJORs should outperform true MINORs systematically)
   Initial proposal: do all three.

## Out of scope (preserved from v1)

- High-frequency execution; cycle remains 5-minute bars
- Options, futures, non-US equities
- Replacement of the gap-and-go rule-based fork (LLM model is parallel,
  not substitute)
- Position management beyond the cadence specified in B.1 (no continuous
  trailing stops, no percentage-based take-profits)

## Status

This is the design contract for v2. Sign-off here means we agree on:

- The architectural change (LLM as analyst + deterministic TradePolicy)
- The schema split (LLMAnalysis + advisory LLMDecision wrapped in LLMOutput)
- Shadow-mode analytics as the precondition for everything else (A.2 first)
- The fail-closed hierarchy (degrade to gap-and-go fork, not to cloud-everywhere)
- Position management at 5-min cadence
- Bucket-stratified deployment gating
- Sequencing: shadow analytics → schema → policy → M2 replay → Tier B/C → live

After sign-off, implementation work begins on A.2.
