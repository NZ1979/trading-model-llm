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

Three questions we want answered:

1. **Does the LLM strategy make different decisions than the base?** If LLM agrees with base 100% of the time, there's no point. If LLM differs sharply, that's where the value (or risk) lives.

2. **Are the LLM's different decisions better?** Measured by: win rate, average win/loss size, P&L per dollar at risk, max drawdown over the replay window.

3. **What does the LLM see that the rules miss, and vice versa?** Qualitative review of decisions where LLM and base disagree — does the LLM catch setups the rules miss? Does it avoid setups the rules over-fire on?

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
                              llm_decision = llm_signal(ctx) ----> cache.get(hash(ctx))
                                                                    or claude.call()
                              base_decision = base_signal(ctx_to_base_inputs(ctx))
                              record(date, time, ticker, llm_decision, base_decision)
                              if llm_decision.action != Hold:
                                  simulate_fill(llm_portfolio, llm_decision)
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
    llm_tier: str                          # "haiku" | "sonnet" | "hybrid"
    llm_prompt_version: str                # "v1.0", "v1.1", etc.

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

To control cost during iteration, the harness caches LLM responses
keyed by `(prompt_hash, model)`:

```python
def cached_llm_call(prompt: str, model: str) -> dict:
    key = sha256(prompt.encode()).hexdigest()
    cache_path = config.cache_dir / model / f"{key}.json"
    if cache_path.exists():
        return json.load(open(cache_path))
    response = anthropic_client.messages.create(...)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(response.model_dump(), open(cache_path, "w"))
    return response
```

First run of a replay window: full cost. Re-runs of the same replay (to
iterate on the comparison report format, not the prompt): zero cost.

When the prompt version bumps (v1.0 → v1.1), the cache key includes the
prompt version, so we don't accidentally use cached v1.0 responses
against v1.1 queries.

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
    decision_source TEXT NOT NULL,     -- "llm" or "base"
    action TEXT NOT NULL,               -- Buy/Sell/Hold
    setup_label TEXT,
    confidence INTEGER,
    reasoning TEXT,
    raw_response TEXT,                  -- JSON for LLM, debug str for base
    risk_check_result TEXT,             -- "approved" or rejection reason
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id)
);

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
- LLM tier, prompt version
- Total LLM calls made (cache hits vs misses)
- Total cost (only counts cache misses)

### 2. Decision summary
- LLM decision count by action and setup_label
- Base decision count by action and setup
- Side-by-side counts in a table

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

### 6. Failure modes
- LLM API failures: count + most common
- Schema validation failures: count + sample
- Risk module rejections: count + reasons

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
    --tier sonnet \
    --prompt-version v1.0 \
    --output-dir docs/reports/
```

For a single-day spot check (cheaper, faster):

```
python scripts/replay_with_llm.py --start 2026-05-05 --end 2026-05-05 --tickers AAPL,NVDA,DDOG
```

## Code structure

```
scripts/replay_with_llm.py            # CLI entry point + main loop
strategy/signals/llm.py               # the live signal generator (built in M3)
data/replay/
    historical_bars.py                # Polygon-backed bar loader, point-in-time
    historical_news.py                # news loader with timestamp gating
    historical_sentiment.py           # query trader-prod's sentiment table
    market_context.py                 # SPY, VIX context
    ticker_metadata.py                # sector, market cap; cached locally
data/replay_cache.py                  # llm response caching helpers
sim/portfolio.py                      # SimulatedPortfolio class
sim/fills.py                          # fill simulator with slippage
sim/comparison.py                     # base vs LLM comparison metrics
docs/reports/replay_v1_<date>.md      # output reports (gitignored)
```

`strategy/signals/llm.py` is the same code that runs live — replay and
production share that module. The `data/replay/*` and `sim/*` modules
are replay-only (live doesn't need them).

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
- [ ] LLM cache cleared when prompt version changes

## Open questions

These need answers before M2 implementation begins.

1. **Where do we get historical SPY 5-min bars?** Polygon Stocks Starter covers SPY. Confirmed.

2. **VIX availability.** VIX is an index; Polygon's coverage of indices is limited. May need a separate source (Yahoo Finance is free but unreliable timestamps). Initial implementation: omit VIX from LLMContext if unavailable; document the gap.

3. **News point-in-time gap.** Production trader-prod has been recording news since 2026-04-29. Replays before that date have no recorded news; we'd need to backfill from Polygon News API. Replays after 2026-04-29 use trader-prod's stored news directly. Implementation: support both modes via a config flag.

4. **Sentiment point-in-time gap.** Same boundary — trader-prod's sentiment table starts 2026-04-29. Pre-2026-04-29 replays have no sentiment context. Acceptable initial limitation; we replay the most recent 30 days primarily.

5. **Multi-day position tracking.** If the LLM returns `time_horizon: overnight` or `multi_day`, the simulated position carries across days. The base never carries overnight; ensure portfolio bookkeeping handles this asymmetry without crashing the comparison.

## Backtest scope expansion (with workstation hardware)

The original effort estimate (3 days, 30-day replay window, watchlist
scope) assumed a developer laptop. The dedicated workstation
(`docs/HARDWARE_PLATFORM.md`) makes a much more ambitious M2 feasible.

### What the hardware enables

- **6-12 month replays** instead of 30 days. 192GB RAM holds the full bar dataset in memory; 6TB NVMe makes initial data loading fast; local LLM inference is ~$0 so we can replay without budget ceiling.
- **Full Russell 3000 ticker scope** instead of just the watchlist. The pre-filter narrows candidates per cycle but the universe of candidates can be all 3000 names.
- **Parameter sweeps**. 24-core CPU runs 16-24 parallel backtests; sweeping ATR multiplier × confidence threshold × pre-filter PM RVOL is feasible in hours.
- **Multiple model variants in parallel comparisons**. Run the same replay against Qwen 72B, Llama 3.3 70B, Qwen 32B, Sonnet (cloud), Haiku (cloud); compare quality and throughput.
- **Walk-forward validation**. Train prompt on first 80% of period, test on last 20%, slide window forward. Standard ML practice; previously cost-prohibitive.

### Phased rollout (within M2)

- **M2.1-M2.4 (initial scope, ~3 days)**: 30-day replay, watchlist tickers, single LLM model (Qwen 72B local), no parameter sweep. Goal: prove the harness works.
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
- Caching strategy
- Storage schema (replay_results.db)
- Comparison report format
- CLI shape

After sign-off, implementation work begins:
- M2.1: scaffolding (CLI, config, data loaders) — ~1 day
- M2.2: replay loop + portfolio sim — ~1 day
- M2.3: comparison report generator — ~half day
- M2.4: stub LLM signal generator (returns Hold; for plumbing test) — ~1 hour
- Total M2 effort: ~3 days of focused work

After M2 is working, M3 is the only thing standing between us and a real
backtest result.
