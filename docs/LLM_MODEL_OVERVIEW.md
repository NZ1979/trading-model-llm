# LLM Model — Project Overview

Comprehensive snapshot of the LLM-model fork as of **Wed 2026-05-13 EDT**. This is the LLM-model fork only. The gap-and-go fork (old laptop + Hetzner VPS at `5.161.199.155`, account `PA3REQ1LMPKO`) is operationally separate per Rule 26 and is referenced here only as boundary context.

For non-negotiable engineering rules see `CLAUDE_PREFLIGHT.md` (26 rules) and `CLAUDE.md`. For the design-stage rationale see `docs/LLM_MODEL_CHARTER.md` and `docs/LLM_MODEL_V2_REFINEMENTS.md`. For the contract between platform and tier clients see `docs/LLM_SIGNAL_INTERFACE.md`.

## 1. Session anchors

- **Date verified**: Wed 2026-05-13, ~09:21–10:30 EDT.
- **Working directory**: `C:\trading\LLM model` (the LLM fork; distinct from the upstream base and from `/opt/trader/app/` on the VPS).
- **Workstation**: **Godzilla**. New box. Future host of the local Qwen tier-1 LLM via LM Studio. NOT the Hetzner VPS.
- **Remote**: `https://github.com/NZ1979/trading-model-llm.git`
- **Head**: `854c2ae` (Add LLMAnalysis schema (Q-resolution A.1)).
- **Branch**: `main`, clean tree.

## 2. What this project is

A fork of `trading-platform` that replaces the rule-based signal engine with a **three-tier LLM-driven signal generator**. The base codebase provides shared infrastructure (Alpaca SIP equity bars, news firehose, Polygon historical bars, bracket order execution, ATR-based stops). This fork swaps in:

- A **classification step** that produces an `LLMAnalysis` (catalyst quality, setup type, trade readiness, risks, position action).
- A **deterministic policy** (TradePolicy in `strategy/llm/policy.py`, planned) that consumes the analysis plus market features and account state, and emits the final action.
- A **shadow-mode analytics path** that records forward returns, MAE/MFE, and stop/target touches against every decision so quality is measurable before going live.

The fork inherits the base's rule-based signals (`strategy/signals/gap_and_go.py`, `pullback.py`) which run when `llm.enabled: false` (the current state). The LLM signal generator code lives in `strategy/llm/` and is wired into the orchestrator at the schema-migration layer only; `signal_engine.evaluate` is **not yet** plugged into `main.py`'s evaluation loop.

## 3. Architecture

### Three tiers

| Tier | Role | Backend | Cadence |
|---|---|---|---|
| **Tier 1** | Every candidate, hot path | Qwen 3.6-27B local via LM Studio when Godzilla is configured. **Haiku stand-in** (`backend: haiku_stand_in`, pinned to `claude-haiku-4-5`) during the bridge. | Every 5-min bar |
| **Tier 2** | Selective escalation | Sonnet 4.5 (`claude-sonnet-4-5`) | ~5–25 / day. Triggered when T1 confidence ∈ [50, 75] AND pre-market RVOL ≥ 3.0. Budget consumed on attempt, not success. |
| **Tier 3** | Offline only, never live | Opus (`claude-opus-4-6`) | Replay harness + weekly audit. `enabled: false` in live config. |

Disagreement between T1 and T2 collapses to Hold. Schema-invalid responses fall through to a synthetic Hold with the failure mode encoded in `setup_label` (`schema_invalid_t1`, `api_failure_t1`, `t1_unexpected`). `signal_engine.evaluate(...)` is required to never raise.

`factory.build_tier_clients(llm_config)` is the only place that maps config strings to client classes. The `haiku_stand_in` backend enforces the model pin so an accidental Sonnet bill cannot sneak in via the T1 path. `LocalClient` (Qwen) raises `NotImplementedError` until LM Studio on Godzilla is reachable — silent substitution to Anthropic is deliberately avoided.

### Module layout (LLM-specific)

