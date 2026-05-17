# LLM Trading Model — Strategy Summary & Pre-Deployment Status

**Date:** 2026-05-16 (Sat 20:53 EDT)
**Workstation:** Godzilla
**Repo:** `C:\trading\LLM model` (fork of `trading-platform`)
**Current config:** `llm.enabled: false`, `broker.mode: paper`, account `PA3QAZ941NFN` reserved
**Last commit on origin/main:** `dabc4af`

---

## 1. What the strategy is

A fork of the base `trading-platform` that replaces its rule-based signal engine (`strategy/signals/`) with a three-tier LLM signal generator. Base infrastructure (data feeds, bracket execution, ATR stops, news+sentiment pipeline) is inherited unchanged. To pull base bug fixes: `git fetch upstream && git merge upstream/main`. Conflicts in `strategy/signal_engine.py` or `strategy/signals/` resolve in favor of the fork.

The trading thesis is intraday momentum on 5-minute bars across a dynamic equity watchlist, with bracket orders (entry + ATR stop + target) and tight risk caps. The LLM's job is to fuse technical indicators, premarket setup, daily context, and headline sentiment into a Buy/Sell/Hold call with a confidence score — replacing the brittle if/then rules in the base.

**Instruments:** US equities, paper-only by config. Account `PA3QAZ941NFN` is reserved for the eventual deploy and must not co-exist with the gap-and-go fork on a single `trader.service`.

**Time horizon:** Intraday. 5-min bar evaluation cadence during RTH (09:30–16:00 ET). Flatten at 15:55 ET; EOD journal at 16:30 ET.

**Risk envelope (from `strategy/risk.py validate_order`):** 20% max single-position size, 90% max total deployed capital, 2% max risk per trade (defined by stop distance × shares).

## 2. Architecture

### Tier stack (see `docs/LLM_SIGNAL_INTERFACE.md` for the full contract)

| Tier | Model | Role | Cadence | Status |
|---|---|---|---|---|
| **T1** (hot path) | Qwen 3.6-27B local via LM Studio (target); Haiku stand-in pinned to `claude-haiku-4-5` during bridge | Per-candidate Buy/Sell/Hold + confidence | Every evaluated 5-min bar (~1,742/day in last replay) | Bridge ready; Qwen workstation deploy pending |
| **T2** (selective) | Sonnet 4.5 | Second opinion on marginal T1 calls | ~5–25/day, triggered when T1 confidence ∈ [50, 75] AND premarket RVOL gate fires | Wired; budget consumed on *attempt*, not success |
| **T3** (offline) | Opus | Gold-standard labeling for replay + weekly audit | Backtest only; never live | M2 replay harness uses it |

`signal_engine.evaluate(ctx, clients, budget)` is the live entry point. It must never raise — every failure becomes a synthetic Hold with the failure mode encoded in `setup_label` (`schema_invalid_t1`, `api_failure_t1`, `t1_unexpected`). `merge_tiers(t1, t2)` resolves T1+T2 disagreement by collapsing to Hold. `tier_provenance` on the `LLMDecision` records which tier(s) contributed.

`factory.build_tier_clients(llm_config)` is the only place that maps config strings to client classes. `haiku_stand_in` enforces the model pin so accidental Sonnet bills can't sneak in via the T1 path. `LocalClient` (Qwen) raises `NotImplementedError` until the workstation is online — silent substitution to Anthropic is deliberately avoided.

### Data flow

```
News firehose (Alpaca WS)  → keyword filter → 60s queue → Haiku batch → SQLite sentiment
Equity bars (Alpaca SIP WS) → bar_aggregator (1m→5m)   → on_5min_bar handler
Daily routine (timer)       → Polygon REST              → DailyContext + PremarketContext per symbol

on_5min_bar(bar):
  compute_intraday_indicators(symbol_df)
  → generate_signal(df, daily_ctx, premarket_ctx)   # rule-based today; LLM-replaced when llm.enabled flips
  → latest_sentiment(db, ticker, max_age=86400)
  → evaluate_trade(ticker, sentiment, tech, walls=None)
  → if Buy/Sell: validate_order → submit_bracket_order → log to SQLite
```

Polygon Stocks Starter is 15-min delayed (historical only; never primary intraday). The Databento futures subsystem is dormant in this fork (`futures.enabled: false`); `walls=None` is permanent in the swing-model architecture regardless of subscription status. Finnhub provides earnings calendar. (Note: this doc originally stated "Databento was canceled," which was inherited from project docs but later determined incorrect on 2026-05-17; the subscription was active and was cancelled that day. See `docs/SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md` §10.)

### Logging discipline (Rule 22)

The Anthropic SDK uses `httpx`, which logs full request URLs at INFO by default. Polygon passes the API key as a URL query param. The `setup_logging` block forcing `httpx, httpcore, aiohttp, anthropic, urllib3` to WARNING **must stay in place** — removing it leaks credentials into journalctl on the next deploy.

