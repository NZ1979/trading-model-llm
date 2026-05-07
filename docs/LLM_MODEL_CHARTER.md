# LLM Model — Project Charter

## Premise

Replace the rule-based signal engine (gap-and-go + pullback in the base
codebase) with an LLM-driven signal generator. At each evaluation tick,
feed an LLM a structured snapshot of market context, recent news, and
computed technical indicators, and have it return a structured
Buy/Sell/Hold decision with reasoning, confidence, and a stop-loss
recommendation.

**Backend choice (updated 2026-05-07):** primary inference path is a
locally-hosted 70B-class open-weight model (Qwen 2.5 72B Instruct or
similar) running on a dedicated workstation with an RTX PRO 5000
Blackwell 48GB GPU via LM Studio. Cloud Claude (Haiku/Sonnet) is the
comparison baseline and the fallback when the local server is
unavailable. Hardware specs and the architectural rationale for the
local-first approach are in `docs/HARDWARE_PLATFORM.md`.

## Why this might work better than rules

The base strategy fires when a fixed pattern matches (gap-up ≥ 1%,
PM RVOL > threshold, sentiment ≥ 3, etc.). Empirically, those rules
produce setups that fail in choppy regimes and over-trigger in trending
ones — a fundamental limitation of fixed rules without regime awareness.

An LLM has three potential advantages:

1. **Holistic context integration.** Instead of pre-defined gates, the LLM weighs all available signals against each other. A weak gap with strong sentiment + high volume + bullish daily regime might fire a Buy where the rule-based engine wouldn't (none of the gates exceeded threshold individually). Conversely, a strong gap on a stock with adverse news might be filtered out where rules would have fired.

2. **Setup discovery, not just setup matching.** Rules can only match setups they were programmed to match. An LLM can identify novel patterns ("this looks like a fade-the-gap setup, not a gap-and-go") and route to a different decision rationale.

3. **Reasoning artifact for review.** Each LLM decision comes with explanations. Reviewing past decisions becomes possible at the rationale level, not just the action level — which makes the iteration loop tighter.

## Why this might NOT work better

1. **Latency.** A Claude call adds 500-2000ms per evaluation. Across 500 watchlist tickers per 5-min bar, that's a non-trivial budget. May require asynchronous batching, caching, or restricting LLM calls to only the most-likely-setup subset.

2. **Cost.** Headline-scoring with Haiku is ~$0.01 per call. A signal-evaluation call needs more context (recent bars, indicators, news, premarket context, sentiment history) so will use more tokens and possibly Sonnet rather than Haiku. Rough estimate: 500 tickers × 12 evaluations/day × $0.05/call = $300/day. Untenable for paper trading; needs careful filtering.

3. **Reproducibility.** LLM outputs are non-deterministic by default. Same prompt can yield different decisions on different calls. Backtesting and live execution will produce slightly different results unless we set temperature=0 and accept the corresponding loss of nuance.

4. **Hallucination risk.** An LLM might invent indicators it didn't actually see, misread numbers, or rationalize a Buy on a stock it shouldn't trade. Defensive design needs schema-validated outputs and sanity bounds.

## Architecture sketch

### Data flow

```
   Existing data feeds (unchanged)
       |
       v
   compute_intraday_indicators + compute_premarket_context (unchanged)
       |
       v
   strategy/llm_signal/llm_signal_engine.py    <-- NEW
   - assembles a context dict per ticker
   - calls Claude with a structured prompt
   - parses + validates the JSON response
       |
       v
   evaluate_trade (unchanged)
       |
       v
   _place_order (unchanged: existing risk + ATR stops apply)
```

The LLM signal engine is a drop-in replacement for the existing
`strategy.signal_engine.evaluate_trade` -> wait, that's the combiner.
Replacement happens at `strategy/signals/`. We add a third signal
module `strategy/signals/llm.py` that returns a TechnicalSignal-shaped
result, and the dispatcher in `analysis.indicators.generate_signal` is
updated to route to it.

### Prompt template (sketch)

```
You are an intraday equity trader evaluating {TICKER} at {TIMESTAMP}.

Market context:
- SPY: {SPY_CHANGE_PCT}% on {SPY_RVOL}x volume
- Daily regime: {regime} (SMA200={sma_200}, ADX={adx_14})
- Today's gap: {gap_pct}% on {pm_rvol}x premarket volume

Recent news (last 24h):
{news_items}  # ranked by recency, with sentiment scores

Latest 5-min indicators:
- Close: ${close}, VWAP: ${vwap}
- RSI(14): {rsi}, MACD hist: {macd_hist}
- Volume vs 20-bar avg: {volume_ratio}x

Currently in window: {gap_and_go_window | pullback_window | post_window}

Decide: Buy, Sell, or Hold. If Buy or Sell, provide:
- confidence (0-100)
- stop_loss_distance_atr (1.0 to 3.0 multiples of daily ATR)
- reasoning (1-2 sentences)
- setup_label (your characterization, free-form)

Output JSON only, matching this schema:
{decision_schema}
```

