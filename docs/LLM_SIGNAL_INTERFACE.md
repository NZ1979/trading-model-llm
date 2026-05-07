# LLM Signal Interface — Design Specification

This is the contract between the platform and Claude. Pin this down
before writing any code. The replay harness, the live signal engine,
the backtest, and the deploy plan all depend on stable input/output
shapes and clear semantics.

## Design principles

1. **Determinism is mandatory for backtesting.** Same input → same output. We achieve this with temperature=0 and by recording both the rendered prompt and the raw response in SQLite so replays match live execution.

2. **Schema-validated output, no free-form anything.** The model returns JSON matching a fixed schema. Any deviation falls back to `Hold(reason='schema_invalid')`. We never trust an LLM output we couldn't parse.

3. **Pre-filter ruthlessly.** A naive "LLM-evaluate every ticker every 5 minutes" scales to ~$70/day on Sonnet. We pre-filter candidates with cheap rules (gap, RVOL, news flag) and only run the LLM on the top N candidates per cycle. Target: ≤30 LLM calls per evaluation cycle.

4. **The LLM advises, the platform decides.** The risk module still gates everything (position cap, exposure cap, ATR stop bounds). If Claude says Buy with `stop_loss_distance_atr=10`, our risk module clamps it. If Claude says Buy on a ticker with earnings today, the existing earnings veto still blocks it. The LLM is one component of a pipeline, not the whole pipeline.

5. **Versioned prompt templates.** Every prompt variant gets a version string baked into the prompt itself and recorded with the decision. When we update the prompt, old recorded decisions don't get retroactively re-interpreted.

6. **Cost and latency are first-class.** Every prompt design decision must justify itself against the cost/latency ledger, not just "what gives the best decisions."

## Architecture

```
Existing infrastructure (unchanged from base):
  - Polygon, Alpaca, Finnhub data feeds
  - compute_intraday_indicators, compute_premarket_context
  - News pipeline + sentiment scoring (Haiku-based)
  - Risk validation, ATR stops, bracket order placement
  - Flatten routine, EOD journal

New for this fork (this doc specifies):

  pre-filter (cheap)              LLM call (expensive)        decision
  ----------------                ---------------             --------
  watchlist (500)
        |
        v
  candidate_filter()  ---->  N candidates  ---->  build_context()
                             (typ. 5-30)                |
                                                        v
                                                   render_prompt()
                                                        |
                                                        v
                                                    Claude API
                                                        |
                                                        v
                                                  parse_response()
                                                        |
                                                        v
                                              TechnicalSignal
                                                        |
                                                        v
                                              evaluate_trade() (existing)
                                                        |
                                                        v
                                              _place_order() (existing)
```

## Input context structure

The platform assembles a `LLMContext` dict per candidate ticker. Every
field has a fixed type and unit. Order is important — the rendered
prompt formats fields in this order.

```python
@dataclass(frozen=True, slots=True)
class LLMContext:
    # ---- Meta ----
    ticker: str                       # "AAPL"
    timestamp_et: str                  # "2026-05-07 09:42:00 ET"
    prompt_version: str                # "v1.0" — bumped on prompt change

    # ---- Market context (same for all tickers in cycle) ----
    spy_change_pct: float              # SPY's session change so far, %
    spy_rvol: float                    # SPY's session-volume ratio vs 20d avg
    vix_level: float                   # VIX latest reading (None if unavailable)
    market_regime_label: str           # "trending_up" | "trending_down" | "choppy"

    # ---- Ticker fundamentals (slowly-changing) ----
    sector: str                        # "Technology" | "Financials" | etc.
    market_cap_bucket: str             # "mega" | "large" | "mid" | "small"
    avg_daily_volume: int              # 30-day ADV in shares

    # ---- Daily regime context ----
    daily_regime: str                  # "bull" | "bear" | "neutral"
    daily_adx_14: float                # 0-100, >25 = trending
    daily_atr_14: float                # in price units
    sma_200: float
    last_5_daily_closes: list[float]   # most recent 5 daily closes

    # ---- Pre-market context ----
    gap_pct: float
    pm_rvol: float                     # vs 20-day mean
    pm_high: float | None
    pm_low: float | None
    pm_volume: int

    # ---- Intraday context ----
    current_close: float
    current_volume: int
    current_5min_bar_count: int        # so the LLM knows how warm indicators are
    last_10_5min_bars: list[dict]      # [{ts, o, h, l, c, v}, ...]
    rsi_14: float
    macd_hist: float
    macd_hist_3bar_trend: str          # "rising" | "falling" | "flat"
    vwap: float
    distance_to_vwap_pct: float
    bollinger_position: float          # -1 (lower band) to +1 (upper band)
    volume_ratio_vs_20bar: float       # current bar vs 20-bar mean

    # ---- News & sentiment ----
    news_items: list[dict]             # [{ts, headline, sentiment_score, source}, ...]
                                       # filtered to last 24h, max 5 items
    has_earnings_today: bool
    has_earnings_within_3d: bool
    catalyst_flags: list[str]          # ["FDA_approval", "M&A_rumor", ...] from existing classifier

    # ---- Position state ----
    currently_holding: bool
    position_qty: int                  # 0 if not holding; positive=long, negative=short
    position_avg_price: float | None
    position_unrealized_pl_pct: float | None
    has_active_stop: bool

    # ---- Decision history ----
    todays_prior_decisions: list[dict] # [{ts, action, setup_label, confidence}, ...]
                                       # at most last 5 evaluations of this ticker today

    # ---- Time-of-day context ----
    minutes_since_open: int
    minutes_until_close: int
    in_gap_and_go_window: bool         # 09:35-10:00 ET
```

