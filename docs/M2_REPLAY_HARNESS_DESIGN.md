# M2 — Replay Harness Design

The replay harness is the tool we use to evaluate the LLM strategy
without spending money on live paper trading. Given historical market
data + news from a past trading day, it reconstructs the LLMContext at
each evaluation tick, calls the LLM signal generator, simulates fills
based on the bar data we have, and outputs a comparison report against
the base codebase's rule-based decisions over the same period.

This is the most important infrastructure piece of the whole project.
If replay is broken or biased, every downstream conclusion is wrong.

## Purpose

Four questions we want answered:

1. **Does the LLM strategy make different decisions than the base?** If LLM agrees with base 100% of the time, there's no point. If LLM differs sharply, that's where the value (or risk) lives.

2. **Are the LLM's different decisions better?** Measured by: win rate, average win/loss size, P&L per dollar at risk, max drawdown over the replay window.

3. **What does the LLM see that the rules miss, and vice versa?** Qualitative review of decisions where LLM and base disagree — does the LLM catch setups the rules miss? Does it avoid setups the rules over-fire on?

4. **How well does Tier 1 (Qwen local) agree with Tier 3 (Opus gold-standard labels)?** Per `LLM_SIGNAL_INTERFACE.md` § Tiered evaluation, Opus runs alongside Qwen on every replay candidate as a reference labeler. Qwen-vs-Opus agreement rate, divergence patterns, and the realized P&L on each path tell us where Qwen's domain-reasoning gaps actually cost money. This drives prompt iteration and the threshold tuning for when Tier 2 escalation should fire in live operation.

The harness must answer these without running on live trader-prod, so we
can iterate on prompt + signal logic safely.

## Design principles

1. **Point-in-time correctness is non-negotiable.** At any (ticker, timestamp), the harness must build LLMContext using only data available AT that timestamp. Using future bars, future news, or future sentiment scores invalidates the replay. We enforce this with explicit time windows on every data fetch.

2. **Same code path as live, just different inputs.** The replay harness builds an LLMContext and passes it to the same LLM signal generator that production would use. We do not have a separate "replay-only" implementation of the signal logic — that would be a different strategy and we'd be measuring the wrong thing.

3. **Deterministic results given the same inputs.** Temperature=0 + LLM response caching means re-running the same replay produces identical decisions and P&L. We can iterate on the comparison report format without re-paying for LLM calls.

4. **Fail loud on missing data.** If we don't have news for a date, or bars for a ticker, the replay errors out rather than silently using empty data. Empty data → bad LLM decisions → meaningless report.

5. **Side-by-side comparison built in, not bolted on.** Every replay run computes both LLM decisions AND base rule-based decisions in parallel, on the same context. The output report includes both and highlights divergences.

## Architecture

```
Inputs                         Replay Harness                  Outputs
------                         --------------                  -------

date_range  ----+
                |
ticker_list ----+----> load_historical_bars() (Polygon)
                |       load_historical_news() (Polygon)
                |       load_historical_sentiment() (production DB)
                |       load_market_data()      (SPY, VIX bars)
                |              |
                |              v
                +----> for each (date, time_tick):
                          for each ticker in pre_filter(tickers):
                              ctx = build_LLMContext(
                                  ticker, time_tick,
                                  bars_up_to_now,
                                  news_up_to_now,
                                  sentiment_up_to_now,
                                  position_state,
                                  decision_history,
                              )
                              # Tier 1 (always): Qwen local, the live primary
                              t1_decision = qwen_signal(ctx) ----> cache.get(qwen_hash)
                                                                    or qwen_call()
                              # Tier 2 (selective): Sonnet escalation,
                              # only when the live escalation rule fires
                              t2_decision = None
                              if escalation_rule(ctx, t1_decision):
                                  t2_decision = sonnet_signal(ctx) -> cache.get(sonnet_hash)
                                                                       or sonnet_call()
                              # merge per LLM_SIGNAL_INTERFACE.md
                              live_decision = merge_tiers(t1_decision, t2_decision)
                              # Tier 3 (always): Opus gold-standard label,
                              # offline path, never affects live_decision
                              t3_decision = opus_signal(ctx)  ---> cache.get(opus_hash)
                                                                    or opus_call()
                              # Base rules still computed in parallel
                              base_decision = base_signal(ctx_to_base_inputs(ctx))
                              record(date, time, ticker,
                                     t1_decision, t2_decision, t3_decision,
                                     live_decision, base_decision)
                              if live_decision.action != Hold:
                                  simulate_fill(llm_portfolio, live_decision)
                              if base_decision.action != Hold:
                                  simulate_fill(base_portfolio, base_decision)
                          mark_to_market(both portfolios)
                      end of day: simulate_flatten(both portfolios)
                       |
                       v
                  generate_comparison_report() ---> Markdown report
                                              ---> SQLite replay DB
                                              ---> CSV decision log
```

