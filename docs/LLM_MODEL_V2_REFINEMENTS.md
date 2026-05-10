# LLM Model V2 Refinements — Design Spec

This is the design contract for the next phase of the LLM trading model.
It supersedes v1's "LLM as direct trader" pattern with "LLM as analyst +
deterministic TradePolicy" pattern, plus shadow analytics, fail-closed
hierarchy, schema splits, and an explicit profit-maximization objective.
After sign-off here, implementation work begins; v1 code paths remain in
place until the v2 paths are validated end-to-end via the M2 replay harness.

## 0. Primary Objective: Maximize Risk-Adjusted Profit

The primary goal of the LLM trading model is to maximize realized
financial profit on the paper account, measured as a risk-adjusted
return metric and subject to hard drawdown constraints.

**Optimized metric**: 90-day rolling **Calmar ratio** = annualized P&L ÷
maximum drawdown over the window. Calmar is preferred over Sharpe because
it penalizes only drawdowns (the kind of volatility that actually hurts an
account), not all volatility (Sharpe penalizes upside volatility too).
Professional CTAs report Calmar to investors for the same reason: it
matches the right intuition about strategy quality.

**Secondary metrics tracked alongside** (informational, not directly optimized):
- Sharpe ratio (annualized)
- Win rate
- Expectancy per trade in R-multiples
- Profit factor (gross wins ÷ gross losses)
- Best / worst day
- Average days between new equity highs