### Notes on field choices

- **No raw bar data beyond 10 5-min bars.** Claude can't usefully process 500 bars; we let our existing indicator math compress to summary metrics. The 10-bar window is enough for "is this stock moving up or down right now."
- **News items capped at 5.** Sentiment scoring already reduces N headlines to a single score per item; including more dilutes signal.
- **Position state included even when 0.** Lets the LLM consider "should I add to this position" or "should I rotate."
- **Time-of-day fields explicit.** Don't make Claude infer from timestamps; the model gets distracted by date math.

## Output schema

JSON only. No prose outside the JSON. We use Anthropic's tool-use feature
to enforce schema (the model literally cannot return non-conforming output
when invoked via a tool definition).

```json
{
  "action": "Buy" | "Sell" | "Hold",
  "confidence": 0-100,
  "setup_label": "string, max 50 chars",
  "reasoning": "string, max 280 chars",

  "stop_loss_atr_multiple": 1.0-3.0,
  "take_profit_atr_multiple": 1.0-5.0,
  "time_horizon": "intraday" | "overnight" | "multi_day",

  "concerns": ["string", "string"],
  "alternative_view": "string, max 140 chars"
}
```

### Field semantics

- **action**: the only field that gates execution. `Hold` is always safe to return when uncertain.
- **confidence**: subjective. 0-30 = "I see something but probably noise." 31-60 = "modest signal." 61-85 = "good setup." 86-100 = "high-conviction." We will calibrate against actual P&L in M4.
- **setup_label**: free-form Claude-generated label. Useful for grouping decisions in the journal: "gap-and-go-with-news", "bear-flag-rejection", "consolidation-breakout", etc. We don't constrain the vocabulary; let the LLM cluster naturally and we'll bucket post-hoc.
- **reasoning**: 1-2 sentences. Short enough to log every time without bloating storage.
- **stop_loss_atr_multiple**: only meaningful if action != Hold. Validates against `stop_atr_min_pct` and `stop_atr_max_pct` config from the base; clamped if out of range.
- **take_profit_atr_multiple**: not currently used by the base order placement (no TP leg in the OTO bracket), but recorded for future use and for backtesting "what would have happened if we'd taken profit at N×ATR."
- **time_horizon**: "intraday" is the default; "overnight" or "multi_day" implies the trade should not be flattened at 15:55 ET. The base flatten routine doesn't currently honor this; recording it now lets us evaluate whether the LLM's overnight calls would have been profitable post-hoc.
- **concerns**: list of strings flagging risks ("low_liquidity", "earnings_within_3d", "counter_trend_to_daily_regime"). The LLM populates these; we use them as audit fields.
- **alternative_view**: 1 sentence stating the opposite-side argument. Forces the LLM to consider the bear case for Buy / bull case for Sell. Recorded for post-hoc review.

## Prompt template

Stored as `strategy/signals/llm_prompt_v1.txt`. Versioned filename so
changes are explicit. Each version is immutable — to update, create
`llm_prompt_v2.txt` and bump `prompt_version` field in `LLMContext`.

