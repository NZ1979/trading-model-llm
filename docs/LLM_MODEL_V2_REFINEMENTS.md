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

5. Look up bucket_stats via HIERARCHICAL FALLBACK (Q1 resolution):
       a. Try fully-granular bucket (regime, cap, catalyst, time_of_day, long_short).
          If sample_count >= sample_min_for_normal_tier → use it.
       b. Else collapse the lowest-information dimension (per nightly ANOVA
          ranking; default order: time_of_day → cap → ...) and retry.
       c. Continue collapsing until sample_count threshold met OR all
          dimensions collapsed (fall back to global prior).
       d. Record bucket_key_used in FinalTradeDecision for audit.
6. If bucket_stats.sample_count < sample_min_for_normal_tier (even after full collapse):
       qty_tier = "tiny"   (paper-trading exploration only)
   Else if bucket_stats.expected_r_lower_ci <= 0:
       Hold (rejection_reason="bucket_negative_expectancy")
   Else if bucket_stats.expected_r_lower_ci > expected_r_for_max_tier
           AND sample_count > sample_min_for_max_tier:
       qty_tier = "max"
   Else:
       qty_tier = "normal"

7. If features.spread_bps > spread_bps_red_flag
       OR features.rvol_percentile < rvol_percentile_red_flag:
       qty_tier = downgrade by one tier (or Hold if already tiny)

8. Stop / target multiples:
       Use advisory's stop_loss_atr_multiple and take_profit_atr_multiple,
       BUT clamp stop to stop_atr_clamp_choppy ATR in choppy regime,
       BUT enforce reward-to-risk ratio >= min_reward_to_risk.
       (All four threshold names are tunable per Q3; see policy.yaml.)

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

> **Updated 2026-05-12 (Q2 resolution).** The three-branch `CASE` formula
> below is superseded for buckets where orders were placed; those are
> computed from the `position_trace` event ledger per the Q2 resolution.
> The legacy SQL is retained for non-deployed decisions (Holds + rejected
> buckets) where no real position exists and counterfactual first-touch
> simulation is the only signal available. The live aggregation is a
> UNION of both sources, partitioned on `holding_day = 0` for entry
> expectancy. See "Supporting schema" below for the full updated query.

The `BucketStats` lookup table is built from historical decisions joined to
`shadow_outcomes` (legacy form shown for reference):

```sql
-- Bucket expectancy (run nightly; cached in memory at trader boot)
-- LEGACY shape — superseded by the UNION-based query in "Supporting schema"
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

## Open questions — resolved 2026-05-12

Each question below retains its original framing for context, followed by
the resolution agreed in the 2026-05-12 design session.

### Q1. Bucket dimension count

**Original framing.** Five dimensions × default categorizations produces
480 buckets; most will be empty. Should some dimensions collapse
(e.g. cap_size: just small_or_other vs mega_or_large)? Initial proposal:
keep all five but report by sample count and let the data tell us which
dimensions matter.

**Resolution.** Hierarchical bucket lookup at decision time; all 5
dimensions retained in storage (no info loss).

1. Store all 5 dimensions per decision row.
2. At decision time, look up the fully-granular bucket. If
   `sample_count >= 30`, use it.
3. Else, collapse the lowest-information dimension and retry. Continue
   until 30 samples or fall back to global prior.
4. Refresh the "which dim collapses first" ordering nightly via one-way
   ANOVA on realized R per dimension.

Precompute the 32 bucket-subset tables nightly; runtime is dictionary
walks. Default collapse priorities until data overrides:

1. `time_of_day`: collapse 5 → 3 (morning_open / midday / afternoon_close)
2. `cap_size`: collapse 4 → 2 (mega_large / mid_small) — liquidity is
   already captured by RVOL/spread features
3. Keep `catalyst_quality` at 4 (collapsing defeats the LLM's
   classification value)
4. Keep `regime` at 3, `long_short` at 2

Max-granular bucket count drops from 480 → 144. The `bucket_key` recorded
in `FinalTradeDecision` makes the inheritance chain auditable.

### Q2. Realized R calculation when neither stop nor target hits

**Original framing.** The query above falls back to
`return_eod_pct / stop_loss_pct`. This assumes the position was held to
flatten. For overnight or multi_day suggestions this is wrong, but those
are out of scope today (system flattens at 15:55 ET for all positions).
Revisit when overnight is enabled.

**Resolution.** Event-trace ledger, not branched SQL. Multi-day holds
allowed up to 3 trading days from this point forward (the "out of scope"
caveat in the original framing is rescinded). The schema and formula
described below handle both intraday and multi-day uniformly.

Realized R per decision is a position-weighted sum over the event ledger:

```
realized_r = SUM(qty_delta × (fill_price - entry_price))
             / (initial_qty × stop_distance_at_entry)