## 3. What's changed (M3 progress so far)

The current milestone is M3 — replay-driven validation of the LLM signal engine against historical bars.

**Code landed in fork (not in base):**
- `strategy/llm/` module tree: `signal_engine.py`, `policy.py`, `prompts.py`, `clients.py`, `factory.py`, `types.py`, `context_builder.py` (~1,300 lines new)
- `analysis/regime.py` + `analysis/regime_data.py` (regime detection feeds context)
- `data/replay/persistence.py` (replay harness SQLite writer)
- Five new test files (~155 tests added). Suite total: 930 passing as of prior session.

**M3 first replay run (completed):**
- 1,742 T1 calls over 5 trading days, mega-cap universe
- 1 directional trade outcome
- 2 schema_invalid T1 failures
- T1 confidence ceiling observed at 52; 1,427/1,742 calls produced confidence <30

**Diagnosis verdict (per `docs/sessions/2026-05-16-m3-diagnosis.md`):** The engine is functionally working. The sparse-trade outcome is the joint product of (a) deliberately conservative prompt calibration and (b) a quiet 5-day mega-cap window. It is **not** mode collapse.

## 4. Defect inventory — must clear before any meaningful re-run

Full line-numbered detail in `docs/sessions/2026-05-16-m3-handoff.md`. Summary here:

| # | Defect | File | Type | Priority |
|---|---|---|---|---|
| 1 | Reasoning truncated to 280 chars mid-word | `types.py:132, 268-273`; `prompts.py:65` | Mechanical | High |
| 2 | T1 schema_invalid: XML tags leak into strings; stringified JSON in `concerns` | `types.py` (needs two `mode="before"` validators); port logic from `clients.py:337-363` | Mechanical | High |
| 3 | `raw_response` + `risk_check_result` not persisted to replay DB | `persistence.py:425-444` (and 376-395 T3 path) — columns exist, INSERT omits them | Mechanical | High |
| 4 | Error path discards raw input on `ValidationError` | `clients.py:231-234` — capture `tool_block.input` before re-raise | Mechanical | High |
| 5 | **Prompt aggressively biases toward Hold** ("Confidence <40 always Hold; low-conviction trades lose money historically") | `prompts.py:36-43` | **Decision required from Neale** — four options on the table (keep / soften / target rate / hybrid) | High |
| 6 | `setup_label` freeform sprawl → constrain to enum | `types.py:131` | Mechanical | Low |
| 7 | `AnthropicClient` less defensive than `LocalClient` | `clients.py` (back-port from local path) | Mechanical | Low |

**Defect #5 is the only one that requires a conversation, not a typing exercise.** The "low-conviction trades lose money historically" claim cites data this fork does not have — provenance is unclear, possibly inherited from gap-and-go base stats which are a Rule 26 partition violation to reuse. Until #5 is decided, no re-run is worth the API spend.

## 5. Remaining work before deploy

Grouped by what unblocks what. Ordering matters.

### 5a. Fix the engine (this week's chat)

1. Decide #5 with Neale (four options: keep, soften, target-rate, hybrid).
2. Land #1, #2, #4 in one commit. Bump `prompt_version` in `config/settings.yaml` (system prompt changes for reasoning length).
3. Land #3 in a separate commit (different module).
4. Land #5 per decision in a separate commit. Bump `prompt_version` again if system prompt text changes.
5. Run `pytest` after each commit. Target: still 930 passing + new validator tests for #2.

### 5b. Validate end-to-end (next milestone)

6. Re-run M3 replay on a richer universe (not just mega-caps in a quiet window). Target ~3-5× more directional decisions than the first run.
7. M2 replay harness validation: T3 Opus passes on a 3-day sample with the new prompt. Look for T1↔T3 agreement rate and disagreement patterns.
8. Verify `raw_response`, `risk_check_result`, and `LLMContext` payload all land in the DB.

### 5c. Wire into the live loop

9. **Currently the LLM signal engine is NOT wired into `main.py`'s evaluation loop.** `strategy/llm/` exists; `on_5min_bar` still calls the rule-based `generate_signal` dispatcher.
10. Replace the dispatcher call with the LLM signal engine behind the `llm.enabled` flag. Keep the rule-based fallback for `llm.enabled: false`.
11. Add a `policy.py` shim that decides T1-only vs T1+T2 per bar.

### 5d. Workstation prerequisites

12. Qwen 3.6-27B deploy on Godzilla via LM Studio. CUDA + driver verification. `LocalClient` `NotImplementedError` flip.
13. Latency budget: T1 must return inside the 5-min bar boundary minus execution slack. Target <30s p99.
14. Memory + thermal soak (Qwen 27B is not trivial on a single workstation; verify steady-state behavior over a full RTH session).

### 5e. Deploy gates (per `docs/WAVE_DEPLOY_CHECKLIST.md`)

