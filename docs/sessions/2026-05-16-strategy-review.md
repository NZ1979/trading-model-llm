# Strategy Review — Response to External Analysis

**Date:** 2026-05-16
**Context:** Reviewing ChatGPT's analysis of the LLM trading model and proposing a revised schedule.

---

## TL;DR

The external analysis is **largely directionally correct** with **three big right ideas**, **three notable wrong ideas**, and **three important things it missed**. The single most valuable insight is the framing of the LLM as an "AI compliance officer, not a trader" and the recommendation to add a quantitative pre-gate. The biggest miss is that calibration metrics it wants require a fix that is already on our list (defect #3) before they are even measurable.

The revised schedule below keeps the M3 fix chat we already planned, then bolts on a measurement phase and a pre-gate phase before the live-wire phase. Net effect: roughly two extra milestones before deploy. Worth the cost.

---

## Section 1 — Validity of ChatGPT's suggestions

### 1a. Where it is right (high confidence)

**The replay outcome diagnosis.** "1 trade in 1,742 calls" is correctly read as prompt suppression, not market quiet. Same diagnosis as our defect #5. ChatGPT's option-A/B/C/D framing maps cleanly onto the four options I gave in the handoff doc. Independent confirmation of the same call is useful.

**LLMs are weak at short-horizon probabilistic prediction.** This is well-supported. LLMs are trained on language modeling, not probability calibration. They are good at ranking, summarization, and contextual synthesis. They are not good at "what is the probability this 5-min bar leads to a 1R move." Asking an LLM "should we trade" is structurally asking for the thing it is worst at. Asking "is this context structurally favorable" plays to strengths.

**Quant trigger → LLM filter is the better architecture.** This is the institutional hybrid pattern. The current fork has the LLM doing *both* candidate discovery and candidate evaluation across every 5-min bar on the watchlist. That overloads the model. A simple quant pre-gate (RVOL, breakout, sector relative strength) narrows the search space before the LLM is asked to judge quality. This is the highest-leverage architectural insight in the entire ChatGPT response.

**Calibration metrics are essential and currently missing.** Brier score, calibration curves, Sharpe by confidence decile, profit factor by regime. These are not optional. Without them you cannot tell if the LLM's confidence score means anything. Right now the project measures trade count and outcomes. It does not measure whether confidence 70 actually outperforms confidence 50, which is the only way to know if the model has any predictive structure. Real gap.

**The "AI compliance officer not a trader" framing.** Good analogy. The current prompt rewards inaction, punishes false positives, and frames uncertainty as dangerous. A trader's job is asymmetric bet selection, not error avoidance. The prompt as written trains the model into the wrong role.

### 1b. Where it overstates or is wrong (these are partial credit at best)

**"Replace Buy/Sell/Hold with expected value scoring."** Directionally right, operationally a 2-3 milestone refactor. The current schema is well-defined and tested. Wholesale change to `edge_score + directional_bias + risk_asymmetry` would touch `types.py`, `prompts.py`, `persistence.py`, the merge logic, the entire risk engine which consumes Buy/Sell/Hold, and the order submission path. That is not "do it after fixing #5." That is its own milestone. Worth doing eventually. Not the immediate next step.

**"Trailing partials, runner positions, volatility-expansion hold logic."** Conflicts with the existing architecture. The system flattens at 15:55 ET and forbids overnight exposure by design. Adding runners means either extending past EOD flatten (violates the principle) or changing what "runner" means (basically the same as the existing target). This is a different strategy, not an improvement to the current one. Park it.

**Ensemble specialized models (news / technicals / regime / volatility).** Premature. You add ensemble complexity *after* you have proven the monolithic architecture has edge and identified specifically where it fails. Adding it now multiplies the surface area to debug without addressing the actual bottleneck.

**The "Revised Potential" numbers (win rate 48-58%, chance profitable 65-75%).** These are reasonable directionally but the specific values are pulled from thin air. There is no empirical basis for "65-75% chance profitable after improvements." My own projection had the same issue. Both projections are architectural reasoning, not forecast. Treat the *direction* (improvement helps) as the takeaway, not the magnitudes.

**Event continuation as the best opportunity.** This is a different strategy entirely. The current architecture is intraday momentum. Pivoting to 1-2 day event-driven means rewriting the time horizon, the data pipeline, the position-holding logic, and the EOD flatten rule. Worth considering as a future fork direction. Not an improvement to this strategy.

**Microstructure as the biggest missing edge source.** Speculative. Even if Databento were re-added, the current execution architecture (Alpaca paper, 5-min bar evaluation cadence) cannot capitalize on sweep behavior or order-flow imbalance at the right time scale. Microstructure edge requires sub-second execution. Adding the data without restructuring execution would be expensive and useless.

### 1c. What ChatGPT missed (these matter)

**Cost economics.** 1,742 T1 calls per day at Haiku rates is real money over a year. T2 escalations on Sonnet add up. The current architecture's cost structure deserves a line on the dashboard. "Scan more aggressively" is free advice; it has a price tag.

**Qwen local deployment changes the economics entirely.** Once Qwen 27B is running on Godzilla via LM Studio, T1 inference is essentially free compute. That makes "scan every bar" or "evaluate more candidates" feasible in a way it is not with paid API. ChatGPT's recommendation to add a quant pre-gate is still correct (it reduces *noise* not just *cost*), but the cost urgency drops post-Qwen.

**Defect #3 is a prerequisite for the metrics it wants.** Calibration curves require persisted `raw_response` and `risk_check_result`. Both are currently dropped by the replay persistence layer (defect #3 in the handoff). ChatGPT writes as if calibration analysis is a tool we already have. We do not. We have the data being generated, we have the columns in the schema, but the INSERT statement omits them. Fix #3 unlocks every measurement ChatGPT recommends.

**The live loop is not wired yet.** Per `CLAUDE.md`: "the LLM signal engine code in `strategy/llm/` exists but is not yet wired into `main.py`'s evaluation loop." Most of ChatGPT's recommendations apply to the replay/research path, which is fine, but the analysis reads as if the LLM is making live decisions today. It is not. We have `llm.enabled: false` and the dispatcher still calls rule-based signals.

**The base upstream already had rule-based signals.** The fork *replaced* `gap_and_go.py` and `pullback.py` with the LLM. ChatGPT's "add a quant trigger before the LLM" is essentially "reinstate the upstream signals as a pre-gate, then have the LLM filter their candidates." That is a coherent design but it is a partial rollback of the fork's founding decision. Worth doing. Worth being explicit about.

---

## Section 2 — Updated schedule

### Phase 1 — M3 engine fixes (next session, already scoped)

Unchanged from the existing handoff doc. Defects #1-4 mechanical, #5 decision + edit, then a clean re-run of M3 on a richer universe. Treat this as the unblocking work. **Estimated effort:** 1-2 focused sessions.

**Adjustment from ChatGPT input:** Defect #5 prompt edit should incorporate the "expectancy, not certainty" framing. Specifically replace the "Confidence below 40 always Hold; low-conviction trades lose money historically" block with something like:

> *Confidence reflects estimated edge quality, not certainty. Moderate-confidence trades are acceptable when the reward-to-risk asymmetry is favorable. The system is evaluated on long-run expectancy, not prediction accuracy.*

This is the option-(b)/option-(c) hybrid from the handoff doc, sharpened. Still requires Neale's explicit go-ahead before editing.

### Phase 2 — Measurement infrastructure (new, was implicit)

Once defect #3 lands and a richer M3 re-run produces hundreds of decisions, build the measurement layer that ChatGPT correctly identifies as missing. **Estimated effort:** 1 focused session.

Specifically:

1. `scripts/analyze_calibration.py` — calibration curve (predicted confidence bucket vs realized win rate), Brier score, expected calibration error.
2. `scripts/analyze_expectancy.py` — Sharpe by confidence decile, profit factor by `setup_label`, expectancy per setup category, hit rate by regime.
3. `scripts/analyze_decision_volume.py` — confidence distribution, Buy/Sell/Hold ratio, percent of bars actually evaluated vs filtered out.

Output of Phase 2 is a single markdown report per replay run. This is what tells us whether the LLM is contributing predictive structure or just acting as expensive noise.

**Gate:** Before Phase 3, the Phase 2 report on the post-fix M3 re-run must show *something* — either confidence has structure (higher confidence buckets show higher win rate) or it does not. If it does not, the calibration question reopens before any more engineering work happens.

### Phase 3 — Quant pre-gate (new, ChatGPT's best contribution)

Build a deliberately simple quantitative trigger layer that decides *which* 5-min bars get LLM evaluation. The LLM no longer scans every bar. **Estimated effort:** 1-2 focused sessions.

Suggested trigger set, all of which are cheap to compute:

1. **Premarket RVOL gate** (already exists in `pm_rvol_thresholds.py`) — but actually wire it into the bar-evaluation path, not just T2 escalation.
2. **5-min breakout** — current bar takes out the prior N-bar high (or low for shorts) by some ATR fraction.
3. **Relative strength vs SPY** — rolling 30-min beta-adjusted return is positive (for longs) or negative (for shorts).
4. **Volume acceleration** — current bar volume > 1.5× the 20-bar average.
5. **Spread acceptable** — bid/ask spread within configured threshold.

A bar passes the gate if (premarket RVOL fires) AND (breakout OR relative-strength) AND (volume accel) AND (spread OK). Roughly: "something has actually changed, the stock is leading the market, and execution is clean enough to matter."

Bars that don't pass the gate skip the LLM entirely and log a `pregate_filtered` setup label. This gives us a measurable comparison: what fraction of trades the LLM would have taken anyway after a pre-gate filter, and whether the pre-gate's selections have better expectancy than the LLM's unfiltered scanning.

**Why this is high-leverage:** It addresses the architectural problem that the LLM is overloaded (candidate discovery + evaluation + risk in one prompt). It is additive, not replacing. It can be A/B tested against the unfiltered path in replay before committing to it.

### Phase 4 — Wire LLM engine into live loop (was Phase 3 in old plan)

Same as the original plan. Replace the rule-based dispatcher call in `on_5min_bar` with the LLM signal engine behind the `llm.enabled` flag, keeping rule-based as fallback. **Now with the Phase 3 pre-gate sitting in front of the LLM call.** **Estimated effort:** 1 focused session.

### Phase 5 — Qwen workstation deploy + paper validation

Same as original plan. Add a 30-day paper-forward window with full Phase 2 metrics running daily. **Estimated effort:** Multi-week elapsed; light per-session active effort.

Key checks during this phase:

1. T1 latency p99 inside the 5-min bar boundary minus execution slack (target <30s).
2. Memory + thermal soak on Godzilla through a full RTH session.
3. Calibration metrics from Phase 2 trending in the right direction over the 30 days.
4. Trade count in a sensible range, not 1 per week.

### Phase 6 — Edge decision + deploy gate

After 30 days of paper, run the Phase 2 metrics one final time and make a binary call:

**Positive edge:** Calibration shows structure, Sharpe by decile is monotonic, expectancy is positive after assumed friction. Proceed to `docs/WAVE_DEPLOY_CHECKLIST.md` gates. Deploy target decision (Godzilla primary, separate VPS, or hybrid) finalized here.

**No edge:** Calibration is flat, confidence does not predict outcome, expectancy is negative. Open the pivot conversation:
- Time horizon (5-min → 30-min or hourly bars where LLM strengths better fit).
- Calibration retraining layer (Platt scaling or isotonic regression on raw LLM confidence).
- Universe change.
- Strategy pivot (only as a last resort, and only with a Rule 26 partition kept clean).

---

## Section 3 — What I am explicitly not recommending and why

**Ensemble specialized models.** Premature. The monolithic architecture has not been shown to fail in a way that ensemble would fix. Adding it now is complexity without diagnosis.

**Trailing partials / runner positions.** Conflicts with the EOD flatten architecture. Either rewrite the architecture (not now) or skip this.

**Pivot to event-continuation strategy.** Different strategy. Park as a possible future fork direction. Do not lift the current architecture toward it; that is the worst of both worlds.

**Wholesale schema change to `edge_score + directional_bias + risk_asymmetry`.** Worth doing eventually. Too disruptive for the current milestone. Buy/Sell/Hold + confidence is enough resolution for Phase 2 metrics to tell us if there is edge. If edge exists, then justify the refactor.

**Re-adding Databento for microstructure.** Execution architecture cannot capitalize on it at the current cadence. Out of scope.

---

## Section 4 — Sequencing summary

| Phase | What | Effort | Unblocks |
|---|---|---|---|
| 1 | M3 engine fixes (#1-5) | 1-2 sessions | Phase 2 |
| 2 | Measurement infrastructure | 1 session | Phase 3 gate decision |
| 3 | Quant pre-gate | 1-2 sessions | Phase 4 |
| 4 | Wire into live loop | 1 session | Phase 5 |
| 5 | Qwen + 30-day paper | Multi-week elapsed | Phase 6 |
| 6 | Edge decision + deploy gate | 1-2 sessions | Live deploy or pivot |

Total: roughly 5-8 focused sessions of active engineering + a 30-day paper window, assuming each phase clears its gate cleanly. Add ~50% for slippage. Realistic timeline to a live-capable system: 2-3 months elapsed.

---

## Section 5 — Honest revised projection

Given the proposed schedule actually lands as written (which is a meaningful "if"), my revised projection:

| Metric | Original projection | Revised |
|---|---|---|
| Win rate on executed trades | 50-53% | 49-55% |
| Profit factor | 1.0-1.3 | 1.1-1.5 |
| Chance of profitable 12-month operation | ~50% | ~55-65% |
| Chance of beating SPY over 12 months | ~30-40% | ~35-50% |
| Chance of NOT exceeding 20% drawdown | 75-85% | 75-85% (risk envelope unchanged) |

These are still architectural reasoning, not data-backed forecasts. The honest version: the proposed improvements move the median outcome from "roughly market-neutral with high uncertainty" to "modestly positive with somewhat lower uncertainty." The biggest single move comes from Phase 2 (measurement) because it is the first thing that turns guesses into measurements.

The unanswered question that no architecture can resolve in advance: does the market inefficiency this strategy targets actually exist after spreads, slippage, and noise? Only Phase 5 paper data can answer that. Everything before Phase 5 is preparation to ask the question cleanly.