## Inputs

### Required at run time

```python
@dataclass(frozen=True, slots=True)
class ReplayConfig:
    start_date: date                       # inclusive
    end_date: date                         # inclusive
    tickers: list[str] | str               # explicit list, or "watchlist" to use today's
    llm_prompt_version: str                # "v1.0", "v1.1", etc.

    # Tiered backend selection (mirrors live architecture)
    t1_backend: str = "qwen_local"         # "qwen_local" | "llama_local" | "haiku" (cloud only for ablation)
    t1_model_id: str = "qwen3.6-27b-instruct-q4"
    t2_enabled: bool = True                # Tier 2 escalation
    t2_model_id: str = "claude-sonnet-4-5"
    t2_max_per_day: int = 25               # daily escalation budget cap
    t3_enabled: bool = True                # Tier 3 Opus labeling
    t3_model_id: str = "claude-opus-4-6"
    t3_sample_rate: float = 1.0            # 1.0 = label every candidate; 0.1 = sample 10% to control cost

    # Pre-filter parameters (cheap rule-based candidate selection)
    pre_filter_min_pm_rvol: float = 2.0
    pre_filter_min_gap_pct: float = 1.0
    pre_filter_news_lookback_hours: int = 2

    # Simulation parameters
    starting_cash: float = 100_000.0       # both portfolios start equal
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 20.0
    slippage_bps: float = 5.0              # 0.05% slippage on fills
    fill_at: str = "next_bar_open"         # "current_close" | "next_bar_open"

    # Output
    output_dir: Path = Path("docs/reports")
    cache_dir: Path = Path(".replay_cache")
```

### Required data

| Source | What | Backed by |
|---|---|---|
| Polygon REST | 1-minute bars per ticker per date | already in base codebase |
| Polygon REST | Daily bars per ticker (300 days back from start_date) | already in base codebase |
| Polygon News API | Historical news per ticker per date | already in base codebase (`data/polygon_news.py`) |
| trader-prod DB | Historical sentiment scores | the production `sentiment` table on the VPS |
| Polygon REST | SPY daily + 5-min bars | new helper needed |
| Polygon REST | VIX latest reading | new helper needed; can also use `^VIX` symbol |
| Static file | Sector / market_cap_bucket per ticker | new helper; can lookup once and cache |

### News-data caveats

Polygon News timestamps are **publication time**, not "available to traders" time. Most news flows through within seconds of publication, but some sources have lag. For replay purposes, we trust the publication timestamp and consider news "available to the LLM" 30 seconds after publication (small buffer for ingestion latency). This is a known approximation; document it in every report.

### Sentiment-data caveats

The production `sentiment` table is the canonical source for "what sentiment score was available at time T." We do **not** re-score historical headlines — that would change the score depending on Claude version drift. The harness queries the production DB read-only.

## Replay loop

### Time discretization

Live evaluation fires every 5 minutes (driven by 5-min bar emission). Replay does the same: for each trading day, evaluation ticks at:

```
09:30, 09:35, 09:40, ..., 15:55  (78 ticks per day)
```

The 09:30 tick has only the pre-market context (no RTH bars yet). 09:35
onward has 1+ RTH bars and is when the live system actually starts
firing signals. Replay matches this exactly.

### Pre-filter

Before LLM evaluation, candidates are narrowed using the rules from
LLM_SIGNAL_INTERFACE.md:

```python
def pre_filter(tickers, time_tick, ctx_data) -> list[str]:
    candidates = []
    for ticker in tickers:
        if ctx_data[ticker].pm_rvol >= config.pre_filter_min_pm_rvol:
            candidates.append(ticker)
            continue
        if abs(ctx_data[ticker].gap_pct) >= config.pre_filter_min_gap_pct:
            candidates.append(ticker)
            continue
        if has_recent_news(ticker, time_tick, config.pre_filter_news_lookback_hours):
            candidates.append(ticker)
            continue
        if currently_holding[ticker]:
            candidates.append(ticker)
            continue
    return candidates[:config.max_candidates_per_tick]   # default 30
```

