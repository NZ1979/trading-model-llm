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

  pre-filter (cheap)              tiered LLM evaluation        decision
  ----------------                ---------------------        --------
  watchlist (500)
        |
        v
  candidate_filter()  ---->  N candidates  ---->  build_context()
                             (typ. 30-200)              |
                                                        v
                                                   render_prompt()
                                                        |
                                                        v
                                              +--------------------+
                                              | Tier 1: Qwen 3.6-27B   |
                                              | local, every call  |
                                              +---------+----------+
                                                        |
                                                   parse + validate
                                                        |
                                                  +-----+-----+
                                                  | escalate? |  conf in [50,75]
                                                  +-----+-----+  AND catalyst hit
                                                        | yes (~5-15/day)
                                                        v
                                              +--------------------+
                                              | Tier 2: Sonnet 4.5 |
                                              | selective only     |
                                              +---------+----------+
                                                        |
                                                  merge(T1, T2)
                                                        |
                                                        v
                                              TechnicalSignal
                                                        |
                                                        v
                                              evaluate_trade() (existing)
                                                        |
                                                        v
                                              _place_order() (existing)

  Offline (not in hot path):
    Tier 3: Claude Opus 4.6 - labels M2 replay decisions as gold
            standard, and audits last week's live decisions weekly.
            See "Tiered evaluation" section below.
```

## Tiered evaluation: Qwen primary, Claude escalation, Opus evaluator

The signal generator uses three model tiers, each chosen for what it
does best and bounded by what it costs. The motivating problem: Qwen
72B is fast, free, private, and deterministic, but has documented
finance-domain reasoning gaps versus Claude Sonnet/Opus on ambiguous
catalyst-driven decisions. Putting Claude in the per-candidate hot
path defeats the privacy and zero-marginal-cost wins from local
inference. The compromise: Claude only where its strengths actually
compound.

### Tier 1: Qwen 3.6-27B local — primary, hot path

Runs on the local RTX PRO 5000 via LM Studio's OpenAI-compatible API.
Evaluates every pre-filtered candidate every cycle. Zero marginal
cost, full privacy (no data leaves the workstation), fully
deterministic for backtest reproducibility (weights are immutable;
temperature=0).

Trade-offs accepted: weaker financial-domain reasoning than Claude on
ambiguous catalyst-driven cases; non-reasoning architecture means
chain-of-thought scaffolding must come from prompt structure rather
than the model itself. Mitigated by Tier 2.

### Tier 2: Claude Sonnet 4.5 — selective escalation

Fired only when ALL of these hold for a candidate:

1. Tier 1 returned `confidence ∈ [50, 75]` (the uncertain middle where stronger reasoning adds the most value; high-conviction Qwen calls don't need a second opinion, low-conviction calls become Hold anyway)
2. Candidate has a high-quality catalyst flag set (`catalyst_flags` non-empty AND any item is one of `FDA_approval`, `M&A`, `earnings_beat_with_guidance_raise`, `breakthrough_news`)
3. Pre-market RVOL > 3x (the setup is liquid enough to actually trade)
4. Daily escalation budget not exhausted (cap: 25 calls/day, configurable)

Expected volume: 5-15 escalations per trading day under normal
conditions. Cost on Sonnet 4.5 with prompt caching: ~$0.10-0.30/day.
The escalation budget cap protects against pathological volatility
days where many candidates fall in the 50-75 confidence band.

### Tier 1 + Tier 2 merge logic

When Tier 2 fires, the final decision is computed by merging:

```python
def merge_tiers(t1: LLMDecision, t2: LLMDecision) -> LLMDecision:
    # Both agree on action: trust the action, take the higher confidence,
    # use Tier 2's reasoning (it's the more carefully reasoned output)
    if t1.action == t2.action:
        return t2.copy(
            confidence=max(t1.confidence, t2.confidence),
            reasoning=f"[T1+T2 agree] {t2.reasoning}",
            tier_provenance="t1_t2_agree",
        )

    # Disagree: default to Hold. Disagreement = no edge, don't trade.
    return LLMDecision(
        action="Hold",
        confidence=0,
        setup_label="tier_disagreement",
        reasoning=(
            f"T1={t1.action}({t1.confidence}); "
            f"T2={t2.action}({t2.confidence}). "
            f"Defaulting to Hold."
        ),
        tier_provenance="t1_t2_disagree",
    )