```text
You are an intraday equity trader evaluating {ticker} at {timestamp_et}.
You make conservative, high-conviction decisions. You prefer Hold over
forcing a marginal trade.

# Market context
SPY today: {spy_change_pct:+.2f}% on {spy_rvol:.1f}x normal volume.
VIX: {vix_level:.1f}.
Regime: {market_regime_label}.

# {ticker}: {sector} / {market_cap_bucket} cap (ADV ~{avg_daily_volume:,} sh)
Daily regime: {daily_regime} (close ${current_close:.2f} vs SMA200 ${sma_200:.2f},
ADX {daily_adx_14:.1f}, ATR ${daily_atr_14:.2f}).
Last 5 daily closes: {last_5_daily_closes}.

# Today
Gap: {gap_pct:+.2f}% on {pm_rvol:.1f}x normal premarket volume.
PM range: {pm_low:.2f} - {pm_high:.2f}.
Time: {minutes_since_open} min since open, {minutes_until_close} until close.
In gap-and-go window: {in_gap_and_go_window}.

# Current 5-min bar
Close ${current_close:.2f}, volume {volume_ratio_vs_20bar:.1f}x recent average.
RSI(14) {rsi_14:.1f}. MACD histogram {macd_hist_3bar_trend} ({macd_hist:+.4f}).
VWAP ${vwap:.2f} ({distance_to_vwap_pct:+.2f}%).
Bollinger position {bollinger_position:+.2f} (-1=lower band, +1=upper band).

# Last 10 5-min bars
{last_10_5min_bars_table}

# Recent news (last 24h, sentiment-scored)
{news_items_table}
{catalyst_flags_line}

# Position state
{position_state_block}

# Today's prior decisions on {ticker}
{prior_decisions_block}

# Decision criteria
- Hold is the default. Only return Buy or Sell if you see a setup
  worth ~0.5% account risk.
- Confidence below 40 should always be Hold (we lose money on low-conviction
  trades historically).
- Counter-trend trades (Buy in bear regime, Sell in bull regime) need
  exceptional justification — typically a clear catalyst.
- Stops must be at least 1.0 ATR (any tighter gets stopped by noise) and
  at most 3.0 ATR (anything wider blows the per-trade risk budget).

Return JSON only, matching this schema:
{output_schema}

Prompt version: {prompt_version}
```

## Worked example

Hypothetical scenario: NVDA at 09:42 ET, gapped up 3% on AI partnership news, currently holding the gap above PM low, MACD turning up.

**Rendered prompt (truncated):**

```
You are an intraday equity trader evaluating NVDA at 2026-05-07 09:42:00 ET.

# Market context
SPY today: +0.45% on 1.1x normal volume.
VIX: 14.2.
Regime: trending_up.

# NVDA: Technology / mega cap (ADV ~280,000,000 sh)
Daily regime: bull (close $148.50 vs SMA200 $122.10, ADX 28.5, ATR $4.20).
Last 5 daily closes: [144.20, 145.80, 147.10, 144.50, 148.50].

# Today
Gap: +3.10% on 4.2x normal premarket volume.
PM range: 147.20 - 149.80.
Time: 12 min since open, 348 until close.
In gap-and-go window: True.

[...]

# Recent news (last 24h, sentiment-scored)
- [09:15 ET] NVDA announces strategic AI partnership with Palantir. (sentiment: 8)
- [yesterday 16:05] Q1 earnings beat consensus on data center revenue. (sentiment: 7)
```

**Expected response (illustrative):**

```json
{
  "action": "Buy",
  "confidence": 78,
  "setup_label": "gap-and-go-with-fresh-catalyst",
  "reasoning": "Strong catalyst (AI partnership news 27 min before open) drove a 3% gap on 4x volume. Price is holding above premarket low and breaking the premarket high, with daily regime bull-aligned. ATR-based stop ~1.5 ATR below entry leaves room for normal noise.",
  "stop_loss_atr_multiple": 1.5,
  "take_profit_atr_multiple": 2.5,
  "time_horizon": "intraday",
  "concerns": ["mega_cap_gaps_often_fade", "VIX_low_so_breakouts_have_less_extension"],
  "alternative_view": "Mega-cap gaps frequently fade after the first 30 min as institutional sellers fade retail enthusiasm; if VIX picks up the trend can reverse fast."
}
```

This response would route through:
1. Schema validation: passes
2. Risk module: 78 confidence × 0.5% per-trade risk = position size from `size_from_risk` with stop at 1.5 × $4.20 = $6.30 below entry. Clamped to `max_position_pct` (20%).
3. Earnings veto: false (NVDA earnings yesterday, not today)
4. Order placement: bracket with limit at current bid, stop at $148.50 - $6.30 = $142.20.

## Cost & latency model

The cost/latency calculus depends entirely on which backend the signal
generator is talking to. See `docs/HARDWARE_PLATFORM.md` for the full
analysis. Summary:

### Cloud backend (Anthropic; comparison baseline)

| Tier | Input tokens (typical) | Output tokens | $/call | Latency |
|---|---|---|---|---|
| Haiku 4.5 | ~1500 | ~250 | $0.0011 | 400-800ms |
| Sonnet 4.5 | ~1500 | ~250 | $0.0048 | 700-1500ms |

With pre-filter at ≤30 candidates/cycle × 78 cycles/day = 2340 calls/day:
- Haiku: $2.57/day, $51/month
- Sonnet: $11.23/day, $225/month