```
strategy/llm/
├── types.py            # LLMContext (frozen dataclass), LLMDecision (Pydantic, v1 advisory)
├── analysis.py         # LLMAnalysis + 4 enums + LLMOutput wrapper (NEW: 2026-05-13)
├── factory.py          # build_tier_clients — config string -> client class
├── signal_engine.py    # evaluate(ctx, clients, budget); merge_tiers(t1, t2)
├── metrics.py          # compute_outcome + ShadowOutcome (consumed by scripts)
├── prompts.py          # tool-use schema + prompt template
├── policy.py           # TradePolicy — NOT YET BUILT (planned ~2 days)
└── (clients: HaikuStandInClient, AnthropicClient, LocalClient)
```

### Schema split (v2, A.1)

In v1 the LLM emitted a single `LLMDecision` combining classification with action. v2 splits these:

- `LLMAnalysis` — what the LLM observes (catalyst, setup type, trade readiness, invalid_if, primary_concerns, counter_thesis, suggested_horizon, position_action, position_action_reasoning). Pure classification.
- `LLMDecision` — preserved from v1; demoted to *advisory*. The policy may agree, override, or ignore.
- `LLMOutput` — wire wrapper combining both plus the raw JSON for audit.

Defensive truncation on string fields with `max_length` lives in both schemas because Anthropic's tool-use enforces required fields and enum/type constraints but NOT string maxLength.

### Database schema (v2 migration)

Applied at every Orchestrator boot via `init_v2_schema(conn)` in `main.py`. Idempotent.

- `decisions` adds: `holding_day` (INTEGER, default 0), `policy_version`, `prompt_version`, `schema_version`, `code_sha` (all TEXT with neutral defaults), `bucket_key_used` (TEXT, nullable).
- `shadow_outcomes` adds: `day_1_eod_pct`, `day_2_eod_pct`, `day_3_eod_pct` (REAL, nullable) for multi-day hold tracking.
- `position_trace` (new): trace_id PK, decision_id FK NOT NULL, event_time, event_type, qty_delta, fill_price, new_stop_price, intent. Indexed by decision_id.

## 4. Current state

What's **built and tested**:

- v2 schema migration (`init_v2_schema` + two helpers in `main.py`). 7 passing tests in `tests/test_v2_schema.py`.
- `strategy/llm/analysis.py` — full schema split per A.1: 4 enums (CatalystQuality, SetupType, TradeReadiness, PositionAction), LLMAnalysis Pydantic class, LLMOutput wrapper. 20 passing tests in `tests/test_analysis_schema.py`.
- `strategy/llm/types.py` — LLMContext (frozen dataclass with full pre-market / market / fundamentals / intraday / news / position / decision-history / time-of-day fields) and v1 LLMDecision (now the advisory component).
- `strategy/llm/factory.py`, `signal_engine.py`, `metrics.py`, `prompts.py` exist and are individually verified (`scripts/verify_*.py`).
- Backfill/analyze scripts (`scripts/backfill_shadow_outcomes.py`, `analyze_shadow_outcomes.py`) exist but require LLM-model-native shadow data, which doesn't yet exist (see § 8).

What's **explicitly NOT built**:

- `strategy/llm/policy.py` — the deterministic TradePolicy module. Largest remaining Phase 1 task (~2 days).
- The wire-up of `signal_engine.evaluate` into `main.py`'s evaluation loop. Currently the rule-based signals are what fire.
- `LocalClient` against LM Studio — placeholder raises `NotImplementedError` until Godzilla is configured.
- Replay harness (`scripts/replay_with_llm.py`, charter milestone M2).
- M2 replay-based comparison report (charter M4).

What's **explicitly NOT here** (out of scope or scoped to gap-and-go fork only):

- Live trading data, decisions, orders, journals — those live on the gap-and-go VPS, account `PA3REQ1LMPKO`. The LLM-model fork must not consume them per Rule 26.
- Any "baseline R-per-trade" or "X decisions backfilled" numbers — until the LLM model is deployed and generating its own decisions, there is no LLM-model baseline.

## 5. Today's session changes (2026-05-13)

Six commits landed on `main`:

| SHA | Title |
|---|---|
| `833961b` | Add Rule 25: session-anchor verification (date, cwd, Godzilla) |
| `a24e37a` | Add NEXT_SESSION_MENU.md: handoff menu for fresh Cowork sessions |
| `7e349d9` | Add Rule 26: hard partition between LLM-model (Godzilla) and gap-and-go (VPS) |
| `87033d1` | Add v2 schema migration (Q1/Q2/Q4): init_v2_schema, position_trace table, decisions/shadow_outcomes columns |
| `e1a7935` | pytest hygiene: remove return values from tests, set asyncio_mode=auto |
| `854c2ae` | Add LLMAnalysis schema (Q-resolution A.1): enums, Pydantic class, LLMOutput wrapper, 20 tests |

Net result: the partition between the two forks is now codified (Rules 25 + 26), the v2 schema migration is wired into `_init_db` with passing tests, and the LLMAnalysis classification schema is in place ready for the TradePolicy module to consume.

One contamination event surfaced and was reverted during this session: a working-tree change to `config/settings.yaml` flipped `take_profit_enabled` to `true` with a comment citing a 38-decision shadow_outcomes baseline. The baseline could only have come from the gap-and-go VPS (the LLM model has no decisions of its own), which is a Rule 26 violation. The flip was discarded via `git checkout config/settings.yaml`. `take_profit_enabled` remains `false` and the take-profit activation will only be justified by LLM-model-native shadow data once the model is deployed.

## 6. Trading strategy

### Currently running

`llm.enabled: false`. The LLM signal generator does not fire. The inherited rule-based signals would fire if this fork were deployed — but it is not deployed anywhere, so no trades are being placed by this fork.

The rule-based signals it would inherit are:

- **Pullback (mean reversion in trend)** — bull regime + close > SMA20 + RSI < 35 + MACD turning up + close ≥ VWAP. Requires sentiment ≥ +5. Walls confirming-only.
- **Gap-and-go (news-driven momentum)** — 09:35–10:00 ET window only, RVOL ≥ 5×, gap > 1%, price holding gap level. Requires sentiment ≥ +3.

Risk rails: 20% max position, 90% max total exposure, 0.5% portfolio risk per trade via `size_from_risk()`, ATR-based stops (1.5× daily ATR), take-profit currently disabled.

### Target strategy (post-LLM activation)

For each new 5-min RTH bar on the watchlist:

1. `compute_intraday_indicators` and `compute_premarket_context` (unchanged from base).
2. Build an `LLMContext` per ticker — pre-market metrics, market regime, fundamentals, indicators, news (last 24h, capped at 5 items), position state, today's prior decisions for this ticker.
3. Tier 1 emits `LLMOutput` (LLMAnalysis + advisory LLMDecision + raw JSON). Schema-validated; failures fall through to synthetic Hold.
4. Escalation rule checks T1 confidence ∈ [50, 75] AND pre-market RVOL ≥ 3.0. If both true and the daily budget (25 calls) has not been consumed, run Tier 2. Disagreement → Hold.
5. **TradePolicy (planned)** consumes `LLMAnalysis + advisory LLMDecision + MarketFeatures + AccountState` and emits the deterministic final action via a hierarchical bucket lookup (Q1 in `LLM_MODEL_V2_REFINEMENTS.md`).
6. If action is Buy/Sell, the existing risk validator + bracket order submission path runs unchanged.

Held positions get re-evaluated every 5 min with `LLMAnalysis.position_action` ∈ {HOLD, SCALE_UP, TAKE_PARTIAL, TRIM, EXIT, TIGHTEN_STOP, NO_OPINION}. Profit protection has a five-layer stack in the design doc; Layer 1 (take-profit on every bracket) ships first.

### Calmar-anchored objective

Per `LLM_MODEL_V2_REFINEMENTS.md`, the model's primary objective is **maximize 90-day rolling Calmar subject to drawdown and per-trade risk constraints**. Shadow-mode analytics is the precondition for measuring progress against this objective — Calmar is unmeasurable without the `shadow_outcomes` table populated by LLM-model-native decisions.

## 7. Vendor stack (LLM-model fork specific)