The base codebase's signal engine evaluates ALL watchlist tickers every
tick. To make the comparison fair, we run base evaluation on every
ticker too — only the LLM evaluation is gated by the pre-filter. This
means we'll see cases where base fires on a ticker the pre-filter
rejected; those count as "miss for LLM, hit for base" in the report.

### Simulation: fills and slippage

When `fill_at="next_bar_open"` (recommended default):
- Buy/Sell decision at tick T executes at tick T+1's open price + slippage
- Stop-loss order is placed immediately after; checked against each subsequent bar's low (for long) or high (for short)
- If stop is hit during a bar, fill at stop price (no further slippage; already a market order to close)

When `fill_at="current_close"`:
- Buy/Sell decision at tick T executes at tick T's close + slippage
- Bias toward optimistic fills; useful for "best case" replay

### Simulated bracket order

Each simulated fill records:
```python
@dataclass
class SimulatedFill:
    ticker: str
    side: str
    qty: int
    fill_price: float
    fill_timestamp: datetime
    stop_price: float
    decision_id: int             # links back to decision record
```

Stop is monitored each subsequent bar. End-of-day simulated flatten
closes any remaining positions at the final bar's close.

### Position cap and risk validation

The harness applies the same risk module as live:
- `validate_order` from `strategy.risk` (existing module)
- Position cap, total exposure cap, ATR-based stop sizing
- Same code path that live uses

If risk module rejects a decision, that's recorded as an outcome
("decision: Buy, risk_rejected: position_cap_exceeded"). Both LLM and
base portfolios use this; it's not the place to differ.

## LLM caching

To control cost during iteration, the harness caches every LLM
response keyed by `(prompt_hash, backend, model_id, prompt_version)`.
Each tier has its own cache namespace so we can re-run a replay with
just one tier modified (e.g. a new Qwen prompt) without re-paying for
the other tiers.

```python
def cached_llm_call(prompt: str, backend: str, model_id: str,
                    prompt_version: str) -> dict:
    key = sha256(f"{prompt_version}|{prompt}".encode()).hexdigest()
    cache_path = config.cache_dir / backend / model_id / f"{key}.json"
    if cache_path.exists():
        return json.load(open(cache_path))
    response = call_backend(backend, model_id, prompt)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(response, open(cache_path, "w"))
    return response
```

First run of a replay window: full cost on whichever tiers are
enabled. Re-runs of the same replay against the same prompts: zero
LLM cost. Re-runs with a bumped prompt_version: only that tier
re-pays; the other tiers' caches still hit.

Cache layout:
```
.replay_cache/
    qwen_local/
        qwen3.6-27b-instruct-q4/
            <prompt_v1.0_sha>.json
    anthropic/
        claude-sonnet-4-5/
            <prompt_v1.0_sha>.json
        claude-opus-4-6/
            <prompt_v1.0_sha>.json
```

The prompt_version included in the cache key ensures v1.0 and v1.1
caches don't collide. When iterating a prompt, only the affected
tier's cache invalidates; the other two tiers' caches remain valid.

For Tier 1 (Qwen local), "cost" is wall-clock time, not dollars —
caching matters because re-running a 60-day replay against fresh
LLM calls on a 50-tok/s model takes hours. With cache hits, the
same replay finishes in minutes.

## Storage

### `.replay_cache/<model>/<sha>.json`

Raw LLM responses, keyed by prompt hash. This is the cache file.

### `replay_results.db` (SQLite)

Aggregated decision and fill records. Schema:

```sql
CREATE TABLE replay_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    config_json TEXT NOT NULL,         -- full ReplayConfig dump
    completed_at TEXT,
    summary_json TEXT                   -- aggregated metrics
);

CREATE TABLE replay_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    decision_source TEXT NOT NULL,     -- "live_merged" | "base" | "t1_only" | "t2_only" | "t3_only"
    -- For decision_source = "live_merged": this is the merged Tier1+Tier2 result
    -- that drives the simulated portfolio. The per-tier rows below capture each
    -- tier's individual output for analysis even when not chosen.
    action TEXT NOT NULL,               -- Buy/Sell/Hold
    setup_label TEXT,
    confidence INTEGER,
    reasoning TEXT,
    raw_response TEXT,                  -- JSON for LLM tier, debug str for base
    risk_check_result TEXT,             -- "approved" or rejection reason
    tier_provenance TEXT,               -- "t1_only" | "t1_t2_agree" | "t1_t2_disagree" | NULL for non-merged rows
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id)
);

-- Each (timestamp, ticker) pair produces up to 5 rows in replay_decisions:
--   1. decision_source="t1_only"      : Tier 1 raw output (always present when ticker passes pre-filter)
--   2. decision_source="t2_only"      : Tier 2 raw output (present only when escalation rule fired)
--   3. decision_source="t3_only"      : Tier 3 Opus gold-standard label (present per t3_sample_rate)
--   4. decision_source="live_merged"  : the merged decision used by the simulated portfolio
--   5. decision_source="base"         : the base codebase rules-based decision (the comparison baseline)
--
-- This shape lets us answer: how often does T2 reverse T1? how often does T3 disagree
-- with T1? when T1 and T3 disagree, which one would have made money? without re-running
-- any LLM calls.

CREATE TABLE replay_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    fill_timestamp TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    exit_timestamp TEXT,                -- NULL if still open at end-of-window
    exit_price REAL,
    exit_reason TEXT,                   -- stop_hit, eod_flatten, take_profit
    realized_pl REAL,
    FOREIGN KEY (decision_id) REFERENCES replay_decisions(id)
);
```

### `docs/reports/replay_<date>_<config_hash>.md`

The human-readable comparison report.

## Comparison report format

Markdown sections:

### 1. Run metadata
- Date range
- Ticker count, candidate count after pre-filter
- Prompt version
- Tier configuration: T1 backend + model_id, T2 enabled/model_id, T3 enabled/sample_rate/model_id
- Calls per tier: T1 (cache hits / misses), T2 (cache hits / misses), T3 (cache hits / misses)
- Cost per tier in dollars (T1: ~$0; T2: $X.XX; T3: $XX.XX)
- Total wall-clock time for the run, broken down by tier (Qwen local time vs Anthropic API time)

### 2. Decision summary
- T1 (Qwen) decision count by action and setup_label
- T2 (Sonnet, when fired) decision count by action and setup_label
- T3 (Opus gold standard) decision count by action and setup_label
- live_merged decision count (the one driving the simulated portfolio)
- Base decision count by action and setup
- Side-by-side counts in a table (one column per source)

### 3. Portfolio performance
- Starting cash, ending equity for each portfolio
- Total realized P&L
- Total trades, winners, losers
- Win rate
- Average win, average loss
- Largest single win, largest single loss
- Max drawdown over the replay window
- Sharpe ratio (annualized; rough, since replay window is short)

### 4. Divergence analysis
For each (ticker, timestamp) where LLM and base produced different actions:
- Tickers where LLM said Buy and base said Hold (LLM "extra" trades)
- Tickers where base said Buy and LLM said Hold (LLM "missed" trades)
- Tickers where LLM said Sell and base said Buy (opposite)
- Outcome of each in a table: which one was right (i.e. which trade would have been profitable)

### 5. LLM-specific quality metrics
- Confidence distribution (histogram of confidence scores)
- Confidence vs. realized P&L (calibration check)
- Setup label frequency
- Reasoning length distribution

### 5b. Regime-stratified performance (added 2026-05-07 per RESEARCH_NOTES.md)

For each regime label seen during the replay window
(`trending_up`, `trending_down`, `choppy`, `crash`), report:
- Decision count by action × source (LLM, base)
- Win rate by source
- Average P&L per trade by source
- Maximum drawdown by source