### Local backend (RTX PRO 5000 Blackwell 48GB; primary)

| Model | 4-bit VRAM | Throughput | $/call | Per-call latency (250 out) |
|---|---|---|---|---|
| Qwen 2.5 72B | ~40GB | 50-70 tok/s | ~$0 | 3.5-5s |
| Llama 3.3 70B | ~38GB | 50-80 tok/s | ~$0 | 3-5s |
| Qwen 2.5 32B | ~17GB | ~100 tok/s | ~$0 | 2.5s |

With local inference at zero marginal cost, the pre-filter exists only
for *quality* reasons (don't run the model on tickers with obviously
no setup) and for *throughput* (a 500-call cycle takes 60-120s with
modest batching, comfortable within a 300s cycle budget).

### Implications for the design

1. **Pre-filter from cost-driven (≤30 candidates) → quality-driven (relax to 100-200, or full watchlist if model throughput allows).** The narrower limit was a budget constraint that no longer applies. Initial M2 keeps the conservative pre-filter; M3+ may relax it after measuring whether it costs us setups.

2. **Per-call latency is higher locally (3-5s vs 700ms cloud).** This is offset by the absence of per-call dollar pressure; we just call concurrently with batching support from LM Studio's API.

3. **Cloud backend remains the fallback.** If LM Studio is offline (workstation down, model unloaded, etc.), the signal generator falls back to Anthropic Haiku. This adds resilience without changing the schema or interface.

4. **All cost numbers are sensitive to context length.** As we iterate the prompt and add more historical bars or news, input tokens grow. Local cost stays $0; cloud cost scales linearly. This favors longer-context experiments locally.

## Failure modes & fallbacks

| Failure | Detection | Fallback |
|---|---|---|
| Anthropic API down / 503 | exception on call | Hold(api_failure), log + alert |
| Schema-invalid output | Pydantic validation fails | Hold(schema_invalid), log raw response |
| Out-of-range field (confidence=150, stop=10×ATR) | range check after parse | clamp to bounds, proceed |
| Latency > 3s | timeout=3000ms | Hold(api_timeout), proceed without LLM |
| Daily Anthropic budget exhausted | spend tracker | switch to Hold for rest of day, alert |
| Prompt version mismatch in replay | recorded version != current version | use recorded prompt verbatim from DB |

All fallback paths produce Hold, never a default Buy/Sell. The LLM
errors safely.

## Open questions

1. **Pre-filter design.** What rules determine the candidate set per cycle? Initial proposal: any ticker with `pm_rvol > 2x` OR `gap_pct > 1%` OR `news_in_last_2h` OR `currently_holding`. Targets are: be inclusive enough to catch real setups, restrictive enough to keep cost manageable.

2. **Position-management calls.** When holding a position, should we evaluate it every cycle (cost: 12-78 calls/day per held position) or rely on the bracket stop alone? Initial proposal: evaluate held positions every 15 min, not every 5 min, to control cost without losing reaction time.

3. **Backtest harness assumptions.** Replay against historical news is only meaningful if the news was available at the time. Polygon's news API timestamps need verification; if news has variable lag, we need to model that.

4. **Regime-aware prompt.** Should the prompt template change based on `market_regime_label`? E.g. in choppy regimes, the prompt explicitly de-emphasizes breakout setups. Could be done with template selection (`v1.0_trending`, `v1.0_choppy`) or with a single template that adapts based on the regime field. Initial proposal: single template; let the LLM weight from context.

5. **Calibrating confidence.** After a few weeks of paper trading, we should plot confidence vs realized P&L. If confidence=80 trades win 40% of the time, the model is overconfident and we recalibrate (or train a confidence-correction layer).

## Open implementation choices for M3

These don't need answers now but should be settled before M3 (the
actual signal engine implementation):

- Sync vs async API client (recommendation: async to support parallel calls)
- Tool-use vs raw JSON output (recommendation: tool-use for guaranteed schema compliance)
- Where the prompt template lives in the codebase (recommendation: `strategy/signals/llm_prompt_v1.txt` as a static text file with format placeholders)
- Cost tracking (recommendation: log input_tokens, output_tokens, model per call; aggregate daily)
- Decision storage schema in SQLite (extends existing `decisions` table with `prompt_version`, `raw_response`, `cost_cents` columns)

## Status

This document is the spec for M2 (replay harness) and M3 (live signal
engine) to build against. Sign-off here means we agree on:

- The input context structure (what gets fed to the LLM)
- The output schema (what the LLM returns)
- The prompt template structure (with the v1.0 template above as the
  starting point — will iterate)
- The pre-filter approach (rule-based candidate selection before LLM call)
- The fallback behavior (Hold on every failure mode)

After sign-off, M2 (replay harness) becomes the next concrete deliverable.