| Component | Service | Cost/mo | Notes |
|---|---|---|---|
| Equity broker | Alpaca paper, account `PA3QAZ941NFN` (reserved for LLM-model deploy) | $0 | Distinct from gap-and-go account `PA3REQ1LMPKO` |
| Equity real-time bars | Alpaca SIP (Algo Trader Plus) | $99 | Shared license, but LLM-fork subscribes its own WebSocket on its own deploy |
| Equity historical | Polygon Stocks Starter | $29 | 15-min delayed; used for historical/backfill only. Real-time bars from Alpaca SIP. |
| News firehose | Alpaca News WebSocket (Benzinga) | $0 | |
| Sentiment scoring (v1 path) | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | ~$2–4/day | Prompt caching for headline batches |
| **Tier 1 (LLM signals, primary)** | **Qwen 3.6-27B local on Godzilla via LM Studio** | $0 (hardware amortized) | Not yet operational |
| Tier 1 (bridge) | Haiku stand-in, pinned to `claude-haiku-4-5` | ~$5–15/day at 503-ticker scale | Active default until LM Studio is wired |
| Tier 2 (selective escalation) | Sonnet 4.5 | ~$1–3/day capped (25 calls/day) | |
| Tier 3 (offline audit) | Opus 4.6 | per-job | Replay harness + weekly audit only |
| Compute | Godzilla workstation (LLM model) | hardware | Target host for the LLM-fork runtime. Separate VPS deploy is an open option per the charter. |
| Storage | SQLite | $0 | Path `trading.db`; same schema as base + v2 migrations |

The **Hetzner VPS at `5.161.199.155`** is the gap-and-go fork's host, NOT this fork's. The LLM-model deploy will go to its own infrastructure with its own checklist.

## 8. Hard partition (Rule 26 summary)

From any LLM-model session on Godzilla in `C:\trading\LLM model\`:

- **NEVER** SSH to `5.161.199.155`, scp/rsync against it, or reference paths under `/opt/trader/app/`.
- **NEVER** read, query, copy, or modify `/opt/trader/app/trading.db`.
- **NEVER** commit, push, pull, or fetch against the gap-and-go fork's repository. The LLM-fork's only remote is `https://github.com/NZ1979/trading-model-llm.git`.
- **NEVER** reference gap-and-go operational state (decisions, orders, account balances, journalctl, systemd) as context for LLM-model work. Not even as "baseline" data.
- **NEVER** run an LLM-fork script (anything under `scripts/`, including `backfill_shadow_outcomes.py`, `analyze_shadow_outcomes.py`, `verify_*.py`) against gap-and-go data or codebase.

Tripwire strings — their appearance in an LLM-model session means stop and check: `5.161.199.155`, `/opt/trader/app/`, `hetzner_trader`, `PA3REQ1LMPKO`, `trader.service`, `trader-prod`.

The only legitimate crossing point is the eventual LLM-model deploy to account `PA3QAZ941NFN` on its own infrastructure, with its own scoped session and checklist.

## 9. Remaining tasks before paper trading on Alpaca

Pre-deploy work that has to land in roughly this order. Estimates are calendar days, not engineering hours.

### Engineering — required

1. **Build `strategy/llm/policy.py` (TradePolicy)** — ~2 days. Deterministic module: hierarchical bucket lookup (Q1) + decision logic that consumes `LLMAnalysis + advisory LLMDecision + MarketFeatures + AccountState`. Pure-deterministic, unit-testable in isolation. This is the LLM model's "what to actually do" layer.

2. **Wire `signal_engine.evaluate(...)` into `main.py`'s 5-min bar handler** — ~0.5 day. Replace the `generate_signal(...)` call in `_evaluate_and_execute` with `evaluate(ctx, clients, budget)`, route the resulting `LLMOutput` + policy decision through the existing risk validator + order path.

3. **Make `llm.enabled: true` safe to flip** — ~0.5 day. Set up the LLM client factory, the daily T2 budget reset, the prompt_version invalidation logic, and the failure-mode-to-Hold synthetic logging path. Verify on an isolated DB before flipping in committed config.