This catches regime-specific behavior that the overall metrics in
section 3 hide. If LLM win rate is 60% in trending regimes but 30% in
choppy regimes (vs base's flat 45% across both), the right move may
not be "deploy LLM everywhere" but "deploy LLM in trending regimes,
keep base in choppy." Research literature on regime-aware ML strongly
supports this pattern.

The replay must include at least one identifiable "choppy" or "crash"
period to exercise this analysis. If the replay window is all
trending_up, regime stratification reduces to the section 3 metrics.

### 5c. Crash-period replay (added 2026-05-07 per RESEARCH_NOTES.md)

A specific high-stress test: identify a known volatile/crash window
in the replay-eligible date range (look for days where SPY
`abs(change_pct) > 1.5%` or VIX > 25, ideally consecutive). Run the
harness on just that window with a separate run_id. Compare:

- Did the LLM avoid large losses (the "regime-aware ML in crashes" claim)?
- Did the LLM concentrate trades in safer setups?
- Did either strategy hit max drawdown limits we'd consider unacceptable?

A strategy that performs decently in normal markets but blows up in
crashes is not deployable. Crash-period replay surfaces this before
live trading does.

### 5d. Tier agreement & escalation analysis (added 2026-05-08)

This section quantifies the value of the tiered architecture per
`LLM_SIGNAL_INTERFACE.md` § Tiered evaluation.

**Tier 1 vs Tier 3 (Qwen vs Opus gold-standard) agreement:**
- Overall agreement rate (% of candidates where T1 and T3 chose the same action)
- Confusion matrix: T1 action × T3 action (3×3 grid of Buy/Sell/Hold)
- Of T1-T3 disagreements: how many turned out profitable for the T1 side vs the T3 side (using simulated fills)
- Disagreement-by-confidence-band: do T1's high-confidence decisions agree with T3 more than its low-confidence ones? (Proxy for confidence calibration.)

**Tier 2 escalation behavior:**
- How many cycles fired Tier 2 escalations (vs the daily cap of 25)?
- Of Tier 2 firings: how often did T2 confirm T1 (`t1_t2_agree`) vs reverse it (`t1_t2_disagree`)?
- When T2 reversed T1, what was the realized P&L on the merged Hold decision vs what would have happened if we'd taken T1's original Buy/Sell?
- When T2 confirmed T1, was the realized P&L different from T1-only's path? (Confirmation alone shouldn't change outcome, but the merged confidence affects sizing.)

**Where Tier 2 escalation would have helped if it had fired more often:**
- For decisions where T1 was wrong (took a losing trade) AND T3 was right (would have held): how many would the escalation rule have caught? This calibrates the escalation rule's threshold (currently conf 50-75 + catalyst).
- For decisions where T1 was right AND T3 was wrong (Opus blocking a winning trade): how often does this happen? If frequent, Opus is too conservative as a labeler and we should weight its disagreements less.

**Implications for production tuning:**
- If T1-T3 agreement is >90% and disagreements split roughly 50/50 on profitability: Tier 1 is good enough, narrow Tier 2 escalation rule.
- If T1-T3 agreement is 70-85% and Opus is right >60% of the time on disagreements: widen Tier 2 escalation rule (lower the conf threshold, drop the catalyst requirement on some setups).
- If T1-T3 agreement is <70%: Qwen prompt needs significant work before deploying, regardless of escalation.

### 6. Failure modes
- LLM API failures: count + most common
- Schema validation failures: count + sample
- Risk module rejections: count + reasons
- Tier 2 budget exhaustion days (escalation cap of 25 hit during cycle)

### 7. Top decisions worth manual review
- 5 highest-confidence wins
- 5 highest-confidence losses
- 5 most divergent (LLM vs base) decisions

## CLI

```
python scripts/replay_with_llm.py \
    --start 2026-04-01 \
    --end 2026-04-30 \
    --tickers watchlist \
    --t1-backend qwen_local \
    --t1-model qwen3.6-27b-instruct-q4 \
    --t2-enabled \
    --t2-model claude-sonnet-4-5 \
    --t3-enabled \
    --t3-model claude-opus-4-6 \
    --t3-sample-rate 1.0 \
    --prompt-version v1.0 \
    --output-dir docs/reports/
```

For a single-day spot check (cheaper, faster) with Tier 3 disabled to save Opus cost during iteration:

```
python scripts/replay_with_llm.py --start 2026-05-05 --end 2026-05-05 \
    --tickers AAPL,NVDA,DDOG --t3-enabled=false
```

For a Tier-1-only ablation (measure how much Tier 2 + Tier 3 actually contribute):

```
python scripts/replay_with_llm.py --start 2026-05-05 --end 2026-05-05 \
    --tickers watchlist --t2-enabled=false --t3-enabled=false
```

## Code structure

