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

- **"Build `strategy/llm/policy.py` (TradePolicy module)."** The largest single Phase 1 task. Implements Q1's hierarchical bucket lookup + the decision logic. Pure-deterministic, unit-testable. ~2 days.
- **"Build `strategy/llm/analysis.py` (LLMAnalysis schema)."** Q-resolution A.1: Pydantic LLMAnalysis class + LLMOutput wrapper + prompt template additions. ~2 days.
- **"Add `position_trace` table and the schema additions on `decisions`."** Per Q2 + Q4 resolutions: holding_day, four version fields, bucket_key_used. ~1 day.

## Parallel track

- **"Pick up Task #6: EH-informed RTH earnings wiring."** Finnhub calendar -> watchlist_builder + pm_rvol_thresholds. ~1-2 days. Independent of the policy/analysis Phase 1 work, so it can slot in between or around them.

## Re-orienting

- **"Walk me through the v2 implementation sequencing again with current state."** Use if priorities feel stale or a new constraint has appeared (workstation status changed, vendor change, etc.) and the order on this menu needs a fresh pass.

## Suggested order if executing top-to-bottom

1. Shadow backfill (precondition)
2. Qwen verify (smoke check, ~minutes)
3. Take-profit activation (~0.5 day)
4. `position_trace` + schema additions (~1 day, unblocks the next two)
5. `strategy/llm/analysis.py` (~2 days)
6. `strategy/llm/policy.py` (~2 days, uses shadow data + analysis schema)
7. EH-informed RTH earnings wiring (~1-2 days, parallelizable)