4. **Verify Qwen Tier 1 on Godzilla via LM Studio** — ~0.5 day. Run `scripts/verify_qwen_local.py`. Confirm the model is reachable, response shape matches the schema, latency is acceptable. Until this passes, the live default is the Haiku stand-in.

5. **Layer 1 take-profit activation** — ~0.5 day. The `risk.take_profit_enabled` flag flip plus the caller in `main.py` to compute and pass `take_profit_limit_price` per `take_profit_atr_multiple`. Must be gated on LLM-model-native shadow data (see § 5; do not flip on gap-and-go evidence).

6. **EH-informed RTH earnings wiring** — ~1–2 days. Finnhub calendar → watchlist_builder + pm_rvol_thresholds. Independent of the policy/analysis work; can run in parallel.

### Data and validation — required

7. **Generate LLM-model-native shadow_outcomes baseline** — needs the LLM model to be running first (against synthesized fixtures, or a brief paper deploy with `llm.enabled: true` and order submission gated off). Backfill via `scripts/backfill_shadow_outcomes.py`, analyze via `scripts/analyze_shadow_outcomes.py`. Without this, every "is the model improving" claim is unfounded.

8. **Replay harness (charter M2)** — `scripts/replay_with_llm.py`. Takes a historical date + ticker list, reconstructs context, feeds the LLM signal engine, records simulated fills + P/L, outputs a markdown comparison report. Lets us iterate on prompts and policy without burning paper-trading days.

### Operational — required before flipping `broker.mode: paper` against new credentials

9. **Provision the LLM-model deployment target.** Choose Godzilla as runtime vs a fresh VPS (charter M5 mentions a separate instance). Set up venv, systemd, environment file with the LLM-model-only credentials.

10. **Issue LLM-model-only Alpaca paper credentials** against account `PA3QAZ941NFN`. Confirm via `python scripts/verify_alpaca.py` against `paper-api.alpaca.markets`. Set up `/etc/trading-platform/env` (mode 0600) or workstation-equivalent secret storage on the deploy target.

11. **Audit logging defaults before any deploy.** Run the Rule 22 checklist: `httpx`, `httpcore`, `aiohttp`, `anthropic`, `urllib3` loggers pinned to WARNING in `setup_logging`. Polygon passes its API key as a URL query param; URL logging at INFO would leak it into journalctl on the deploy host.

12. **Wave Deploy Checklist** — work through `docs/WAVE_DEPLOY_CHECKLIST.md` for this fork's deploy. Code review → logging audit → credential surface audit → execution-context label audit → placeholder audit → pre-flight → atomic deploy with `py_compile` on the deploy host → post-restart credential-leak grep → 24h soak.

### Decision gates (charter M4)

13. **Replay-based comparison** of LLM strategy vs the rule-based base over 30 trading days. Compute win rate, average win, average loss, Sharpe, and (per v2 priorities) Calmar + drawdown. Decision point: deploy to paper or iterate further.

14. **First clean paper-trading day.** With `llm.enabled: true`, `broker.mode: paper`, the deploy live and the watchlist sized for safe validation. Daily review against `shadow_outcomes` and journal output for the first week.

### Out of scope until after paper trading lands

- Multi-day position management beyond what `holding_day` and `position_trace` already track.
- Layer 4 / Layer 5 of the profit-protection stack (deferred per design doc).
- Phase 7 options walls via Polygon Options Starter.
- Multi-asset support, RL/fine-tuning, ensemble methods.

## 10. How to resume

A fresh Cowork session pointed at `C:\trading\LLM model\` should open with the Rule 25 anchor verification (date, cwd, Godzilla) and then pick one item from `docs/NEXT_SESSION_MENU.md`. The current top-of-list task is:

> **"Build `strategy/llm/policy.py` (TradePolicy module)."**

That's the largest single Phase 1 task remaining (~2 days). After that, `signal_engine.evaluate` can be wired into `main.py` and `llm.enabled` becomes safe to flip in an isolated test environment.

Operational rules of engagement live in `CLAUDE_PREFLIGHT.md`. Read it. Apply Rule 26 to anything that mentions the VPS.