```

Both Tier 1 and Tier 2 outputs are recorded in the `decisions` table
with a `tier_provenance` column. We can post-hoc analyze how often
Tier 2 reverses Tier 1 and whether reversals were correct.

### Tier 3: Claude Opus 4.6 — offline evaluator

Not in the live signal path. Two uses:

1. **M2 replay gold-standard labeling.** During M2 replay, we run Opus on every candidate alongside Qwen and record both. Opus's decision is treated as the reference label; we measure Qwen's agreement with Opus and investigate divergences as candidate prompt-improvement targets. Detail in `M2_REPLAY_HARNESS_DESIGN.md`.

2. **Weekly live audit.** Once per week (Sunday), an offline job re-evaluates the prior week's live decisions with Opus on the same recorded context. Where Opus systematically disagrees with Qwen (e.g. Opus says Hold on 80% of Qwen's losing trades), we have a prompt-engineering signal. Output: `docs/reports/weekly_audit_<date>.md`.

Cost: Opus pricing is roughly $15/$75 per MTok input/output. For a
weekly audit of ~12,000 decisions, expect ~$15-30/audit. For an M2
full replay labeling pass, ~$200-400 per 60-day window. Treated as a
fixed cost of evaluating the system, not a per-trade operating cost.

### Cost summary (live operating)

| Tier | Backend | Volume/day | Cost/day |
|---|---|---|---|
| 1 | Qwen 3.6-27B local | 30-200 calls × 78 cycles | ~$0 (electricity) |
| 2 | Sonnet 4.5 (selective) | 5-15 escalations, capped at 25 | ~$0.10-0.30 |
| 3 | Opus 4.6 (weekly audit) | ~12K decisions/audit | ~$2-5 amortized |
| **Total live** | | | **~$2-5/day** |

For comparison: Claude in the hot path on every call would be ~$20/day on Sonnet (with caching) or ~$80-150/day on Opus. The tiered design captures domain expertise where it matters and spends ~95% less.

### Why this structure

Three reasons it's strictly better than Claude-everywhere:

1. **Privacy preserved.** 99%+ of decisions stay on-workstation. Only escalations (rare) leak strategy details to Anthropic.
2. **Determinism preserved.** Tier 1 is fully reproducible across versions; Tier 2 is the small, known fraction that depends on Anthropic version pinning.
3. **Latency bounded.** Tier 1 adds ~3-5s per candidate; Tier 2 adds another ~1s but only on ~5-15 candidates/day, so net cycle time is unchanged.

And three reasons it's strictly better than Qwen-only:

1. **Domain expertise on hard cases.** The 5-15/day escalations are exactly the cases where Qwen's confidence indicates uncertainty AND a real catalyst is present. Claude's stronger financial reasoning is most valuable here.
2. **Calibration anchor.** Tier 3 weekly Opus audits give us a reference standard to detect systematic Qwen biases.
3. **Diversity of error.** When T1 and T2 agree, confidence is better justified. When they disagree, Hold is the safe default.

### What changes if Qwen catches up

If a future Qwen variant (the QwQ reasoning models, or Qwen 3.6 with
its 256K context window enabling longer-form research-report context)
closes the financial-domain reasoning gap, we shrink Tier 2 to
fallback-only and remove the escalation rule. The architecture is
designed so this is a config change (`escalation.enabled: false`),
not a code change.

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

The cost/latency calculus depends on which tier handles the call. See
`docs/HARDWARE_PLATFORM.md` for the workstation analysis and the
"Tiered evaluation" section above for the operational summary. The
tables below describe per-call economics for each backend; the Tier
2 escalation rule (5-15 calls/day) and Tier 3 audit cadence (weekly)
determine how often each is exercised.

### Cloud backend (Anthropic; Tier 2 selective escalation)

| Tier | Input tokens (typical) | Output tokens | $/call | Latency |
|---|---|---|---|---|
| Haiku 4.5 | ~1500 | ~250 | $0.0011 | 400-800ms |
| Sonnet 4.5 | ~1500 | ~250 | $0.0048 | 700-1500ms |

Hypothetical "Sonnet on every candidate" (NOT the chosen architecture; included for comparison): pre-filter at ≤30 candidates/cycle × 78 cycles/day = 2340 calls/day:
- Haiku: $2.57/day, $51/month
- Sonnet: $11.23/day, $225/month

Actual Tier 2 usage (5-15 escalations/day, capped at 25):
- Sonnet: ~$0.10-0.30/day (with prompt caching enabled)
- Haiku: ~$0.02-0.06/day (used only as Tier 2 fallback if Sonnet is rate-limited)

Tier 3 (Opus 4.6 weekly audit): ~$15-30/audit; amortizes to ~$2-5/day.

### Local backend (RTX PRO 5000 Blackwell 48GB; primary)

| Model | 4-bit VRAM | Throughput | $/call | Per-call latency (250 out) |
|---|---|---|---|---|
| **Qwen 3.6-27B (production target)** | ~17GB | ~120-180 tok/s | ~$0 | ~1.5-2.5s |
| Qwen 3.6-35B-A3B (MoE) | ~24GB | ~150+ tok/s | ~$0 | ~1.5-2s |
| Llama 3.3 70B (larger comparison) | ~38GB | 50-80 tok/s | ~$0 | 3-5s |

With local inference at zero marginal cost, the pre-filter exists only
for *quality* reasons (don't run the model on tickers with obviously
no setup) and for *throughput*. At Qwen 3.6-27B speeds with 16-32 way batching,
a 500-call cycle completes in ~20-40s — comfortably within the 300s
cycle budget.

### Implications for the design

1. **Pre-filter from cost-driven (≤30 candidates) → quality-driven (relax to 100-200, or full watchlist if model throughput allows).** The narrower limit was a budget constraint that no longer applies. Initial M2 keeps the conservative pre-filter; M3+ may relax it after measuring whether it costs us setups.

2. **Per-call latency is comparable on Tier 1 local (~1.5-2.5s for Qwen 3.6-27B vs 700ms cloud).** Offset by absence of per-call dollar pressure; we just call concurrently with 16-32 way batching support from LM Studio's API. Tier 2 escalations add ~1-2s on the candidates that fire them, but only ~5-15/day, so net impact on cycle time is minimal.

3. **Cloud backend has two roles, not one.** Tier 2 selective escalation (in-cycle) and Tier 3 weekly audit (offline). Plus a fallback role if LM Studio is offline (workstation down, model unloaded, etc.) — in that mode, Sonnet handles every call rather than just escalations, and we accept the cost for the duration of the outage.

4. **All cost numbers are sensitive to context length.** As we iterate the prompt and add more historical bars or news, input tokens grow. Tier 1 local cost stays $0; Tier 2 and Tier 3 cloud cost scales linearly. This favors longer-context experiments locally; if a context expansion looks promising, we re-measure Tier 2 escalation cost before deploying.

5. **Cloud costs in tables above are uncached baseline.** With prompt caching enabled (next section), actual cloud spend drops by ~60%.

## Prompt caching strategy (cloud backend)

Applies to Tier 2 (Sonnet escalations), Tier 3 (Opus audit + M2 replay
labeling), and the Tier-1-fallback path when LM Studio is offline.
Tier 1 hot-path uses local Qwen, which has no caching primitive but
also no marginal token cost.

Anthropic's `ephemeral` prompt cache reduces cached-input cost to 10% of base rate and avoids re-encoding the prefix on the server. Cache writes cost 1.25x base; TTL is 5 minutes from the last hit. Break-even: ~2 reuses of the same prefix within 5 minutes.

This signal generator is a textbook fit. The per-call input is mostly stable across many calls:

- **Highly stable** (changes only on `prompt_version` bump): system prompt, output schema, few-shot examples → 4-6K tokens
- **Cycle-stable** (refreshes every 5 min when SPY bar arrives): market context (SPY/VIX/regime) → ~150 tokens
- **Per-ticker variable**: fundamentals, daily regime, intraday bars, news, position state, history, time-of-day → 800-1500 tokens

30-200 candidates per cycle all share both stable layers; only the per-ticker block varies.

### Cache layout

Two cache breakpoints, ordered most-stable to least-stable:

```
[cache_control: ephemeral, breakpoint 1]   ← persists across cycles
  System prompt
  Output schema (tool definition)
  Few-shot examples

