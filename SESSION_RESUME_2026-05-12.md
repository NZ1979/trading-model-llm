# LLM Model — Session Resume Snapshot (2026-05-12)

## How to use this file

Open in a new Cowork session pointed at `C:\trading\LLM model\`. Read top to bottom; total length is intentionally short enough to absorb in ~5 minutes. Authoritative project docs are listed at the bottom. To start work, pick one of the orienting phrases at the end.

## What this project is

A fork of `trading-platform` (added as git remote `upstream`). The base codebase provides shared infrastructure: Alpaca SIP bars, Alpaca News WS, Polygon REST historical, bracket order execution, ATR-based stops, Haiku news sentiment, daily journal. This fork replaces only `strategy/signals/` with a tiered LLM signal generator.

Currently `llm.enabled: false` in `config/settings.yaml`. The inherited rule-based signals (`strategy/signals/gap_and_go.py`, `pullback.py`) run in production. The LLM signal engine code in `strategy/llm/` exists but is not yet wired into `main.py`'s evaluation loop.

## Architecture in one paragraph

Asyncio orchestrator pattern. `main.py` boots a news firehose, an Alpaca SIP bars feed (1m aggregated to 5m), and a daily Polygon routine. Each 5-min RTH bar triggers: indicator computation → signal generation → sentiment lookup → trade evaluation → risk validation → bracket order submission. V1 architecture had the LLM as direct trader. V2 (resolved this session) splits to LLM-as-analyst (classifies catalyst, setup, readiness) plus a deterministic TradePolicy module that consumes the classification, market features, account state, and historical realized P&L per bucket, and produces the final trade decision.

## Production state today

### VPS (running)

- **`5.161.199.155`** (Hetzner Ashburn, CPX21). Runs the **gap-and-go fork** (rule-based, llm.enabled: false) on Alpaca paper account `PA3REQ1LMPKO`. Code at `/opt/trader/app`, systemd unit `trader.service`, secrets at `/etc/trading-platform/env`.
- **Large Cap account `PA3QAZ941NFN`** reserved for the LLM model deployment. Must not co-exist with the gap-and-go fork on a single `trader.service`.

### Workstation (operational since ~2026-05-11 evening MDT)

- **Puget Z890 C132-XL.** Intel Core Ultra 7 270K Plus 24-core, **RTX PRO 5000 Blackwell 48GB VRAM**, 192GB DDR5-4800 RAM, 6TB NVMe Gen4 across 3 drives, Win11 Pro. Full specs in `docs/HARDWARE_PLATFORM.md`.
- **LM Studio installed and operational.** Qwen 3.6-27B Instruct loaded.
- **Qwen 3.6-27B benchmarked 2026-05-12:** 43.9 and 48.2 tok/s on two contexts at single-stream, thinking-disabled tool-use. Single 250-token call ≈ 5.5s; 500-token call ≈ 11s. Real throughput is lower than the pre-arrival estimate of 120-180 tok/s but well within the 300s cycle budget for batched candidate evaluation.
- **`LocalClient` (LM Studio OpenAI-compat) fully implemented** in `strategy/llm/clients.py`. Includes Qwen 3.6-specific handling: `/no_think` injection to suppress thinking-mode (which otherwise causes empty tool_calls with finish_reason=stop), XML tool-call fallback parser for Qwen's native `<function=submit_decision>...</function>` format, temperature=0 for determinism.
- **`qwen_local` backend wired** in `strategy/llm/factory.py`. Construction is cheap; failures surface at evaluate-time per Rule 18.
- **Verify + smoke scripts** in place: `scripts/verify_qwen_local.py` (server + model loaded + PONG roundtrip), `scripts/smoke_test_qwen_decision.py` (end-to-end LLMDecision generation).

### Watchlist + data feeds

- **Watchlist:** full S&P 500 (503 symbols) for gap-and-go on VPS. LLM-specific watchlist TBD when LLM model goes live.
- **Active data feeds:** Alpaca SIP bars, Alpaca News, Polygon REST historical, Finnhub earnings calendar.
- **Dormant:** Databento (canceled), Polygon options walls (deferred).

## Recent design decisions (2026-05-12 session)

Resolved the five open design questions in `docs/LLM_MODEL_V2_REFINEMENTS.md`. The doc now contains the full resolution for each; brief summaries:

- **Q1: Bucket dimension count.** Hierarchical lookup at decision time; all 5 dims stored. Default collapse order: time_of_day (5→3) then cap_size (4→2). Max-granular bucket count drops from 480 to 144.
- **Q2: Realized R calculation.** Event-trace ledger in a new `position_trace` table. Multi-day holds allowed up to 3 trading days with `MAX_DURATION_FLATTEN` at day-3 close (never day-4 open). Morning bracket refresh at 09:30. `holding_day` column distinguishes entry from position-management decisions. Gap risk captured automatically via actual `fill_price`.
- **Q3: TradePolicy tuning.** Calmar-optimized Bayesian optimization via Optuna (not grid search, not Sharpe). Quarterly cadence, walk-forward validation, hard parameter bounds, PR-style human review. 10 tunable policy params separated from 5 untunable risk constraints.
- **Q4: Policy versioning.** SemVer with PATCH/MINOR/MAJOR semantics. Four version fields per decision: policy_version, prompt_version, schema_version, code_sha. CI-blocked silent bumps. Rollback = new version.
- **Q5: Classifier ground-truth.** Three-layer protocol: nightly realized R proxy (primary), daily Opus cross-check (30/day), triggered human review queue (~10-15/week from anomaly triggers, not random sampling). Weekly Classifier Health Report. Explicit halt triggers wired to bucket-deployment gates.

Plus added **Rule 24** to `CLAUDE_PREFLIGHT.md` and `CLAUDE.md`: Cowork bash mount can serve stale snapshots of Windows-side files. Verification of disk state runs from PowerShell on the workstation, never from the bash sandbox.

Commits landed on `origin/main` today:
- `568845c` — Q1-Q5 resolutions in `docs/LLM_MODEL_V2_REFINEMENTS.md`
- `45122ff` — Rule 24 in `CLAUDE_PREFLIGHT.md`
- `f017e04` — Rule 24 in `CLAUDE.md` Hard Rules section + count bump 23→24

## What's left before live trading

### Phase 1 — V2 architectural foundation

| # | Task | Status | Effort | Notes |
|---|---|---|---|---|
| 1 | Layer 1 take-profit | **Code wired, feature flag OFF** | flip + verify | `execution/alpaca_orders.py::submit_bracket_order` accepts `take_profit_limit_price`. Callers don't pass it yet. To activate: set `risk.take_profit_enabled: true` in `config/settings.yaml` AND update the caller in `main.py` (or wherever bracket orders are submitted) to compute and pass the TP price. |
| 2 | A.2 Shadow analytics | **Schema exists, not populated** | 1 day to first populated run | `shadow_outcomes` table created at boot in `main.py`. `scripts/backfill_shadow_outcomes.py` + `scripts/analyze_shadow_outcomes.py` exist. Run the backfill against the existing decisions to seed initial rows. Then extend with day_1/2/3_eod_pct columns per Q2 resolution. |
| 3 | `position_trace` ledger (Q2) | **DONE** (verified 2026-05-13) | n/a | `main.py::init_v2_schema()` lines 177-216 runs all Q2 + Q4 column additions on `decisions` and `shadow_outcomes`, creates `position_trace` with the exact spec'd schema, indexed on `decision_id`, idempotent. Wired at boot in `Orchestrator._init_db` line 345. 7 unit tests in `tests/test_v2_schema.py`. SESSION_RESUME's prior "Not built" status was stale. |
| 4 | A.1 Schema split (`strategy/llm/analysis.py`) | Not built | 2 days | `LLMAnalysis` Pydantic class, prompt template additions, parser updates. Shadow mode for one week before live reliance. |
| 5 | A.3 Health state machine | Not built | 1 day | `HealthState` dataclass, periodic probes, T1-down branch falls back to gap-and-go fork |
| 6 | TradePolicy module (`strategy/llm/policy.py`) | **DETERMINISTIC CORE DONE** (2026-05-13) | n/a for core | Foundation (4 dataclasses, PolicyConfig with Q3 defaults, early-Hold gates, position-management mapping) + steps 5-8 (hierarchical bucket lookup, sample-count tier sizing, red-flag downgrade, choppy-regime / min-R/R clamps). 61 unit tests passing on Windows. **Still pending in policy.py:** liquidity gate (~0.5 day), EV scoring (~0.5 day, depends on calibrator which is blocked on shadow data), cross-sectional ranking (~1 day). |
| 7 | Wire `signal_engine.evaluate` into `main.py` hot path | **PART A DONE (builders)**; Part B (main.py call site) pending | 0.5 day for Part B | Part A (2026-05-13): `strategy/llm/context_builder.py` (~420 lines, 36 tests) with `build_llm_context`, `build_market_features`, `build_account_state`, `synthesize_default_analysis`. Pure functions, no I/O, fully unit-tested. Part B: add schema migration for `llm_shadow_decisions` table, init TierClients + EscalationBudget at boot when `llm.enabled`, new method `_evaluate_llm_shadow()` that calls the builders + signal_engine.evaluate + policy.decide, log result; call from `_evaluate_and_execute`. Shadow mode initially (no execution) — rule-based path stays in charge. The builders are pre-tested so the hot-path edit can stay surgical (~30 lines). |
| 8 | M2 replay with v2 | Not started | 3.5 days | 60-day replay producing per-bucket expectancy report. **+0.5 day for slippage simulator** (entry: worse of bar open + half-spread / first-30s VWAP; stop: stop minus 0.5 × ATR/share, capped at next bar low; MOC: close − 5 bps). Configurable under `replay.slippage_model:` in `config/settings.yaml`. |
| 9 | **NEW:** Regime classifier (`analysis/regime.py`) | Not built | 1 day | 3-bucket deterministic classifier (`risk_on_momentum` / `chop` / `risk_off`) over SPY 20-day return + VIX vs 60-day median + breadth (% of SP500 above 50-day SMA). Hard thresholds, no ML. Populates `LLMContext.market_regime_label` which is currently always `"unknown"`. Verify via 1-year backtest distribution sanity check. |
| 10 | **NEW:** Confidence calibrator (`analysis/calibration.py`) | Not built | 2 days | Isotonic regression mapping (T1 confidence, setup_label, regime_label) → realized win rate, fit on shadow_outcomes. Refit weekly. Fallback to identity when sample size in a (setup, regime) cell < 30. Reliability diagram + ECE in `analyze_shadow_outcomes.py`. **Blocked on Tasks #2 (populated shadow data) and #9 (regime labels).** |
| 11 | **NEW:** LLMDecision schema additions | Not built | 0.5 day | Add `expected_move_pct` (-20..20) and `expected_holding_minutes` (0..390) to `LLMDecision`. Update prompt template + `LLM_SIGNAL_INTERFACE.md`. Bump `prompt_version`. Required by Task #6 EV scoring. |

Phase 1 revised estimate: **~10 days focused work** (was 12.5 days as of 2026-05-13 morning; -1 day because Task #3 already-complete + Task #9 regime classifier and Task #11 LLMDecision schema additions landed during the 2026-05-13 session, leaving ~10 days for the rest of Phase 1).

**2026-05-13 session deliveries (deduct from Phase 1 timeline):**

- Task #3 (`position_trace` + decisions schema): discovered already complete, verified via test_v2_schema.py
- Task #9 (regime classifier `analysis/regime.py` + `analysis/regime_data.py`): built, 23 unit tests passing, empirically verified on 267 trading days against real Polygon data
- Task #11 (LLMDecision schema additions): `expected_move_pct` + `expected_holding_minutes` added to LLMDecision, prompt template + system prompt updated, `prompt_version` bumped `v0.0-stub → v0.1-ev-fields`, 22 unit tests passing
- Task #1 (Layer 1 take-profit): activated via config flag flip + extracted `compute_take_profit_price` helper into `strategy/risk.py`; 13 unit tests passing including grid invariants
- Task #6 (TradePolicy deterministic core): foundation + steps 5-8 landed in `strategy/llm/policy.py` (~900 lines), 61 tests passing covering every gate, position-management case, bucket-fallback walk, tier-sizing branch, red-flag downgrade ladder, and clamp scenario
- Task #7 Part A (signal_engine wiring builders): `strategy/llm/context_builder.py` (~420 lines) + 36 tests. Pure builders for LLMContext / MarketFeatures / AccountState / synthesized LLMAnalysis. The main.py call site (Part B) is the next session's task and is small (~30 lines surgical edit) because the builders are pre-tested.
- Rule 22 hardening: `data/polygon_feed.py::_scrub_apikey` + `verify_regime.py` top-level traceback scrubber. Closes the apiKey-in-URL leak pattern across all Polygon-using scripts.
- Followups parked (do not re-discover): VPS Polygon key migration (separate gap-and-go session), VIX data source (Stocks Starter 403s on `I:VIX`), wiring regime classifier into LLMContext build path (lands with Task #7), validator strictness audit (bool/numeric-string coerce instead of reject).

**Total Phase 1 progress today: ~7 of 11 tasks completed or substantially advanced (Task #7 is half done — builders landed; main.py call site queued). Revised Phase 1 estimate: ~4.5 days remaining work** (was 12.5 this morning). Critical path to first live LLM paper trade is now Task #7 Part B (main.py call site, ~0.5 day) + Task #5 (A.3 Health state machine, ~1 day) + Task #8 (M2 replay with slippage, ~3.5 days) + Phase 5 soak. Plus one bigger followup (task #29: LLM tier emits LLMOutput; ~1-2 days; not on critical path because shadow mode works with synthesized bridge analysis).

### Phase 2 — Quality layers (Tier B)

| # | Task | Effort |
|---|---|---|
| 1 | B.1 Layer 2: trailing stop ratchet | 1-2 days |
| 2 | B.1 Layer 3: LLM position-management evaluator | 2 days |
| 3 | B.1 Layer 4: late-day exit bias modifier | 1 day |
| 4 | Multi-day holds: CARRY_OVERNIGHT gate + MORNING_BRACKET_REFRESH + MAX_DURATION_FLATTEN + T2 escalation cap | 2-3 days |
| 5 | B.2 Multi-condition escalation | 1 day |
| 6 | B.3 Clamp observability + 5% Hold-only threshold | 0.5 day |
| 7 | B.4 Regime-stratified deployment gates | 1 day |
| 8 | EH-informed RTH earnings wiring (current Task #6) | 1-2 days |

Phase 2 estimate: **~10-12 days**.

### Phase 3 — Hardening (Tier C)

| # | Task | Effort |
|---|---|---|
| 1 | C.1 Determinism pinning (temperature=0, top_p=1, top_k=1, seed pinned) | 1 day |
| 2 | C.2 Survivorship-bias snapshots (daily IWM holdings) | 0.5 day |
| 3 | C.3 Reason field expansion (280→500) + dedicated `invalid_if` 200-char field | 0.5 day |
| 4 | C.4 Network-split protection (workstation /health, stale-decision treatment) | 1 day |
| 5 | CI version-bump enforcement (`scripts/check_version_bumps.py`) | 0.5 day |
| 6 | `scripts/tune_policy.py` with Optuna walk-forward | 2-3 days |
| 7 | Weekly Classifier Health Report generator | 1 day |
| 8 | `WAVE_DEPLOY_CHECKLIST.md` updated for v2 schema migration | 0.5 day |

Phase 3 estimate: **~7-10 days**.

### Phase 4 — Hardware bridge (DONE except trader-to-workstation network plumbing)

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | Workstation arrival + Qwen 3.6-27B local inference set up | **COMPLETE** (2026-05-11/12) | Puget Z890 on-site; LM Studio + Qwen 3.6-27B benchmarked at 43-48 tok/s |
| 2 | `LocalClient` implementation | **COMPLETE** (commit `b0d6c95` on 2026-05-11) | `strategy/llm/clients.py::LocalClient` with Qwen 3.6 quirks handled; `qwen_local` backend wired in factory.py |
| 3 | Qwen verify + smoke scripts | **COMPLETE** | `scripts/verify_qwen_local.py`, `scripts/smoke_test_qwen_decision.py` |
| 4 | Flip `t1.backend` from `haiku_stand_in` to `qwen_local` in `config/settings.yaml` | Not done | Pending Phase 1 completion; needs `llm.enabled: true` and `signal_engine.evaluate` wired into `main.py` first |
| 5 | VPS↔workstation network plumbing (heartbeat + decision shipping if trader stays on VPS) | Not built | See Phase 3 C.4. Only needed if running Option A from `HARDWARE_PLATFORM.md` (trader on VPS, LLM on workstation). Skip if trader moves to workstation (Option B). |

### Phase 5 — Live paper deployment

1. First live paper trade in tiny `qty_tier`, only for buckets that passed the deployment gate (sample_count >= 30 AND lower-CI > 0).
2. 30-day soak with shadow analytics confirming positive Calmar in at least one deployed bucket.
3. Bucket-by-bucket promotion: at sample_count >= 100 AND expected_r_lower_ci > 0.30, bucket eligible for `max` qty_tier.

## Total estimate to first live LLM-driven paper trade

**4-6 weeks of focused work** from 2026-05-12, depending on M2 replay findings. Workstation+Qwen no longer on the critical path (operational since Mon 05/11). If no buckets show positive expectancy after the first 30-day M2 replay, that's itself a finding (the question becomes "is the LLM adding value at all, or does the prompt/model need to change") and the timeline extends.

## Open implementation tasks (carried from this session)

| Task ID | Subject | Status |
|---|---|---|
| #6 | EH-informed RTH earnings wiring (Finnhub calendar → watchlist_builder + pm_rvol_thresholds) | Pending, ~1-2 days |

All five design questions resolved and committed. Only implementation task #6 carries over.

## Hard rules to apply in every session (from `CLAUDE_PREFLIGHT.md`)

24 numbered rules total. Most operationally critical, quoted verbatim in `CLAUDE.md` Hard Rules section:

- Rule 14: Verification before conclusion (HYPOTHESIS / UNVERIFIED labels until tested)
- Rule 16: State execution context explicitly before every command block
- Rule 18: Fail loud, never fake (no silent fallbacks)
- Rule 20: Audit output for placeholders and unstated assumptions
- Rule 21: Never request command output that would expose credentials
- Rule 22: Audit logging behavior for credential leaks (`httpx` URL logging trap)
- Rule 23: Verify actual system date/time before any time-anchored claim
- Rule 24: Cowork bash mount can serve stale snapshots of Windows-side files; verify from PowerShell on host, never trust bash mount for "did the edit reach disk"

## Authoritative references (read these to deepen context)

- `CLAUDE.md` — repo-root project guidance, Hard Rules section
- `CLAUDE_PREFLIGHT.md` — 24 numbered rules from prior failures
- `PROJECT_BLUEPRINT.md` — deployment state, vendor stack, daily timeline, do-not-re-debate facts
- `docs/LLM_MODEL_CHARTER.md` — what makes this fork different from base
- `docs/LLM_SIGNAL_INTERFACE.md` — v1 contract (output schema, prompt template, tier orchestration)
- `docs/LLM_MODEL_V2_REFINEMENTS.md` — v2 design contract, includes all Q1-Q5 resolutions from 2026-05-12 plus supporting schema for the new tables
- `docs/M2_REPLAY_HARNESS_DESIGN.md` — replay design
- `docs/HARDWARE_PLATFORM.md` — workstation context
- `docs/WAVE_DEPLOY_CHECKLIST.md` — gated deploy procedure

## Orienting phrases to start a new session

Say one of these to a fresh Cowork session pointed at `C:\trading\LLM model\`:

- **"Activate Layer 1 take-profit."** Flip `risk.take_profit_enabled` to true and wire the caller in `main.py` to compute and pass `take_profit_limit_price`. Code path already in place. ~0.5 day.
- **"Run the shadow_outcomes backfill against current decisions."** `python scripts/backfill_shadow_outcomes.py` then `python scripts/analyze_shadow_outcomes.py`. Seeds the analytics table from existing v1 decisions; precondition for measuring anything.
- **"Build `strategy/llm/policy.py` (TradePolicy module)."** The largest single Phase 1 task. Implements Q1's hierarchical bucket lookup + the decision logic, plus liquidity gate + cross-sectional ranking + EV scoring (review #2 integration). Pure-deterministic, unit-testable. ~2.5 days.
- **"Build `strategy/llm/analysis.py` (LLMAnalysis schema)."** Q-resolution A.1: Pydantic LLMAnalysis class + LLMOutput wrapper + prompt template additions. ~2 days.
- **"Add `position_trace` table and the schema additions on `decisions`."** Per Q2 + Q4 resolutions: holding_day, four version fields, bucket_key_used. ~1 day.
- **"Build `analysis/regime.py` (regime classifier)."** New deterministic 3-bucket classifier (SPY return + VIX + breadth). Populates `LLMContext.market_regime_label`. ~1 day. Review #2 integration.
- **"Build `analysis/calibration.py` (confidence calibrator)."** Isotonic regression on shadow_outcomes mapping (T1 confidence, setup, regime) → calibrated win rate. ~2 days. **Blocked on populated shadow_outcomes + regime classifier.** Review #2 integration.
- **"Add `expected_move_pct` and `expected_holding_minutes` to `LLMDecision`."** Schema + prompt + interface doc updates; bump `prompt_version`. ~0.5 day. Review #2 integration; required by EV scoring in policy.py.
- **"Verify Qwen Tier 1 is still serving correctly."** Runs `python scripts/verify_qwen_local.py` and reports.
- **"Pick up Task #6: EH-informed RTH earnings wiring."** Finnhub calendar → watchlist_builder + pm_rvol_thresholds. ~1-2 days.
- **"Walk me through the v2 implementation sequencing again with current state."** If the priority order needs fresh framing given today's snapshot.
- **"What's running on the VPS right now?"** Status check on the gap-and-go fork at `5.161.199.155`.
