# Next Session Menu

Pick one of these and paste it as the first prompt to a fresh Cowork session pointed at `C:\trading\LLM model\`. Each is sized small enough to finish (or land a clean checkpoint) in a single session.

## Recommended first: shadow_outcomes backfill

**"Run the shadow_outcomes backfill against current decisions."**

```powershell
python scripts/backfill_shadow_outcomes.py
python scripts/analyze_shadow_outcomes.py
```

Seeds the analytics table from existing v1 decisions. Explicit precondition for measuring anything else, including validating the TradePolicy work in Phase 1. No code changes, two scripts, fresh data at the end. Do this before the larger Phase 1 modules so they can be checked against real shadow numbers.

## Quick wins (half-day)

- **"Activate Layer 1 take-profit."** Flip `risk.take_profit_enabled` to true and wire the caller in `main.py` to compute and pass `take_profit_limit_price`. Code path already in place. ~0.5 day.
- **"Verify Qwen Tier 1 is still serving correctly."** Runs `python scripts/verify_qwen_local.py` and reports. Smoke check before any LLM-touching work.

## Phase 1 build tasks

- **"Build `strategy/llm/policy.py` (TradePolicy module)."** The largest single Phase 1 task. Implements Q1's hierarchical bucket lookup + decision logic + liquidity gate + cross-sectional ranking + EV scoring (review #2 integration). Pure-deterministic, unit-testable. ~2.5 days.
- **"Build `strategy/llm/analysis.py` (LLMAnalysis schema)."** Q-resolution A.1: Pydantic LLMAnalysis class + LLMOutput wrapper + prompt template additions. ~2 days.
- **"Add `position_trace` table and the schema additions on `decisions`."** Per Q2 + Q4 resolutions: holding_day, four version fields, bucket_key_used. ~1 day.
- **"Build `analysis/regime.py` (regime classifier)."** New deterministic 3-bucket classifier (SPY return + VIX + breadth). Populates `LLMContext.market_regime_label`. ~1 day. Review #2 integration. Self-contained, no dependencies.
- **"Build `analysis/calibration.py` (confidence calibrator)."** Isotonic regression on shadow_outcomes mapping (T1 confidence, setup, regime) → calibrated win rate. Reliability diagram + ECE reporting. ~2 days. **Blocked on populated shadow_outcomes + regime classifier.** Review #2 integration.
- **"Add `expected_move_pct` and `expected_holding_minutes` to `LLMDecision`."** Schema + prompt + interface doc updates; bump `prompt_version`. ~0.5 day. Review #2 integration; required by EV scoring in policy.py.

## Parallel track

- **"Pick up Task #6: EH-informed RTH earnings wiring."** Finnhub calendar -> watchlist_builder + pm_rvol_thresholds. ~1-2 days. Independent of the policy/analysis Phase 1 work, so it can slot in between or around them.

## Re-orienting

- **"Walk me through the v2 implementation sequencing again with current state."** Use if priorities feel stale or a new constraint has appeared (workstation status changed, vendor change, etc.) and the order on this menu needs a fresh pass.

## Suggested order if executing top-to-bottom

1. Shadow backfill (precondition; seeds data the calibrator needs)
2. Qwen verify (smoke check, ~minutes)
3. Regime classifier `analysis/regime.py` (~1 day, self-contained, unblocks calibrator + populates `LLMContext`)
4. Take-profit activation (~0.5 day)
5. `position_trace` + schema additions (~1 day, unblocks analysis.py + policy.py)
6. LLMDecision schema additions: `expected_move_pct` + `expected_holding_minutes` (~0.5 day, before policy.py)
7. `strategy/llm/analysis.py` (~2 days)
8. `strategy/llm/policy.py` with liquidity gate + ranking + EV scoring (~2.5 days)
9. Wire `signal_engine.evaluate` into `main.py` (~0.5 day)
10. M2 replay with slippage simulator (~3.5 days)
11. `analysis/calibration.py` (~2 days, runs against M2 output + live shadow rows)
12. EH-informed RTH earnings wiring (~1-2 days, parallelizable anywhere)