```
scripts/replay_with_llm.py            # CLI entry point + main loop
strategy/signals/llm.py               # the live signal generator (built in M3) — orchestrates T1+T2+merge
strategy/signals/llm_clients.py       # backend clients: QwenLocalClient, AnthropicClient (used by T1, T2, T3 alike)
strategy/signals/escalation.py        # escalation_rule(ctx, t1_decision) -> bool
strategy/signals/merge.py             # merge_tiers(t1, t2) -> live_decision
data/replay/
    historical_bars.py                # Polygon-backed bar loader, point-in-time
    historical_news.py                # news loader with timestamp gating
    historical_sentiment.py           # query trader-prod's sentiment table
    market_context.py                 # SPY, VIX context
    ticker_metadata.py                # sector, market cap; cached locally
data/replay_cache.py                  # per-tier llm response caching (qwen_local/, anthropic/<model>/)
sim/portfolio.py                      # SimulatedPortfolio class
sim/fills.py                          # fill simulator with slippage
sim/comparison.py                     # base vs LLM comparison metrics
sim/tier_analysis.py                  # T1-T2 and T1-T3 agreement metrics for report § 5d
docs/reports/replay_v1_<date>.md      # output reports (gitignored)
```

`strategy/signals/llm.py`, `escalation.py`, and `merge.py` are the
same code that runs live — replay and production share these modules.
The `data/replay/*` and `sim/*` modules are replay-only (live doesn't
need them).

## Backtest credibility checklist

Before any replay result is taken seriously, the harness must satisfy
these checks:

- [ ] Point-in-time correctness verified by sampling: pick 5 random (ticker, timestamp) pairs and manually verify that LLMContext doesn't include any data published after the timestamp
- [ ] Slippage applied to every fill (default 5 bps; configurable)
- [ ] No look-ahead in stop-loss simulation (stop checked against each bar's low/high, not the bar's close, and not the next bar's open)
- [ ] Fees are not modeled (Alpaca paper has no fees, but live commission models are out of scope for this harness)
- [ ] Survivorship bias: the ticker list is the live watchlist on the start_date, not the watchlist as-of-today
- [ ] News timestamps offset by 30s lag (documented; configurable)
- [ ] Both portfolios start with identical initial state
- [ ] Risk module applies identically to both
- [ ] LLM cache cleared when prompt version changes (per-tier; bumping prompt for T1 doesn't invalidate T3 cache)
- [ ] All three tiers see identical LLMContext (no information leakage between tiers — Opus must not see Qwen's decision when labeling, and vice versa)
- [ ] Tier 2 escalation rule in replay matches the live config exactly (conf 50-75 + catalyst flag + PM RVOL > 3x); a "what-if escalation rule X" sweep must use a different config to be honest
- [ ] Tier 3 sample rate documented in the report header; if t3_sample_rate < 1.0, the agreement metrics are statistical estimates with confidence intervals, not exact counts
- [ ] Anthropic model IDs pinned in config and recorded in the run metadata; replays months later use the same pinned IDs

## Open questions

These need answers before M2 implementation begins.

1. **Where do we get historical SPY 5-min bars?** Polygon Stocks Starter covers SPY. Confirmed.

2. **VIX availability.** VIX is an index; Polygon's coverage of indices is limited. May need a separate source (Yahoo Finance is free but unreliable timestamps). Initial implementation: omit VIX from LLMContext if unavailable; document the gap.

3. **News point-in-time gap.** Production trader-prod has been recording news since 2026-04-29. Replays before that date have no recorded news; we'd need to backfill from Polygon News API. Replays after 2026-04-29 use trader-prod's stored news directly. Implementation: support both modes via a config flag.

4. **Sentiment point-in-time gap.** Same boundary — trader-prod's sentiment table starts 2026-04-29. Pre-2026-04-29 replays have no sentiment context. Acceptable initial limitation; we replay the most recent 30 days primarily.

5. **Multi-day position tracking.** If the LLM returns `time_horizon: overnight` or `multi_day`, the simulated position carries across days. The base never carries overnight; ensure portfolio bookkeeping handles this asymmetry without crashing the comparison.

6. **Tier 3 Opus labeling cost budget.** A 60-day, full-watchlist replay with `t3_sample_rate=1.0` is roughly 140K Opus calls × ~$0.02/call (with caching) ≈ $200-400. M2.1-M2.4 initial scope (30-day, watchlist) is half that. We should set a hard budget cap in the harness (`config.t3_max_dollars_per_run = 500`) that aborts the Opus pass if exceeded, so a config typo can't run up a large bill. For prompt iteration where Opus labels don't need to refresh: cache hits are free, so the second iteration on the same window costs $0 for T3. Initial proposal: cap at $500/run; revisit after first M2 run measures actual cost.

7. **Tier 1 wall-clock budget.** Qwen local at 50-70 tok/s × 250 output tokens × 140K calls = roughly 100 hours of GPU time per 60-day full replay. With 4-way batching that drops to ~25 hours, with 8-way to ~12-15 hours. Acceptable for an overnight run; not acceptable for interactive iteration. Mitigation: most iteration happens on cached responses (zero wall-clock); fresh-prompt iterations are scheduled overnight or scoped to a smaller window first.

8. **Tier 2 escalation rule honesty.** The escalation rule references catalyst flags from the news classifier. Historical news classifier output may differ from current classifier output (the classifier itself has been updated over time). For replay correctness, we re-run today's classifier on historical news rather than using whatever was logged at the time. Document the gap; flag any replay where classifier version drift might bias escalation rates.

## Backtest scope expansion (with workstation hardware)

The original effort estimate (3 days, 30-day replay window, watchlist
scope) assumed a developer laptop. The dedicated workstation
(`docs/HARDWARE_PLATFORM.md`) makes a much more ambitious M2 feasible.

### What the hardware enables

- **6-12 month replays** instead of 30 days. 192GB RAM holds the full bar dataset in memory; 6TB NVMe makes initial data loading fast; local LLM inference is ~$0 so we can replay without budget ceiling.
- **Full Russell 3000 ticker scope** instead of just the watchlist. The pre-filter narrows candidates per cycle but the universe of candidates can be all 3000 names.
- **Parameter sweeps**. 24-core CPU runs 16-24 parallel backtests; sweeping ATR multiplier × confidence threshold × pre-filter PM RVOL is feasible in hours.
- **Multiple model variants in parallel comparisons**. Run the same replay against Qwen 3.6-27B, Llama 3.3 70B, Qwen 32B, Sonnet (cloud), Haiku (cloud); compare quality and throughput.
- **Walk-forward validation**. Train prompt on first 80% of period, test on last 20%, slide window forward. Standard ML practice; previously cost-prohibitive.

### Phased rollout (within M2)

- **M2.1-M2.4 (initial scope, ~3 days)**: 30-day replay, watchlist tickers, single LLM model (Qwen 3.6-27B local), no parameter sweep. Goal: prove the harness works.
- **M2.5 (~2 days)**: extend to 90-day replay, add a second model (Llama 3.3 70B) for comparison.
- **M2.6 (~2 days)**: add walk-forward validation and a parameter sweep dimension.
- **M2.7 (~2-3 days)**: extend ticker universe to Russell 3000.

The phased approach lets us catch design flaws on small replays before
running expensive (in wall-clock time, not money) large replays.

## Status

This is the design spec for M2. Sign-off here means we agree on:

- Replay loop structure and time discretization (78 ticks/day, 09:30-15:55)
- Data sources and point-in-time correctness rules
- Pre-filter approach
- Per-tier caching strategy with prompt-version-keyed namespaces
- Storage schema (replay_results.db) recording per-tier outputs
- Comparison report format including Section 5d (tier agreement & escalation analysis)
- CLI shape with tier toggles
- Three-tier replay execution: T1 always, T2 on escalation rule, T3 always (subject to sample_rate and budget cap)

After sign-off, implementation work begins:
- M2.1: scaffolding (CLI, config, data loaders, per-tier cache layout) — ~1 day
- M2.2: replay loop + portfolio sim + tier orchestration (call T1, conditionally T2, always T3) — ~1.5 days
- M2.3: comparison report generator (sections 1-7 + section 5d tier analysis) — ~1 day
- M2.4: stub Tier 1 LLM signal generator (returns Hold; for plumbing test) + stub Anthropic client for T2/T3 (returns canned response) — ~2 hours
- Total M2 effort: ~4 days of focused work (was ~3; tier orchestration adds ~1 day)

After M2 is working, M3 is the only thing standing between us and a real
backtest result.