15. Decide deployment target. Options:
    - **(a) Godzilla as primary** — keep model + execution on the workstation. Simplest. Single point of failure.
    - **(b) Separate VPS for execution, Godzilla for T1 inference** — adds network hop; T1 latency budget tightens. Decoupled blast radius.
    - **(c) Separate VPS hosting both** — requires Qwen on a GPU VPS. Costlier.

    The Hetzner VPS at `5.161.199.155` is **not** an option — it runs gap-and-go on account `PA3REQ1LMPKO`. Per Rule 26 the two forks must not co-exist on a single `trader.service`.

16. Code review pass on the full LLM path.
17. Logging audit — verify the Rule 22 suppression block is intact in the final deploy artifact.
18. Credential surface audit — environment file `/etc/trading-platform/env` (mode 0600) lists exactly the required keys, no more.
19. Execution-context labels on every operational command in the deploy doc (Rule 16).
20. Placeholder/assumption audit on the deploy script (Rule 20).
21. Pre-flight: `py_compile` of every changed file *on the deploy host* (not just locally — Rule 24 trap).
22. Atomic cutover: stop old, deploy new, `py_compile` on host, `systemctl restart`, post-restart credential-leak grep on journalctl, 24h soak.

### 5f. Rule 27 hygiene throughout

Every fix session ends with `git push` landing on the LLM-fork remote, verified by a clean `git status` AND "up to date with `<remote>/main`". The session summary must include a literal `Committed and pushed: <SHA>` line. Words like "shipped", "ARMED", "deployed", "done" are off-limits until the push lands.

## 6. Expected success — projected outcomes

The user asked for a number. Honest framing first, then the number.

### What I can and can't reason from

I have not seen a validated backtest. M3 first-run data is too sparse (1 directional trade) to estimate edge. The projection below is **architectural reasoning + comparable-strategy base rates**, not a forecast from the model's own history.

### Comparable base rates

- Discretionary momentum traders, multi-year: 40-55% win rate, profit factor 1.1-1.8.
- Algorithmic intraday momentum strategies: 35-50% win rate, profit factor 1.2-2.0 if edge exists; sub-1.0 if not.
- News/sentiment-augmented event strategies: 45-65% win rate but high variance, often dominated by tail events.
- LLM-as-signal-engine: insufficient published live data to base-rate.

### Projection (12-month paper operation post-deploy)

| Metric | Estimate | Reasoning |
|---|---|---|
| **Win rate on executed trades** | **50-53%** | Conservative calibration filters low-conviction setups. Bracket orders define R:R asymmetry. Comparable strategies cluster here. |
| **Profit factor** | **1.0-1.3** | If sentiment+technical fusion has any edge, it shows up here, not in raw win rate. |
| **Probability of profitable 12-month operation** | **~50%** | Roughly a coin flip. The architecture is sound; whether the edge is real is empirically open. |
| **Probability of beating SPY over 12 months** | **~30-40%** | Intraday strategies typically underperform in trending bull markets due to fee/spread drag. |
| **Probability of NOT exceeding 20% drawdown** | **~75-85%** | Risk caps (2%/20%/90%) and bracket stops bound downside structurally. The likelier failure mode is "did nothing" (over-conservative calibration), not blow-up. |
| **Expected annual return range** | **-5% to +8% vs SPY, median ~breakeven** | Wide because edge is unverified. |

### Specific success number, in one sentence

**Roughly 50% probability of profitable 12-month operation, with median expected return approximately market-neutral vs SPY (range -5% to +8%), and a ~30-40% chance of meaningfully beating the index.**

### What would move this projection

- **Up:** M3 re-run shows T1↔T3 (Haiku vs Opus) agreement rate >70% on directional calls, AND the directional calls show positive expectancy on out-of-sample replay.
- **Down:** T1 confidence distribution stays bimodal at the calibration extremes (most calls at conf<30 or conf>70 with nothing in between) — indicates the model isn't actually differentiating setups.
- **Sideways:** Defect #5 stays at the current "Hold-default" calibration. Strategy becomes "expensive way to hold cash 95% of the time."

### The likeliest failure mode

It is **not** a blow-up. The risk envelope is too tight for that — 2% per trade with ATR stops caps single-trade damage; 90% deployment cap limits portfolio concentration; bracket orders force a stop in the market before the position opens. The likeliest failure mode is **slow underperformance**: the LLM is too conservative, takes few trades, eats friction costs, and finishes the year ~3-7% below SPY. Detectable inside the first 60 trading days from trade count + journal review.

---

## 7. Bottom line

The engine works. The wiring isn't done. The replay-data foundation has known holes. The calibration question is a real decision, not a typo to fix. The risk framework is conservative enough that the realistic downside is *boredom*, not loss.

Deploy is not days away — it is the four blocks in §5 (fix → validate → wire → gate). If the M3 re-run after #1-5 land shows convincing edge, the wire-and-deploy phase is roughly 2-3 focused sessions. If it doesn't, the calibration question reopens and the timeline extends.