**Hard constraints** (any breach forces Hold-only mode until investigated):
- Maximum drawdown ceiling: 15% from account peak
- Single-day loss cap: 5% of account equity
- Per-trade risk cap: 1.0% (raised from v1's 0.5% once edge is proven)
- Total exposure cap: 90% (unchanged)
- Operational: any system breach (clamp anomaly storm, network split, data staleness) forces Hold-only

**What "maximize profit" specifically means in design choices:**

The v1 design has a "do no harm" posture ("be no worse than base, prefer
Hold"). V2 elevates profit to primary, which changes specific tradeoffs
without removing the safety constraints:

1. **Position sizing scales with proven edge.** Once a bucket has 30+
   samples with statistically significant positive expectancy, position
   size grows toward the per-trade risk cap (capped at half-Kelly for
   safety) instead of staying tiny indefinitely.

2. **Take-profit logic actually fires.** Bracket orders include both stop
   and take-profit legs. Positions that hit target during the day exit
   immediately at broker level rather than waiting for the 15:55 ET
   flatten. See § B.1 for the full multi-layer profit-protection design.

3. **Hold default is a tunable, not a virtue.** The "Hold by default"
   bias becomes a numerical threshold: trade when expected R clears the
   profit-optimization threshold. The threshold is tuned against
   historical Calmar, not pinned to a conservative constant.

4. **Position-management LLM is profit-positive, not just risk-positive.**
   SCALE_UP and TAKE_PARTIAL become first-class actions alongside
   defensive ones. When a setup is working, adding to a winner is
   profit-maximizing; when a setup has run, banking partial profit and
   letting the rest ride is profit-maximizing.

5. **Bucket gating uses Calmar contribution, not just expected R.** A
   bucket with marginal point-estimate expectancy can still be deployed
   if its returns are uncorrelated with other deployed buckets and it
   reduces portfolio drawdown (diversification benefit).

6. **15:55 ET flatten becomes the safety net, not the exit strategy.**
   Most exits should happen earlier via take-profit (broker-side),
   trailing stop (deterministic), or LLM EXIT/TAKE_PARTIAL signals.
   The 15:55 routine catches stragglers; well-managed positions are
   already closed by then.

**What does NOT change under profit-max framing:**
- Risk validator stays as the final gate; no LLM or policy output bypasses it
- Fail-closed hierarchy stays; profit-max during a degraded system is dangerous
- Schema validation, clamp-anomaly tracking, regime-stratified gating all stay
- Discrete qty_tier system stays (we add Kelly-fraction *within* a tier rather than removing tiers)
- All paper trading until 100+ historical samples in a deployed bucket show realized positive Calmar

The objective and its constraints are codified once here. The rest of the
doc references back to this section when an implementation choice is a
profit-vs-conservatism tradeoff.

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
    """For decisions on already-held positions, evaluated every 5 min.

    Profit-aware action set under v2's max-profit framing. See § B.1
    Layer 3 for the full action -> order mapping in TradePolicy.
    """
    HOLD = "hold"                      # No change; trust bracket + trailing stop
    SCALE_UP = "scale_up"              # Add to a working position
    TAKE_PARTIAL = "take_partial"      # Sell 1/3 to 1/2 to bank profit, let rest run
    TRIM = "trim"                      # Sell 50% defensively (uncertainty rising)
    EXIT = "exit"                      # Close immediately (LLM call, not stop hit)
    TIGHTEN_STOP = "tighten_stop"      # LLM-driven stop tightening beyond ratchet
    NO_OPINION = "no_opinion"          # Defer to bracket + trailing stop


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

### B.1 Profit-maximizing position management

The single biggest change between v1 and v2 trading behavior. V1 placed a
bracket order with a stop and then waited for either the stop to hit or
15:55 ET to flatten. V2 actively works the position throughout the day to
capture wins as they develop, protect them as they grow, and re-deploy
capital from setups that have stalled.

The design is a **defense-in-depth stack of five layers**, each cheaper
than the next-higher layer at catching the most common case, with the
expensive layers handling cases the cheap ones miss. The 15:55 ET flatten
remains as the final safety net, but a well-managed position is rarely
still open at 15:55.

#### B.1 Layer 1: Static take-profit leg (broker-side, instant)

**Every bracket order includes both a stop AND a take-profit leg.**

Currently `execution/alpaca_orders.py::submit_bracket_order` accepts a
`take_profit_limit_price` parameter but the v1 caller passes None for it.
V2 wires it: TP price is set at `entry + (take_profit_atr_multiple × daily_ATR_14)`
for longs, or `entry - (take_profit_atr_multiple × daily_ATR_14)` for shorts.

**Why this layer first:** zero latency. When price prints at TP, Alpaca's
servers fill the order immediately, before our next 5-min eval cycle
fires. Captures the case where a setup runs 2-3R inside a single 5-min bar
and would have faded back below target by the next eval.

**Implementation effort:** 0.5 day. The Alpaca OTO order shape already
supports both legs; we just need to compute and pass the TP price at
order submission time.

#### B.1 Layer 2: Profit-locking trailing stop (deterministic, every cycle)

**Once a position is in profit by 1R or more, the stop ratchets up to
lock in gains.** Pure deterministic logic; no LLM call needed. Runs every
5-min cycle in the policy module.

Ratchet schedule (R = entry-to-stop distance, in dollars):

| Position state | New stop level |
|---|---|
| Position up by less than 1R | Original stop (no change) |
| Position up by 1R or more | Move stop to entry + 0.10R buffer (effectively breakeven) |
| Position up by 2R or more | Move stop to entry + 1.0R (lock 50% of best move) |
| Position up by 3R or more | Move stop to entry + 2.0R (lock ~67% of best move) |
| Position up by 4R or more | Trail stop at high-water-mark minus 1.5R |

The ratchet is **one-way**: a stop never moves backward (looser). If price
pulls back from a peak, the stop stays at the highest level it reached.

Implementation: each 5-min cycle, for every open position, compute the
target trailing-stop level. If the target is tighter than the current
stop, cancel the existing stop child and submit a new one at the target
level. Use the same Alpaca DELETE-then-POST pattern that
`replace_stop_for_position` uses today.

**Why this layer matters:** captures the case where a position runs to 3R
peak and then fades over the next 30 min. Without this, the original stop
1R below entry sits unchanged and we give back the entire gain. With this,
we exit at the locked-in 2R level when the pullback hits.

**Race condition note:** between the bracket TP filling and the eval
running its trailing-stop update, both could fire on a fast move. That's
fine; whichever broker-side order fills first wins, the other becomes a
no-op. We just need to handle "position not found" cleanly when updating
a stop on a position that just got TP'd.

#### B.1 Layer 3: LLM position-management evaluator (5-min cadence)

Held positions get re-evaluated every 5 min, same cadence as new candidate
evaluation. The LLM sees current unrealized P&L, distance to stop, distance
to TP, recent bars, and the news/sentiment context, and emits a refined
`LLMAnalysis.position_action`:

```
class PositionAction(str, Enum):
    HOLD          = "hold"           # No change; trust bracket + trailing stop
    SCALE_UP      = "scale_up"       # Add to a working position
    TAKE_PARTIAL  = "take_partial"   # Sell 1/3 to 1/2 to bank profit, let rest run
    TRIM          = "trim"           # Sell 50% defensively (uncertainty rising)
    EXIT          = "exit"           # Close immediately (LLM is making the call)
    TIGHTEN_STOP  = "tighten_stop"   # LLM-driven stop tightening beyond the deterministic ratchet
    NO_OPINION    = "no_opinion"     # Defer to bracket + trailing stop
```

TradePolicy mapping (when ctx.currently_holding):

| LLM action | Order placed |
|---|---|
| HOLD | No order; existing bracket + trailing stop continue |
| SCALE_UP | New bracket order for additional qty if exposure cap allows; else demote to HOLD with reason logged |
| TAKE_PARTIAL | Market sell `partial_fraction × qty` (default 0.5; configurable per setup type). Remaining position keeps its bracket and trailing stop. |
| TRIM | Same as TAKE_PARTIAL but signals "defensive" rather than "profit-banking" — used for shadow analytics bucket grouping |
| EXIT | Market close full position; cancel remaining bracket children |
| TIGHTEN_STOP | Replace stop with current-price minus 0.5 ATR (long) or plus 0.5 ATR (short), only if tighter than current stop |
| NO_OPINION | No order; bracket + trailing stop continue |

**Difference between TAKE_PARTIAL (new) and TRIM (existing):** semantically
identical action (sell a portion), but recorded with different `intent`
metadata so the shadow analytics can answer questions like "when LLM
flagged TAKE_PARTIAL, did the remainder continue to run?" vs "when LLM
flagged TRIM defensively, did the remainder fade?" The realized R on each
action class informs whether the LLM's profit-banking instinct is
positively expected or not.

**Difference between EXIT and the trailing stop hitting:** EXIT is the
LLM's active call to close (e.g., "thesis broken: SPY just broke key
support and our long is fading"). Trailing stop is mechanical; it fires
on price action regardless of whether the LLM has a view. Both outcomes
are logged separately for analysis.

**SCALE_UP becomes a first-class action under profit-max framing.**
V1 had it as an optional escape hatch; v2 elevates it: when a setup is
working strongly (LLM sees continuation strength + trailing stop already
locked in 1R+ of gains + market regime supports the direction), adding
to the winner is the profit-max move. The deterministic trailing stop
alone won't generate this; the LLM's holistic read is what triggers it.

#### B.1 Layer 4: Late-day exit bias (deterministic, time-of-day modifier)

After 14:30 ET (90 min before close), the policy applies an
exit-leaning modifier to LLM actions:

| Time window | Modifier |
|---|---|
| 09:35 - 14:30 ET | Normal: LLM action passes through unchanged |
| 14:30 - 15:00 ET | If LLM action is HOLD on a position in profit > 1R, downgrade to TIGHTEN_STOP (lock more of the gain) |
| 15:00 - 15:30 ET | If LLM action is HOLD on any position in profit, downgrade to TAKE_PARTIAL (bank half) |
| 15:30 - 15:55 ET | If LLM action is anything other than EXIT or HOLD-with-explicit-overnight-thesis, downgrade to EXIT (close defensively before flatten) |
| 15:55 ET | Unconditional flatten (existing safety net) |

**Why time-of-day matters for profit:** late-day liquidity thins, news
flow drops, mean-reversion pressure rises. A position in 2R profit at
13:00 has plenty of time to develop further; the same position at 15:30
is statistically more likely to give back gains than make new highs.
Banking the gain locks in profit and avoids the close-of-day flatten
slippage.

**The 15:55 ET flatten remains** as the final safety net, but it should
catch only stragglers — positions where every prior layer passed (LLM
recommended HOLD with overnight-thesis, time-of-day modifier didn't
override, no take-profit hit, no trailing stop hit). Most days, by
15:55 ET there should be nothing left to flatten.

#### B.1 Layer 5: [Deferred] Price-event-triggered evaluation

A 5-min eval cadence has a structural blind spot: a position can run 2R
inside one 5-min bar (entry at 09:35, target at 09:36 inside the bar that
prints at 09:40), and our next eval doesn't fire until 09:40. Layer 1's
broker-side TP catches the literal target hit; but we miss the case where
price runs 1.7R, stalls, fades back to 0.5R, all within one 5-min bar,
without ever printing exactly at our TP price.

**Mitigation deferred to a later phase.** Possible future approaches:
- Subscribe to 1-min bars for tickers with open positions; trigger a
  position-mgmt eval when price moves more than 1 ATR from entry within a
  single 1-min bar
- Use Alpaca's trade-update WebSocket to fire evals on every fill
- Add a polling loop (every 30s) that checks each open position's current
  price vs entry and fires an eval if a threshold is crossed

These add complexity (new feeds, reconciliation against the 5-min cycle)
without proven value. Defer until shadow analytics show that meaningful
profit is being missed inside the 5-min window. If the 60-day replay
shows that >5% of profitable trades had a peak-to-trough decline within
the 5-min bar of >0.5R that was unrecovered by next-bar exit, build
Layer 5. Otherwise, the four layers above are sufficient.

#### B.1 Layered defense summary

| Layer | Mechanism | Latency | Catches |
|---|---|---|---|
| 1. Static TP leg | Broker-side bracket order | ~zero | Price printing exactly at target |
| 2. Trailing stop | Deterministic policy, 5-min cycle | Up to 5 min | Position runs to peak then fades |
| 3. LLM eval | Tier 1 evaluation, 5-min cycle | 5 min + LLM latency | Thesis breakage, regime shift, continuation strength |
| 4. Late-day modifier | Time-of-day rule on LLM action | Same as Layer 3 | Late-day fade risk |
| 5. 15:55 flatten | Hard safety net | None | Anything still open |

A well-managed profitable position typically exits via Layer 1 (target
hit) or Layer 2 (peak then fade). Layer 3 closes positions when the
thesis breaks. Layer 4 handles the end-of-day fade pattern. Layer 5 is
the final guarantee that nothing carries overnight without explicit
intent. The 15:55 flatten should rarely have anything to do.

**Rationale for keeping 5-min cadence (not 15):** held positions are
*more* important than candidate evaluations. We do not want a faster eval
cadence on potential entries than on actual capital at risk. Cost is
not a constraint.

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

**Pre-step (do immediately, in parallel with everything below).**
**B.1 Layer 1: wire the static take-profit leg into bracket orders.**
Estimated 0.5 day. No architectural prerequisites; touches only
`execution/alpaca_orders.py::submit_bracket_order`'s caller and the order
config. This change improves production behavior the day it ships, well
before the v2 architecture is complete. Treat as a hotfix-class change:
land it, deploy it, then return to the architectural work below.

1. **A.2 Shadow analytics** (1-2 days). Database schema, follower process,
   first M2 replay populating shadow_outcomes for the last 30 days against
   v1 decisions already in the DB. Without this, every other improvement
   is unmeasurable. Calmar ratio computation uses these tables; without
   them we cannot measure progress against the primary objective.

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
   management Layers 2-4 (Layer 1 already shipped per pre-step; Layer 5
   deferred), B.2 multi-trigger escalation, B.3 clamp observability,
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

- The primary objective: maximize 90-day rolling Calmar subject to drawdown + per-trade risk constraints
- The architectural change (LLM as analyst + deterministic TradePolicy)
- The schema split (LLMAnalysis + advisory LLMDecision wrapped in LLMOutput)
- Shadow-mode analytics as the precondition for everything else (A.2 first)
- The fail-closed hierarchy (degrade to gap-and-go fork, not to cloud-everywhere)
- Position management at 5-min cadence with the five-layer profit-protection stack (Layer 1 ships first as a hotfix; Layer 5 deferred)
- Bucket-stratified deployment gating using Calmar contribution, not just expected R
- Kelly-fraction sizing within qty tiers once buckets prove out
- Late-day exit bias modifier in TradePolicy
- Sequencing: Layer 1 hotfix immediately → shadow analytics → schema → policy → M2 replay → Tier B/C → live

After sign-off, implementation work begins on Layer 1 (take-profit hotfix)
in parallel with A.2 (shadow analytics).