[cache_control: ephemeral, breakpoint 2]   ← rewritten each 5-min cycle
  Market context (SPY change, VIX, regime label)

[no cache_control — per-ticker, variable]
  Ticker block (fundamentals → daily → intraday → news → position → history → time)
```

Breakpoint 1 keeps hitting until we deploy a new prompt version. Breakpoint 2 is rewritten on the first call of each 5-min cycle and hits for the remaining 29-199 calls in that cycle.

### Expected savings (Sonnet 4.5)

Assumed shape: 5K stable + 0.15K cycle-stable + 1K variable = ~6.15K input, 250 output. 30 candidates × 78 cycles = 2340 calls/day.

| | Without caching | With caching |
|---|---|---|
| Input cost/day | ~$43 | ~$11 (read) + ~$0.07 (write) |
| Output cost/day | ~$8.80 | ~$8.80 |
| **Total/day** | **~$52** | **~$20** |

Savings ~60% on Sonnet. On Haiku the percentage is similar; absolute savings smaller (~$1.50/day). The earlier "Cost & latency model" table used 1500 input tokens as a rough lower bound; the 6K figure here reflects what the v1.0 template actually renders to once the bar table, news block, and prior-decisions block are populated.

**Where this matters most in the tiered architecture:**

- **Tier 1 fallback during LM Studio outages.** This is the only time Anthropic gets called on every candidate. Caching is the difference between a $5 outage and a $50 outage.
- **Tier 3 M2 replay (Opus labeling).** ~140K calls per 60-day window; caching turns ~$3,100 into ~$1,200. Essential for affordable iteration.
- **Tier 3 weekly Opus audit.** ~12K calls share a common prefix; caching saves ~$10/audit.
- **Tier 2 selective escalation.** Small absolute savings (~$0.05/day) since volume is low, but the prompt structure must support caching anyway for the cases above.

### Prompt template reorganization (required)

The v1.0 template starts with `"You are an intraday equity trader evaluating {ticker} at {timestamp_et}."` — that puts variable content at position 1, defeating cache hits. Restructured order (functionally equivalent — same information reaches the LLM):

```
[stable, cacheable — breakpoint 1]
  Role + decision criteria + output schema + few-shot examples