```

- `entry_price`: position-weighted average of ENTRY + SCALE_UP fills
- `stop_distance_at_entry`: original (not ratcheted) distance, so R is
  comparable across decisions

Event types in `position_trace`:

| Event | When emitted |
|---|---|
| ENTRY | Initial bracket fill |
| SCALE_UP | Additional qty added to a working position |
| TAKE_PARTIAL | Profit-banking sell of a fraction (intent=profit_banking) |
| TRIM | Defensive sell of a fraction (intent=defensive) |
| STOP_RATCHET | Trailing stop replaced; no fill, marker only |
| MORNING_BRACKET_REFRESH | 09:30 cancel-and-replace of bracket children on a multi-day hold; no fill, marker only |
| CARRY_OVERNIGHT | 15:55 carry decision for a position with trading_days_held < 3; no fill, marker only |
| STOP_HIT | Stop child filled (fill_price captures gap fills accurately) |
| TARGET_HIT | Take-profit child filled |
| EXIT_LLM | LLM-driven market close (intent=thesis_break or similar) |
| FLATTEN_1555 | Default 15:55 intraday flatten |
| MAX_DURATION_FLATTEN | Forced flatten at 15:55 on day 3 (trading_days_held >= 3) |

**Multi-day hard cap.** `trading_days_held >= 3` at the 15:55 routine
forces `MAX_DURATION_FLATTEN`, never a 4th-day open. The cap is
implementation-enforced, not LLM-discretionary.

**Carry gate.** A position is carried overnight only if all of:

- `suggested_horizon ∈ {intraday, multi_day}` is multi_day (note: collapse
  the v1 3-value horizon to 2 values: `intraday` and `multi_day`; the
  `overnight` value is redundant since the position-mgmt LLM re-evaluates
  each session anyway)
- `policy.allows_carry(bucket_key, position_state)` returns true (bucket
  has positive expectancy with sample_count >= 30 specifically for
  multi_day-tagged decisions; no operational red flags)
- `trading_days_held < 3` (hard cap)

**Bracket refresh.** At 09:30 each session for held positions, cancel
yesterday's stop+target children and re-place at current (post-ratchet)
levels. Use the same DELETE-then-POST pattern as the deterministic
trailing-stop logic. GTC OTOCO is deliberately avoided because of known
single-leg cancellation bugs.

**Gap risk captured automatically.** Pre-market gaps past the stop fill
at the open print; the event records actual `fill_price`, so realized R
yields the true outcome (e.g., -1.5R rather than the planned -1.0R).
No special handling required.

**`holding_day` column on decisions.** A new column distinguishes:

- `holding_day = 0`: fresh entry decision; opens a position; feeds
  BucketStats
- `holding_day = 1 / 2 / 3`: position-management decision on an
  already-held position; logged for analytics; does NOT feed BucketStats
  for entry expectancy

**T2 escalation cap per multi-day position.** Default 5 escalations per
position lifetime. After cap, T1 alone decides position-management calls
until the position closes. Prevents one sticky 3-day position from
consuming the daily T2 budget.

**Two-regime BucketStats aggregation.** The bucket-expectancy query
unions:

| Source | Used for | Realized R formula |
|---|---|---|
| `position_trace` | Buckets where orders were placed | Event-weighted sum above |
| `shadow_outcomes` | Hold decisions + rejected-bucket decisions | Simulated first-touch (legacy SQL retained for non-deployed decisions) |

`shadow_outcomes` is extended with `day_1_eod_pct`, `day_2_eod_pct`,
`day_3_eod_pct` to support counterfactual multi-day analysis on
non-deployed decisions.

### Q3. TradePolicy parameter tuning approach

**Original framing.** Thresholds (30 samples minimum, 0.30 expected_r for
max tier, 50 bps spread red flag) are pulled from intuition. After M2
replay we should grid-search these against historical buckets and pick
the values that maximize out-of-sample Sharpe. Until then, document the
hand-picked values and review monthly.

**Resolution.** Two corrections to the original framing, then the
implementation shape:

1. **Tune Calmar, not Sharpe.** Section 0 of this doc defines Calmar as
   the primary objective; the original Q3 framing slipped to Sharpe.
2. **Bayesian optimization via Optuna (TPE sampler), not grid search.**
   The 10-param tuning surface is non-convex and noisy; BayesOpt finds
   good regions in ~200 trials.

**Two-tier parameter classification.**

Tunable policy parameters (10), with bounds:

| Param | Current | Bounds |
|---|---|---|
| `sample_min_for_normal_tier` | 30 | [20, 100] |
| `sample_min_for_max_tier` | 100 | [50, 200] |
| `expected_r_for_max_tier` | 0.30 | [0.20, 1.00] |
| `spread_bps_red_flag` | 50 | [25, 200] |
| `rvol_percentile_red_flag` | 20 | [10, 40] |
| `stop_atr_clamp_choppy` | 2.0 | [1.5, 3.0] |
| `min_reward_to_risk` | 1.5 | [1.2, 2.5] |
| `escalation_confidence_low` | 50 | [40, 60] |
| `escalation_confidence_high` | 75 | [65, 85] |
| `trim_pct_default` | 0.5 | [0.33, 0.67] |

Untunable risk constraints (5) — never touched by the tuner:

| Constraint | Value | Why fixed |
|---|---|---|
| `risk_per_trade_pct` | 1.0% | Account-protection rule |
| `max_drawdown_pct` | 15% | Hard stop |
| `single_day_loss_pct` | 5% | Hard stop |
| `total_exposure_pct` | 90% | Hard constraint |
| `max_holding_days` | 3 | Q2 hard cap |

**Walk-forward validation methodology.**

```
Rolling 60-day train / 15-day validate / 15-day test
Window advances 15 days per fold across available history.
```

Acceptance criteria for a tune:

1. Mean OOS Calmar across folds > current Calmar
2. Variance of OOS Calmar not materially worse (within 1.5x)
3. No parameter pegged at its boundary (boundary hit ⇒ human review)

**Cadence.** Quarterly. Monthly is overfit-prone at our sample volume;
quarterly gives ~300 trades per bucket per tune.

**No-peek discipline.** New thresholds apply to decisions from
activation_date onward; bucket stats they influence are measured from
activation_date forward. Implementation: `policy_tuning_history` table
records activation_date; aggregation queries filter
`WHERE decision_date >= activation_date` when measuring policy-version
performance.

**Output of a tune is a PR, not a live config write.**
`scripts/tune_policy.py` reads from M2 replay, writes to
`config/policy_candidate.yaml`. Human reviews the diff, runs the smoke
verification, commits to `config/policy.yaml`. Trader only reads from
`policy.yaml`. Aligns with Rule 18 (fail loud) and existing deploy gate
culture.

**Bridge plan until M2 replay completes.** Hand-picked values stay.
Logged in `policy_tuning_history` with `method='hand_pick'`. Tuning is
gated on having 60+ days of M2 replay output with `holding_day=0`
decisions across at least 5 buckets, each with `sample_count >= 30`.

### Q4. Policy versioning lifecycle

**Original framing.** When we change a TradePolicy threshold, every
prior decision's recorded `policy_version` becomes ambiguous: was it
produced by the old or new policy? Proposal: bump the policy_version
string and treat it as a backtest cutover boundary.

**Resolution.** SemVer with explicit change-class semantics, plus
hard separation between policy versioning and prompt/schema/code
versioning.

`policy_version` follows `MAJOR.MINOR.PATCH`:

| Bump type | What changed | Comparability of old data |
|---|---|---|
| PATCH (1.2.3 → 1.2.4) | Threshold value(s) only — any of the 10 tunable params | Fully comparable. Bucket stats roll forward unchanged. |
| MINOR (1.2.x → 1.3.0) | New policy param or new bucket dimension added; old logic preserved | Partially comparable. Old decisions null on new dim; backfill or treat as default subgroup. |
| MAJOR (1.x.y → 2.0.0) | Structural change: new realized-R formula, new bucket dimension scheme, new event types in position_trace, new fail-mode hierarchy | Not comparable. Bucket stats restart from bump date. Pre-bump decisions stay queryable for audit; do not feed live BucketStats. |

The first MAJOR bump landing v2 changes is `2.0.0`. Everything before is
`1.x.y` legacy.

**Four version fields per decision row.**

```sql
ALTER TABLE decisions ADD COLUMN policy_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN schema_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN code_sha TEXT NOT NULL;
```

Why separate: prompt changes affect LLM classification of the same
market; policy changes affect how classifications convert to trades;
schema changes affect what fields exist; code_sha pins the exact running
build. Bucket expectancy is mostly a market property (policy-invariant
for PATCH; cross-prompt-version requires soft cutover).

**Enforcement: CI-blocked silent bumps.**

A pre-deploy gate (`scripts/check_version_bumps.py`) blocks commits that
modify `strategy/llm/policy.py` or `config/policy.yaml` without bumping
`POLICY_VERSION`. Same pattern for prompt and schema. Added to
`WAVE_DEPLOY_CHECKLIST`.

**Rollback semantics: rollback is just another version bump.**

Reverting from `2.1.0` to the previous `stop_atr_clamp` value produces a
new `2.1.1`, never reuses `2.0.0`. Recorded as `method='rollback'` in
`policy_tuning_history`. The audit trail is monotonic.

**No-peek validation under versioning.** A tune candidate that proposes
a MINOR or MAJOR change must extend M2 replay for 30 days under the
candidate policy in shadow mode before tuning is accepted. PATCH
candidates may tune against existing data because the bucket population
is unaffected.

**Bucket expectancy filtering.** Default `BucketStats` aggregation
filters `policy_version >= current_major_version`. Pre-MAJOR-bump
decisions become audit-only. PATCH/MINOR include all data, optionally
stratified by version.

### Q5. LLM classification ground-truth

**Original framing.** How do we measure whether the LLM is correctly
classifying catalyst_quality? Options: human spot-check 50 random per
week, Opus cross-check, realized P&L proxy. Initial proposal: do all
three.

**Resolution.** Three-layer protocol, each layer scoped to its
strengths. The original "50 random human reviews per week" is replaced
by a triggered review queue (~10-15/week) targeting only edge cases.

| Layer | Method | Cadence | Volume | Cost |
|---|---|---|---|---|
| 1. Realized P&L proxy | Nightly aggregation from `position_trace`-derived realized R | Continuous | All decisions | $0 |
| 2. Opus cross-check | Random-sample re-classification | Daily | 30/day | ~$60-90/month |
| 3. Human review | Queue fed by anomaly triggers | As-needed | 10-15/week | Operator time on edge cases only |

**Layer 1 — realized P&L is the primary truth signal.**

Computed nightly, post-v2-launch decisions only
(`policy_version >= '2.0.0'`, `holding_day = 0`, last 90 days):

```sql
SELECT 
    catalyst_quality,
    COUNT(*) as sample_count,
    AVG(realized_r) as mean_r,
    STDDEV(realized_r) / SQRT(COUNT(*)) as sem_r