This sketch is illustrative; the real prompt will go through several
iterations on a held-out replay set before going live.

### Output schema (preliminary)

```json
{
  "action": "Buy" | "Sell" | "Hold",
  "confidence": 0-100,
  "stop_loss_distance_atr": 1.0 | 1.5 | 2.0 | 2.5 | 3.0,
  "setup_label": "free-form, e.g. 'gap-and-go-with-news-catalyst'",
  "reasoning": "1-2 sentences explaining the decision"
}
```

Schema validation happens before passing to `evaluate_trade`. Anything
that fails validation falls back to Hold(no_setup).

## Open questions

These need answers before implementation begins:

1. **Which Claude tier?** Haiku is cheap but may not handle the full
context. Sonnet is more capable but 4-8x cost. Opus is overkill for
real-time use. Proposal: prototype on Haiku, escalate to Sonnet if
output quality is insufficient.

2. **How often to call?** Every 5-min bar (12 calls/day per ticker)?
Only once per setup identified by a cheaper pre-filter? Only on news?
Different cadences per regime?

3. **Pre-filter or no?** A rule-based pre-filter could narrow the
universe to "candidates" before LLM evaluation. Saves cost but
re-introduces the rule-based limitation we're trying to escape from.

4. **Backtest infrastructure.** The base has no formal backtest engine
(strategy is forward-tested in paper). LLM strategy needs a replay
harness to evaluate deterministically against historical data. This is
its own project.

5. **Live execution fallback.** If the Anthropic API is down or returns
malformed JSON, what does the system do? Proposal: fall back to
hardcoded Hold(api_failure) and emit a metric. No silent degradation
to the rule-based base strategy — that would mask the LLM's failure
mode.

6. **Cost control.** Hard daily budget for Anthropic spend on this
strategy? Daily quota that shuts down LLM evaluation if exceeded?

## Milestones

### M1 — Project setup (this commit)

- ✅ Fork repo created
- ✅ README + charter doc written
- ✅ Connected to GitHub at `github.com/NZ1979/trading-model-llm`
- Next commit: any additional setup work

### M2 — Replay harness

- New script: `scripts/replay_with_llm.py`
- Takes a historical date + ticker list
- Loads daily + 5-min bars from local cache (or pulls from Polygon)
- For each evaluation tick, calls the LLM signal engine with the
  reconstructed context
- Records decisions, simulated fills, simulated P/L
- Outputs a markdown report comparing LLM decisions vs the base's
  rule-based decisions on the same period

### M3 — Initial LLM signal engine

- New module: `strategy/signals/llm.py`
- Calls Claude with the prompt template
- Validates output schema
- Returns a TechnicalSignal-compatible result
- Falls back to Hold on any failure
- Tests: prompt template stability, schema validation, fallback paths

### M4 — Backtest + comparison

- Run M2 replay against the last 30 trading days, both base and LLM
  strategies
- Compute win rate, avg win, avg loss, Sharpe, drawdown for each
- Markdown report
- Decision: deploy LLM strategy to paper trading or iterate further

### M5 — Paper trading

- Deploy to a separate VPS instance (don't co-host with base since
  resource profiles differ — LLM model is more memory/network-heavy)
- 4-week paper trading observation
- Compare to base trading-platform results over the same period

### M6 — Decision

- Promote to a primary strategy, OR
- Keep as a research branch indefinitely, OR
- Retire if results don't justify cost

## Out of scope (for now)

- Multi-asset support (futures, options, crypto)
- Reinforcement learning / fine-tuned models
- Human-in-the-loop confirmation before order placement
- Live news streaming directly into the LLM context (we use the existing news pipeline's stored sentiment scores instead)
- Multiple LLM ensemble (run multiple models, vote)

These are interesting directions but not the immediate goal. The
immediate goal is: does a single Claude call per evaluation produce
better trading decisions than the base's rule-based signals?

## Related research and competing approaches

This isn't the only way to ML-enhance momentum trading. Worth knowing
the landscape (full notes in `docs/RESEARCH_NOTES.md`):

- **Characteristic-Managed Momentum (CMM)**: ML-enhanced *traditional* momentum strategies. Engineers features that improve momentum signal quality and reduce crash risk. Different paradigm from ours (LLM-driven holistic reasoning) but tackles the same problem class.
- **Regime-dependent model selection**: research suggests different ML model types (LSTM, SVM, etc.) excel in different market regimes. The implication for our work: Claude's regime-passing prompt may not be enough; regime-specific prompt templates may be required. Listed as an open question above.
- **Ensemble methods**: combining multiple signal generators (rule-based + LLM + ML classifier) tends to outperform single-strategy approaches in published research. Our `strategy/signals/` plug-in architecture supports this; the LLM is one signal module, and we can register others alongside in M5+.

We're betting on LLM holistic reasoning as the primary differentiator
for this fork. If that bet pays off, the ensemble-of-strategies
extension is straightforward. If it doesn't, the research above gives
us alternative directions.