[cycle-stable, cacheable — breakpoint 2]
  # Market context
  SPY today: ...; VIX: ...; Regime: ...

[variable, not cached]
  # Evaluating {ticker} at {timestamp_et}
  [ticker fundamentals → daily → intraday → news → position → history → time]
```

Restructure happens before M3 implementation; locked into `llm_prompt_v1.txt` from the start so cache hits work on the first deployed prompt.

### Replay harness implication

60-day replay × ~30 candidates × 78 cycles ≈ 140K calls. Without caching that's ~$3,100 on Sonnet; with caching ~$1,200. Caching is the difference between affordable and unaffordable prompt iteration during M2.

Replay uses caching by default — the rendered-prompt log is byte-identical to live, so cache breakpoints and TTL behave identically. Bypass caching only when investigating a suspected cache-correctness bug.

### Local backend note

LM Studio exposes no prompt-caching primitive. Local inference has zero marginal token cost, so caching contributes nothing there. The reorganization above is harmless for local inference — one prompt template structure works for both backends.

### What invalidates the cache

- `prompt_version` bump → breakpoint 1 invalidated (rare; deploy boundary)
- New 5-min cycle (SPY bar arrives) → breakpoint 2 rewritten
- 5-min idle (no calls hitting the cache) → TTL expiry, both invalidated
- Model identifier change (e.g. `claude-sonnet-4-5` → `claude-sonnet-4-6`) → both invalidated

We log Anthropic's `cache_creation_input_tokens` and `cache_read_input_tokens` per call into the `decisions` table for cost accounting and to detect cache-miss regressions (e.g. accidental prompt drift that breaks the prefix match).

## Failure modes & fallbacks

| Failure | Detection | Fallback |
|---|---|---|
| Tier 1 (Qwen/LM Studio) down or unreachable | connection refused / timeout | Switch to Tier-1-fallback mode: every candidate goes to Sonnet for the duration of the outage; alert operator |
| Tier 1 schema-invalid output | Pydantic validation fails | Hold(schema_invalid_t1), log raw response, do not escalate |
| Tier 1 latency > 8s | timeout=8000ms | Hold(t1_timeout) |
| Tier 2 escalation timeout | timeout=2000ms | Use Tier 1 result alone, log; do not block cycle |
| Tier 2 schema-invalid output | Pydantic validation fails | Use Tier 1 result alone, log raw response |
| Tier 2 disagreement with Tier 1 | merge_tiers() | Hold(tier_disagreement); both outputs recorded |
| Daily Tier 2 escalation budget exhausted | counter reaches 25 | All remaining candidates use Tier 1 result alone |
| Anthropic API down / 503 (Tier 2) | exception on call | Use Tier 1 result alone, log + alert |
| Anthropic API down / 503 (Tier 1 fallback) | exception on call | Hold(api_failure), log + alert |
| Out-of-range field (confidence=150, stop=10×ATR) | range check after parse | clamp to bounds, proceed |
| Daily Anthropic budget exhausted (across all tiers) | spend tracker | switch to Hold for rest of day, alert |
| Prompt version mismatch in replay | recorded version != current version | use recorded prompt verbatim from DB |
| Tier 3 weekly Opus audit fails | exception in scheduled job | retry next week, alert; live trading unaffected |

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
- Cost tracking (recommendation: log input_tokens, output_tokens, model, cache_creation_input_tokens, cache_read_input_tokens per call; aggregate daily)
- Decision storage schema in SQLite (extends existing `decisions` table with `prompt_version`, `raw_response`, `cost_cents`, `cache_read_tokens`, `cache_write_tokens` columns)
- Prompt caching breakpoint placement (recommendation: two breakpoints as documented in "Prompt caching strategy" — system/schema/few-shot, then market context)
- Tier 2 escalation client (recommendation: separate `AnthropicClient` instance with its own rate limiter and budget tracker, distinct from sentiment-pipeline Haiku client; share connection pool only)
- Decision schema extension for tier provenance (recommendation: add `tier_provenance` enum column: `t1_only` | `t1_t2_agree` | `t1_t2_disagree` | `t1_fallback_t2` | `t1_only_budget_exhausted`)
- Per-tier output storage (recommendation: store `t1_raw_response` always; `t2_raw_response` whenever Tier 2 fires; both are useful for post-hoc analysis even when merge picks one over the other)

## Status

This document is the spec for M2 (replay harness) and M3 (live signal
engine) to build against. Sign-off here means we agree on:

- The input context structure (what gets fed to the LLM)
- The output schema (what the LLM returns)
- The prompt template structure (with the v1.0 template above as the
  starting point — will iterate)
- The pre-filter approach (rule-based candidate selection before LLM call)
- The fallback behavior (Hold on every failure mode)
- The prompt caching strategy (two-breakpoint layout, variable content last)
- The tiered evaluation architecture (Tier 1 Qwen primary, Tier 2 Sonnet selective escalation, Tier 3 Opus offline evaluator)
- The escalation rule (conf 50-75 AND high-quality catalyst AND PM RVOL > 3x AND budget not exhausted)
- The merge logic on Tier 1/Tier 2 disagreement (Hold)

After sign-off, M2 (replay harness) becomes the next concrete deliverable.