FROM decisions d
JOIN position_trace_realized_r r ON r.decision_id = d.id
WHERE d.holding_day = 0
  AND d.policy_version >= '2.0.0'
  AND d.created_at >= date('now', '-90 days')
GROUP BY catalyst_quality;
```

**Acceptance test.** At `sample_count >= 100` per class, ordering must
be `mean_r(MAJOR) > MATERIAL > MINOR > AMBIGUOUS ≈ NONE` with at least
one pairwise gap statistically significant (95% CI separated).

**Failure mode.** If MAJOR and MINOR mean R are within 1 sigma of each
other at 100+ samples each, halt new deployments to MAJOR-flagged
buckets pending investigation.

**No-leak guard.** Only count realized R from buckets that passed the
deployment gate. Shadow-mode buckets are confounded by policy's
reluctance to size them and bias the proxy downward.

**Layer 2 — Opus cross-check.**

Random sample of 30 T1 classifications per day, re-run through Opus
offline with the **same T1 prompt template** (not the T3 replay-harness
prompt; the comparison is "what would a stronger model say with the same
inputs").

Acceptance: agreement >= 80% on `catalyst_quality`, >= 75% on
`setup_type`. Drift below those bands escalates to investigation.

Caveat: Opus and Haiku share Claude family biases; "both Claudes agreed"
is weaker evidence than "Claude and a human agreed." This is why Layer 3
exists for the residual cases.

**Layer 3 — Human review, triggered queue.**

| Trigger | Volume estimate |
|---|---|
| Haiku-Opus disagreement on catalyst_quality | ~6/day |
| `clamp_anomaly` fires | <1/day in healthy state |
| Realized R outlier (>2 sigma from bucket mean) | ~5/week |
| Rejected-bucket decision with strongly-positive forward returns (missed opportunity) | ~5/week |

Verdict drives action:

- `haiku_wrong` (pattern of 3+ on same setup_type in 14 days) → prompt
  iteration with added worked example
- `opus_wrong` → log; rare in expectation
- `both_correct_market_surprise` → regime-shift evidence; classifier was
  fine, market did something unexpected

**Weekly Classifier Health Report** written to
`journal/classifier_health_YYYY-WNN.md` every Sunday night:

1. Per-class agreement rate trend (90 days, daily)
2. Per-class realized R trend (90 days, weekly)
3. Top 10 human-reviewed cases with verdicts
4. Active alerts (agreement < 75%, realized R separation collapsed, etc.)
5. Bucket halts in effect

**Trigger-to-action mapping.**

| Signal | Threshold | Action |
|---|---|---|
| Agreement rate drift | -10 pp in 30 days | Halt new bucket-deployment promotions; investigate prompt |
| Realized R separation collapse | MAJOR-MINOR CI overlaps zero (n >= 100 each) | Halt MAJOR-bucket deployments; reclassify backlog |
| Human verdicts of "haiku_wrong" | 3+ on same setup_type in 14 days | Schedule prompt review |
| Sustained clamp_anomaly rate | >2% over 100 calls | Already triggers Hold-only per B.3 |

## Supporting schema (added by 2026-05-12 resolutions)

New tables and columns introduced by the Q1-Q5 resolutions. All schema
landed in a single `2.0.0` MAJOR policy_version bump (Q4 semantics).

### New columns on `decisions`

```sql
ALTER TABLE decisions ADD COLUMN holding_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE decisions ADD COLUMN policy_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN schema_version TEXT NOT NULL DEFAULT '0.0.0';
ALTER TABLE decisions ADD COLUMN code_sha TEXT NOT NULL;
ALTER TABLE decisions ADD COLUMN bucket_key_used TEXT;
-- bucket_key_used records the actual (possibly collapsed) bucket the
-- hierarchical lookup landed on; differs from the entry-time bucket_key
-- when fallback fired.
```

### `position_trace` (Q2)

```sql
CREATE TABLE position_trace (
    trace_id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    -- ENTRY | SCALE_UP | TAKE_PARTIAL | TRIM | STOP_RATCHET
    -- | MORNING_BRACKET_REFRESH | CARRY_OVERNIGHT
    -- | STOP_HIT | TARGET_HIT | EXIT_LLM | FLATTEN_1555
    -- | MAX_DURATION_FLATTEN
    qty_delta INTEGER,           -- + for entry/scale_up, - for partial/exit
    fill_price REAL,             -- actual fill; null for marker events
    new_stop_price REAL,         -- only for STOP_RATCHET
    intent TEXT,                 -- "profit_banking" | "defensive" | "thesis_break" | null
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
```

### `shadow_outcomes` extension (Q2)

```sql
ALTER TABLE shadow_outcomes ADD COLUMN day_1_eod_pct REAL;
ALTER TABLE shadow_outcomes ADD COLUMN day_2_eod_pct REAL;
ALTER TABLE shadow_outcomes ADD COLUMN day_3_eod_pct REAL;
```

### `policy_tuning_history` (Q3, Q4)

```sql
CREATE TABLE policy_tuning_history (
    tuning_id INTEGER PRIMARY KEY,
    param_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    set_at TEXT NOT NULL,
    method TEXT NOT NULL,        -- "hand_pick" | "optuna_tune" | "rollback"
    rationale TEXT,
    oos_calmar_train REAL,       -- null for hand_pick / rollback
    oos_calmar_validate REAL,
    oos_calmar_test REAL,
    set_by TEXT NOT NULL,
    policy_version_after TEXT NOT NULL  -- the SemVer this change landed at
);
```

### `opus_crosscheck` (Q5)

```sql
CREATE TABLE opus_crosscheck (
    decision_id INTEGER PRIMARY KEY,
    haiku_catalyst_quality TEXT,
    opus_catalyst_quality TEXT,
    haiku_setup_type TEXT,
    opus_setup_type TEXT,
    haiku_trade_readiness TEXT,
    opus_trade_readiness TEXT,
    agree_catalyst BOOLEAN,
    agree_setup BOOLEAN,
    agree_readiness BOOLEAN,
    checked_at TEXT NOT NULL,
    opus_response_raw TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
```

### `human_review_queue` (Q5)

```sql
CREATE TABLE human_review_queue (
    review_id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    -- "opus_disagree" | "clamp_anomaly" | "realized_r_outlier" | "missed_opportunity"
    enqueued_at TEXT NOT NULL,
    reviewed_at TEXT,
    verdict TEXT,
    -- "haiku_wrong" | "opus_wrong" | "both_correct_market_surprise" | "ambiguous"
    reviewer TEXT,
    notes TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
```

### Updated BucketStats aggregation (Q1, Q2, Q4)

```sql
-- Two-regime UNION: deployed buckets use position_trace event-trace
-- realized R; non-deployed (Hold + rejected) use shadow_outcomes
-- simulated first-touch. Only holding_day=0 feeds entry expectancy.
-- Policy_version filter applies the Q4 cutover semantics.

WITH realized AS (
    -- Deployed: event-trace realized R
    SELECT 
        d.id, d.regime, d.cap_size, d.catalyst_quality,
        d.time_of_day_bucket, d.long_short,
        SUM(p.qty_delta * (p.fill_price - first_entry.fill_price))
            / (first_entry.qty_delta * d.stop_distance_pct * first_entry.fill_price)
            AS realized_r
    FROM decisions d
    JOIN position_trace p ON p.decision_id = d.id
    JOIN position_trace first_entry 
         ON first_entry.decision_id = d.id 
         AND first_entry.event_type = 'ENTRY'
    WHERE d.action != 'Hold'
      AND d.holding_day = 0
      AND d.policy_version >= '2.0.0'
      AND d.created_at > date('now', '-90 days')
    GROUP BY d.id

    UNION ALL

    -- Non-deployed: simulated first-touch from shadow_outcomes
    SELECT 
        d.id, d.regime, d.cap_size, d.catalyst_quality,
        d.time_of_day_bucket, d.long_short,
        CASE
            WHEN s.stop_would_hit AND s.first_touch = 'stop' THEN -1.0
            WHEN s.target_would_hit AND s.first_touch = 'target' THEN
                d.take_profit_atr_multiple / d.stop_loss_atr_multiple
            ELSE s.return_eod_pct / d.stop_loss_pct
        END as realized_r
    FROM decisions d
    JOIN shadow_outcomes s ON s.decision_id = d.id
    WHERE d.action = 'Hold'  -- or rejected by policy
      AND d.holding_day = 0
      AND d.policy_version >= '2.0.0'
      AND d.created_at > date('now', '-90 days')
)
SELECT 
    regime, cap_size, catalyst_quality, time_of_day_bucket, long_short,
    COUNT(*) as sample_count,
    AVG(realized_r) as expected_r,
    AVG(realized_r) - 1.96 * STDDEV(realized_r) / SQRT(COUNT(*)) as expected_r_lower_ci,
    AVG(CASE WHEN realized_r > 0 THEN 1.0 ELSE 0.0 END) as win_rate,
    AVG(CASE WHEN realized_r > 0 THEN realized_r END) as avg_win_r,
    AVG(CASE WHEN realized_r <= 0 THEN realized_r END) as avg_loss_r
FROM realized
GROUP BY regime, cap_size, catalyst_quality, time_of_day_bucket, long_short
HAVING COUNT(*) >= 5;
```

The 32 hierarchical-fallback variants are precomputed nightly as
separate cached tables (one per dimension-subset), each populated by
re-running the above with the relevant `GROUP BY` columns dropped.

## Out of scope (preserved from v1)

- High-frequency execution; cycle remains 5-minute bars
- Options, futures, non-US equities
- Replacement of the gap-and-go rule-based fork (LLM model is parallel,
  not substitute)
- Position management beyond the cadence specified in B.1 (no continuous
  trailing stops, no percentage-based take-profits)
- **Active trading during extended hours** (pre-market 04:00-09:30 ET,
  after-hours 16:00-20:00 ET). Earnings-spike capture is handled via the
  EH-informed RTH path: earnings calendar + Phase B watchlist + Phase C
  PM RVOL feed the LLM context at the 09:30 RTH open with full bracket
  protection. Active EH trading requires software-side stops and is
  deferred to a separate Phase E design.

## In scope, added by 2026-05-12 resolutions

- **Multi-day holds up to 3 trading days** (Q2). Hard cap enforced at
  the 15:55 routine on day 3 (`MAX_DURATION_FLATTEN`). Carry gated by
  bucket expectancy and operational state via
  `policy.allows_carry(bucket_key, position_state)`.
- **EH-informed RTH earnings trading** via Finnhub calendar + Phase B
  watchlist + Phase C PM RVOL extensions (no active EH execution).

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

**Resolutions added 2026-05-12 (sign-off extends to):**

- Hierarchical bucket lookup (Q1): all 5 dims stored, runtime fallback collapses lowest-information dim until sample_count >= threshold; 144 max-granular buckets after default time_of_day and cap_size collapses
- Event-trace realized R via `position_trace` table (Q2); multi-day holds up to 3 trading days with `MAX_DURATION_FLATTEN` hard cap at day-3 close; morning bracket refresh at 09:30; `holding_day` column distinguishes entry from position-management decisions
- Calmar-optimized BayesOpt via Optuna with walk-forward validation (Q3); quarterly cadence; 10 tunable policy params, 5 untunable risk constraints; PR-style human review on every change
- SemVer policy_version with PATCH/MINOR/MAJOR semantics (Q4); four version fields per decision (policy, prompt, schema, code_sha); CI-blocked silent bumps; rollback = new version
- Three-layer classifier ground-truth protocol (Q5): nightly realized R proxy, daily Opus cross-check (30/day), triggered human review queue (10-15/week); weekly Classifier Health Report; explicit halt triggers
- Active EH trading remains out of scope; EH-informed RTH path wires Finnhub earnings calendar into Phase B watchlist and Phase C PM RVOL for the 09:30 RTH open

After sign-off, implementation work begins on Layer 1 (take-profit hotfix)
in parallel with A.2 (shadow analytics). The MAJOR `2.0.0` policy_version
lands when the schema additions above ship together.
