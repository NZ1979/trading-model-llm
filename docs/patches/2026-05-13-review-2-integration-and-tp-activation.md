# 2026-05-13 — ChatGPT review #2 integration + Layer 1 take-profit activation

Single session covering: external review evaluation, regime classifier
(new), Rule 22 hardening (apiKey scrubber), LLMDecision EV-field
additions, position_trace schema verification, and Layer 1 take-profit
activation. 85 unit tests added or covered, all passing on Windows.

## Why

ChatGPT review #2 of the LLM-model architecture (`uploads/ChatGPt.review.2.docx`)
flagged several real gaps and several over-scoped ones. The audit at
`docs/audits/2026-05-13-chatgpt-review-2-evaluation.md` lays out the
per-item verdict; this session executed the high-priority items that
fit the project's current phase, while parking the misfit ones as
explicit non-goals.

The Polygon API-key leak partway through the session (Rule 22 trap:
`httpx.HTTPStatusError` embeds the full URL with the apiKey query param
into its default message) forced an unplanned but necessary hardening
pass on `data/polygon_feed.py` and `scripts/verify_regime.py`. The
leaked key is the shared LLM-model + gap-and-go key; coordinated
rotation is parked as a separate gap-and-go session (task #12).

Phase 1 Task #3 (`position_trace` + decisions schema) turned out to
already be complete — `main.py::init_v2_schema()` lines 177-216 had
been built but `SESSION_RESUME` still listed it as "Not built". One
of the planning-doc updates corrects that.

## What

### New: `analysis/regime.py` + `analysis/regime_data.py` (~430 lines)

Market regime classifier — pure 5-label dispatcher
(`trending_up | trending_down | choppy | crash | unknown`) over three
scalar inputs (SPY 20-day return, VIX/60d-median ratio, breadth proxy).
Hard thresholds, no ML, deliberately rejecting the XGBoost ensemble
from review #2 as over-scoped. Async `fetch_regime_inputs` adapter pulls
SPY + VIX from Polygon. VIX-None fallback degrades to a 4-bucket
classifier (no `crash` reachable). Populates the previously hardcoded
`LLMContext.market_regime_label = "unknown"`.

Empirical Rule-14 gate (`scripts/verify_regime.py`): 267 trading days
labelled, distribution 31% trending_up / 63% choppy / 6% trending_down
/ 0% crash (VIX unavailable on Stocks Starter — followup #13) / 0%
unknown. 90% same-label persistence, no whipsaw, directional regimes
only flip through `choppy`.

Wiring into `LLMContext` construction is deferred to Phase 1 Task #7
(signal_engine.evaluate into main.py) — parked as task #14.

### New: `tests/test_regime.py` (~280 lines, 23 tests)

Pure-classifier unit tests + a 378-input grid sweep asserting the
classifier NEVER emits `"unknown"` on real-shaped inputs.

### Hardened: `data/polygon_feed.py` (+35 lines)

`_scrub_apikey()` helper + URL-scrubbed `RuntimeError` re-raises from
`_get_with_retry`. Closes the Rule-22 leak pattern across every
Polygon-using script in the repo (the verifier surfaced it; the fix
covers all callers). Idempotent against already-redacted strings,
case-insensitive on the `apiKey=` prefix.

### Hardened: `scripts/verify_regime.py` (+25 lines)

Top-level traceback scrubber as defense-in-depth. Even if a future
dependency leaks the URL in a code path we don't currently exercise,
the script's stderr output goes through the scrubber first.

### Modified: `strategy/llm/types.py` — LLMDecision EV fields

Two new fields on `LLMDecision` for the EV scoring that will live in
`policy.py`:

```python
expected_move_pct: float = Field(ge=-20.0, le=20.0, default=0.0)
expected_holding_minutes: int = Field(ge=0, le=390, default=0)
```

Permissive clamp validators mirror the existing pattern. Defaults of
`0.0` / `0` mean "no opinion" — Hold decisions don't need to populate
them, existing test fixtures keep working without modification.
Bounds match the documented intraday horizon: ±20% covers halt-resume
catalysts; 0..390 minutes spans a full RTH session.

The system prompt (`strategy/llm/prompts.py`) gets a new "Forward
predictions" paragraph instructing the LLM to populate these for Buy/Sell.
`prompt_version` bumps `v0.0-stub → v0.1-ev-fields` in
`config/settings.yaml`; the tool input_schema is generated from
`LLMDecision.model_json_schema()` so the new fields flow automatically
to both Anthropic and LM Studio. Interface doc
(`docs/LLM_SIGNAL_INTERFACE.md`) updated with the new JSON schema lines
and field semantics.

### New: `tests/test_llm_decision_ev_fields.py` (~240 lines, 22 tests)

Bounds clamping, defaults, roundtrip, tool-schema generation. Documents
the lax bool/numeric-string coercion the existing validators have
(Pydantic v2 default behavior) — followup #18 tracks a project-wide
audit to tighten the seven affected validators.

### Activated: Layer 1 take-profit

`config/settings.yaml::risk.take_profit_enabled` flipped `false → true`.
Extracted the inline computation from `main.py` into a pure helper
`strategy/risk.py::compute_take_profit_price(side, entry_price,
daily_atr, tp_atr_multiple, enabled) -> float | None`. The helper
returns `None` for both `enabled=False` and `daily_atr <= 0`; the latter
case logs a Rule-18 warning at the caller so warmup-incomplete tickers
are visible in live logs. Main.py caller refactored to call the helper.

`tests/test_take_profit_price.py` (~215 lines, 13 tests) covers the
disabled paths, direction (buy → above, sell → below), magnitude
(offset = tp_atr_multiple × daily_atr exactly to rounding), boundary
multiples (1.0, 5.0), and grid invariants asserting the strict-inequality
property `execution/alpaca_orders.py::submit_bracket_order` enforces
at submit time.

### Already complete (discovered, not built): position_trace + decisions schema

`main.py::init_v2_schema()` lines 177-216 was already running all six
Q2 + Q4 column additions on `decisions`, the three Q2 shadow_outcomes
additions, and creating the `position_trace` table with the exact spec.
Wired idempotently into `Orchestrator._init_db` at line 345. 7 unit
tests in `tests/test_v2_schema.py`. `SESSION_RESUME` previously listed
this as "Not built" — corrected this session.

## Verification

- **Compile gate (Rule 16 + WAVE_DEPLOY_CHECKLIST Gate A):**
  `python -m py_compile` clean for every modified Python file on
  Godzilla's Windows venv (Python 3.14).
- **Unit-test gate:** `python -m pytest tests/test_take_profit_price.py
  tests/test_v2_schema.py tests/test_llm_decision_ev_fields.py
  tests/test_regime.py tests/test_analysis_schema.py -v` → **85 passed,
  23 warnings (return-string convention) in 2.53s** on Windows.
- **Empirical gate for regime classifier (Rule 14):**
  `scripts/verify_regime.py` walked 267 trading days against live
  Polygon data and produced a sane distribution + transition matrix.
- **Rule-22 scrubber:** verified end-to-end against the actual VIX
  403 traceback that the verifier emits in degraded mode — the
  apiKey shows as `<redacted>` in the error message.

## Followups parked

So they don't get rediscovered next session:

| ID | Item | Where it lives |
|----|------|---------------|
| #12 | Migrate gap-and-go VPS to its own Polygon key | Separate gap-and-go laptop session per Rule 26 |
| #13 | VIX data source — VIXY proxy or Polygon plan upgrade | Revisit when policy.py consumes regime |
| #14 | Wire regime classifier into LLMContext build path | Lands with Phase 1 Task #7 |
| #18 | Tighten clamp validators to actively reject bool/string | Project-wide audit; affects 7 validators |

## What's not in this patch (updated end-of-session)

Three of the five ChatGPT review #2 items remain pending. Two of them
sit inside `policy.py` and need the calibrator first; the third lives
in the M2 replay harness:

- Liquidity gate (`policy.py`)
- Cross-sectional ranking (`policy.py`)
- Slippage simulator (M2 replay harness)

A fourth (the confidence calibrator) is blocked on populated
`shadow_outcomes` data, which itself is blocked on the first live LLM
deploy generating decisions. Calibrator skeleton work without data is
make-work; we wait.

## Late addition (same session): policy.py deterministic core

After the patch note's main scope landed, the session pressed on and
built `strategy/llm/policy.py` end-to-end for the deterministic 9-step
decision tree. This was scheduled as ~2.5 days of work; landed in one
session because the dependencies fell into place (analysis.py already
existed, position_trace already migrated, regime + LLMDecision EV
fields landed earlier the same session, take-profit helper pattern
showed how to factor pure logic out of orchestration).

### New: `strategy/llm/policy.py` (~900 lines)

- Four frozen dataclasses (`PolicyInput`, `MarketFeatures`,
  `BucketStats`, `AccountState`, `FinalTradeDecision`) with the
  pinned fields from the design doc § "TradePolicy module spec."
- `PolicyConfig` with the 10 tunable parameters from Q3 (Q3 defaults
  + documented bounds for the eventual Optuna tuner) plus the 5
  untunable risk constraints.
- `decide(input, config) -> FinalTradeDecision` — the public entry
  point. Implements all 9 steps:
    1. Health gate (placeholder `HealthState` Literal until A.3 lands)
    2. LLM `AVOID` hard veto
    3. `clamp_anomaly` gate
    4. Advisory `Hold` pass-through (and step-4b position-management
       translation when `ctx.currently_holding`)
    5. Hierarchical bucket lookup via the public `hierarchical_lookup()`
       helper (Q1 collapse order: time_of_day 5→3, then cap_size 5→2,
       then global prior)
    6. Sample-count tier sizing with negative-CI rejection
    7. Red-flag downgrade (spread / RVOL / composite)
    8. Stop/target clamps (choppy regime widens stop;
       `min_reward_to_risk` raises TP)
    9. Final-decision construction with `bucket_key` + `policy_version`
- Position-management mapping for the 6 `PositionAction` cases
  (`HOLD`, `TRIM`, `EXIT`, `TIGHTEN_STOP`, `SCALE_UP`, `NO_OPINION`,
  plus `TAKE_PARTIAL` and a defensive default for future enum
  additions).
- Rejection-reason strings exposed as stable module-level constants so
  the EOD report can aggregate over them without grep-the-source.
- `policy_version = "0.1.0"` per Q4 SemVer. Bumps to `1.0.0` when the
  three remaining review-#2 items land and M2 replay has validated
  the bucket-stats behavior end-to-end.

### New: `tests/test_policy_foundation.py` (~940 lines, 61 tests)

Foundation (36 tests): every early-Hold gate, every position-management
case, bucket-key derivation, time-of-day boundaries, PolicyConfig Q3
defaults, gate precedence (health beats AVOID beats advisory-Hold),
BucketStats.empty(), policy_version surfaced on every decision.

Steps 5-8 (25 tests): hierarchical_lookup base / time-collapse /
both-collapse / cold-start cases, `_collapse_time_of_day` and
`_collapse_cap_size` mappings, `_tier_from_bucket` across the four
tier outcomes including the max-tier double-condition, red-flag
downgrade ladder (max→normal→tiny→zero), spread / RVOL / composite
triggers, choppy clamp behavior, min-R/R TP-raise (with stop never
tightening), and end-to-end `decide()` paths for negative-bucket
Hold, red-flag-downgrade-to-zero Hold, max-tier happy path, and
choppy-regime clamp.

### Verification

`python -m pytest tests\test_policy_foundation.py tests\test_take_profit_price.py
tests\test_v2_schema.py tests\test_llm_decision_ev_fields.py
tests\test_regime.py tests\test_analysis_schema.py -q` on Godzilla's
Windows venv: **147 passed in 1.16s**. Phase-1 regression suite is
clean.

### What's left in policy.py specifically

- Liquidity gate (cheap; uses MarketFeatures fields directly)
- EV scoring (blocked on calibrator → blocked on shadow data)
- Cross-sectional ranking (operates on a SET of PolicyInputs across
  candidates within a 5-min bar; different shape than the single-
  candidate `decide()`)

## Phase 1 status snapshot at end of session

About 6 of 11 Phase 1 tasks completed or substantially advanced today.
Revised Phase 1 estimate: ~5 days remaining (was 12.5 days at session
start). Critical path to first live LLM paper trade is now:

1. Wire `signal_engine.evaluate` into `main.py` hot path (~0.5 day) —
   the actual gating task
2. A.3 Health state machine (~1 day)
3. M2 replay with slippage simulator (~3.5 days)
4. Phase 5 paper-trading soak

The three remaining review-#2 items can land in parallel with the M2
work; none gate the first live trade.
