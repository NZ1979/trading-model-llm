# LLM Model — Session Resume Snapshot (2026-05-12)

## How to use this file

Open in a new Cowork session pointed at `C:\trading\LLM model\`. Read top to bottom; total length is intentionally short enough to absorb in ~5 minutes. Authoritative project docs are listed at the bottom. To start work, pick one of the orienting phrases at the end.

## What this project is

A fork of `trading-platform` (added as git remote `upstream`). The base codebase provides shared infrastructure: Alpaca SIP bars, Alpaca News WS, Polygon REST historical, bracket order execution, ATR-based stops, Haiku news sentiment, daily journal. This fork replaces only `strategy/signals/` with a tiered LLM signal generator.

Currently `llm.enabled: false` in `config/settings.yaml`. The inherited rule-based signals (`strategy/signals/gap_and_go.py`, `pullback.py`) run in production. The LLM signal engine code in `strategy/llm/` exists but is not yet wired into `main.py`'s evaluation loop.

## Architecture in one paragraph

Asyncio orchestrator pattern. `main.py` boots a news firehose, an Alpaca SIP bars feed (1m aggregated to 5m), and a daily Polygon routine. Each 5-min RTH bar triggers: indicator computation → signal generation → sentiment lookup → trade evaluation → risk validation → bracket order submission. V1 architecture had the LLM as direct trader. V2 (resolved this session) splits to LLM-as-analyst (classifies catalyst, setup, readiness) plus a deterministic TradePolicy module that consumes the classification, market features, account state, and historical realized P&L per bucket, and produces the final trade decision.

## Production state today

- **VPS:** `5.161.199.155` (Hetzner Ashburn, CPX21). Runs the gap-and-go fork on Alpaca paper account `PA3REQ1LMPKO`. `llm.enabled: false`. Code at `/opt/trader/app`, systemd unit `trader.service`, secrets at `/etc/trading-platform/env`.
- **Large Cap account `PA3QAZ941NFN`** is reserved for when the LLM model deploys. Must not co-exist with the gap-and-go fork on a single `trader.service`. Verify which fork's code runs where before recommending any infra action.
- **Watchlist:** full S&P 500 (503 symbols) for gap-and-go. LLM-specific watchlist TBD.
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

### Phase 1 — V2 architectural foundation (none built yet)

| # | Task | Effort | Notes |
|---|---|---|---|
| 1 | Layer 1 take-profit hotfix | ~0.5 day | Wire `take_profit_limit_price` into `execution/alpaca_orders.py::submit_bracket_order` callers. Ships independently. |
| 2 | A.2 Shadow analytics infrastructure | 1-2 days | `shadow_outcomes` table + day_1/2/3_eod_pct extensions + follower process + initial M2 replay |
| 3 | `position_trace` ledger | 1 day | Event-trace table + decisions schema additions (holding_day, version fields, bucket_key_used) |
| 4 | A.1 Schema split | 2 days | `LLMAnalysis` Pydantic class, prompt template additions, parser updates. Shadow mode for one week before live reliance. |
| 5 | A.3 Health state machine | 1 day | HealthState dataclass, periodic probes, T1-down branch falls back to gap-and-go fork |
| 6 | TradePolicy module + tests | 2 days | Pure-deterministic, fully unit-tested, hierarchical bucket lookup per Q1 |
| 7 | M2 replay with v2 | 3 days | 60-day replay producing per-bucket expectancy report |

Phase 1 estimate: **~10 days focused work**.

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

### Phase 4 — Hardware bridge (runs in parallel)

| # | Task | Status |
|---|---|---|
| 1 | Workstation arrival + Qwen 3.6-27B local inference set up | Awaiting hardware |
| 2 | `LocalClient` implementation (currently raises `NotImplementedError`) | Blocked on hardware |
| 3 | VPS↔workstation network plumbing + fail-closed | Blocked on hardware |

### Phase 5 — Live paper deployment

1. First live paper trade in tiny `qty_tier`, only for buckets that passed the deployment gate (sample_count >= 30 AND lower-CI > 0).
2. 30-day soak with shadow analytics confirming positive Calmar in at least one deployed bucket.
3. Bucket-by-bucket promotion: at sample_count >= 100 AND expected_r_lower_ci > 0.30, bucket eligible for `max` qty_tier.

## Total estimate to first live LLM-driven paper trade

**4-6 weeks of focused work**, depending on workstation arrival and M2 replay findings. If no buckets show positive expectancy after the first 30-day M2 replay, that's itself a finding (the question becomes "is the LLM adding value at all, or does the prompt/model need to change") and the timeline extends.

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

Say one of these to a fresh Cowork session pointed at `C:\trading\LLM model\` after the model finishes reading this file:

- **"Start Phase 1 #1: implement the Layer 1 take-profit hotfix."** Fastest immediate-value change; ships independently of v2 architecture.
- **"Start Phase 1 #2: build the shadow_outcomes follower process."** This is the precondition for every measurement that comes after; nothing else is meaningful without it.
- **"Pick up Task #6: do the EH-informed RTH earnings wiring."** Self-contained 1-2 day feature task using already-installed Finnhub calendar + Phase B watchlist + Phase C PM RVOL infra.
- **"Walk me through the V2 implementation sequencing again."** If the order of Phase 1 work needs fresh framing.
- **"What's the current production state of 5.161.199.155?"** If returning after a few days and wanting to confirm the gap-and-go fork is still healthy before touching anything in the LLM model fork.
- **"Verify signal evaluation worked at market open."** If returning during/after a trading session and wanting to confirm the production gap-and-go fork fired decisions correctly.
