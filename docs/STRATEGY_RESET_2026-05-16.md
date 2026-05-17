# Strategy Reset — Realigning the LLM Model to Workstation-Native Design

**Date:** 2026-05-16
**Trigger:** User feedback that the current architecture is a gap-and-go retrofit, not an LLM-native system. Original spec was "most successful model based on the new computer's specs" with a multi-day strategy and target expected returns >65%.

This document supersedes the prior strategy direction. It is meant to sit alongside `PROJECT_BLUEPRINT.md` as a foundational reference.

---

## 1. Diagnosis of how we got off track

### 1a. The structural cause

The fork started from `trading-platform` as upstream. That codebase is a gap-and-go intraday momentum system. Its core abstractions are 5-min bar evaluation, EOD flatten, bracket orders with intraday-tuned ATR stops, pre-market RVOL gates, and a dynamic watchlist regenerated daily. All of that is gap-and-go's structural skeleton.

When the LLM signal engine was added, it was framed as a *drop-in replacement for the rule-based signal layer*. That framing is structurally wrong for an LLM-native system. It treats the LLM as a smarter version of `gap_and_go.py` rather than as a fundamentally different capability that justifies a different architecture.

### 1b. The interpretive cause

The original instruction was to build the most successful model leveraging the new workstation's hardware specs. That instruction was interpreted narrowly: "run Qwen locally to replace Anthropic API costs on T1." That is one lever among many and not the most important one.

The broader interpretation, which is the correct one, is that workstation-class hardware unlocks classes of strategy that cannot run on a thin client or API-only setup. Continuous local inference, large persistent knowledge bases, GPU-accelerated semantic search, multi-year backtest iteration without API spend, and deep-context document analysis at scale. These are the capabilities that justify the hardware investment. None of them are leveraged by 5-min bar momentum trading.

### 1c. What this means in practice

The current `strategy/llm/` engine is well-engineered code solving the wrong problem. It can be partially salvaged. The risk validation, the bracket execution, the deployment infrastructure, the test framework, the Alpaca integration, and the sentiment pipeline all transfer. The 5-min evaluation loop, the EOD flatten, the gap-and-go watchlist, the pre-market RVOL gates, the on_5min_bar handler, and most of the prompt engineering do not transfer. They have to be rebuilt around a different architecture.

## 2. The new architecture

### 2a. Strategy thesis

Sustained outsized returns come from identifying mispriced narrative shifts before consensus updates. The market reprices catalysts (earnings, guidance changes, M&A speculation, sector rotation, regulatory shifts, management changes, FDA decisions) over days to weeks, not minutes. The edge available to an LLM-driven system is in reading the underlying primary sources (filings, transcripts, earnings calls, analyst notes, news flow) at a depth and breadth that algorithmic systems miss and human analysts cannot match at scale.

This is event-driven multi-day swing trading. It is a genuinely different product from intraday momentum.

### 2b. Time horizon and cadence

| Parameter | Value | Rationale |
|---|---|---|
| Decision cadence | Daily, pre-market (08:00 ET) | Catalyst-driven systems need overnight to digest after-hours news, earnings, filings |
| Optional intraday review | 11:30 ET and 14:30 ET for held positions | Re-evaluate on intraday narrative shifts; not for new entries |
| Holding period | 3-15 days typical | Matches catalyst resolution window |
| Maximum hold | 21 trading days | Hard cap; positions stale past this lose informational edge |
| Universe size | 200-500 names | Liquid US equities with options markets; expanded from intraday's mega-cap focus |

### 2c. The four-layer system

```
Layer 1: Continuous research (24/7 on Godzilla, local Qwen)
  └── Daily filings sweep: 10-Ks, 10-Qs, 8-Ks via SEC EDGAR
  └── Earnings transcripts: Seeking Alpha or AlphaSense scrape
  └── News flow: Polygon + Alpaca + sector-specific RSS
  └── Analyst notes: where available (likely manual feed initially)
  └── Sector rotation signals: relative strength matrices
  └── Output: structured analysis stored in vector DB + relational DB

Layer 2: Daily research loop (08:00 ET, pre-market)
  └── Reads everything Layer 1 produced overnight
  └── Generates candidate scores for each name: catalyst quality,
      narrative shift magnitude, conviction, time-to-event
  └── Cross-references against held positions: any exit triggers?
  └── Outputs ranked watchlist for the session

Layer 3: Decision engine (08:30 ET)
  └── For each candidate above threshold: T2 Sonnet evaluation
      with full context retrieval from Layer 1 knowledge base
  └── For top conviction: T3 Opus review (5-15 names/week max)
  └── Position sizing per conviction + volatility-adjusted Kelly
  └── Output: structured trade decisions with rationales

Layer 4: Execution and management (09:30 ET onward)
  └── Limit orders on entry, scaled in over the first 90 minutes
  └── GTC bracket stops set on fill
  └── Daily mark-to-market and risk check
  └── Exit triggers: stop hit, target hit, catalyst resolved,
      narrative breaks, hold cap reached
```

### 2d. The knowledge base

This is the single most workstation-leveraging component and the one entirely absent from the current architecture.

**Vector database:** Qdrant or Chroma, running locally. Stores embeddings of:

- Every 10-K, 10-Q, 8-K filed by universe companies (decade of history)
- Every earnings call transcript (~5 years history)
- News articles, segmented by company
- Analyst notes where available
- Prior LLM decision rationales (for retrieval-augmented learning)

**Time-series database:** Either Postgres with TimescaleDB extension or QuestDB. Stores:

- Daily bars for universe (decade of history)
- Sentiment scores per company per day
- Fundamental snapshots quarterly
- Sector relative strength
- Macro regime indicators (VIX, breadth, yield curve, sector rotation)

**Relational database:** Postgres. Stores:

- Position history with full decision rationale
- Outcome attribution (what catalyst drove the move)
- Performance attribution (what factor explains returns)
- Configuration history (so prompt version is traceable to outcomes)

**Total disk budget:** Estimate 200-500 GB after first year. Trivial on workstation-class storage.

**Refresh cadence:** Filings sweep nightly, transcript sweep within 24h of release, news flow streaming, fundamentals daily after close.

### 2e. The LLM role

The LLM is doing things it is genuinely good at:

- Reading long primary documents (10-Ks, transcripts) and synthesizing
- Identifying narrative shifts: tone changes in management guidance, new language in risk factors, changes in forward-looking statements
- Cross-referencing against prior decisions: "we passed on this in March because of X; has X changed?"
- Pattern matching across companies: "this is the same setup as Company Y in Q2 2025"
- Generating auditable rationales that a human can read and disagree with

The LLM is not doing:

- 5-min directional prediction
- Single-bar pattern recognition
- High-frequency anything

### 2f. Risk framework

| Parameter | Current intraday | Proposed swing | Rationale |
|---|---|---|---|
| Max position size | 20% | 25% (33% for highest conviction with T3 endorsement) | Concentration needed for return target |
| Max total deployed | 90% | 80% (cash buffer for redeployment) | Multi-day holds reduce opportunity to redeploy intraday |
| Max risk per trade | 2% | 3-4% | Wider stops needed for multi-day volatility; offset by selectivity |
| Stop loss methodology | ATR-based intraday | ATR-based daily, 3-5 day window | Daily ATR captures real swing volatility |
| Max concurrent positions | Variable, intraday only | 5-10 positions | Forces selectivity; matches catalyst flow |
| Sector concentration | None enforced | Max 40% in single sector | Prevents thematic blow-up |
| Earnings blackout | N/A | Hard rule: no new positions within 3 days of earnings unless earnings IS the catalyst | Different play entirely if it is the catalyst |
| Portfolio heat cap | N/A | 12% open risk at session end | Forces position trimming |
| Max drawdown circuit breaker | N/A | 25% peak-to-trough triggers pause and review | Required for psychological + capital preservation |

The proposed framework allows higher concentration than the current one because the trade selection process is more selective. The intraday architecture had to manage many low-conviction trades; the swing architecture takes fewer, higher-conviction positions.

### 2g. Position sizing

Volatility-targeted with conviction multiplier. For each position:

```
base_size = (target_portfolio_vol / position_vol) * equity
conviction_multiplier = f(T2_confidence, T3_endorsement, catalyst_quality)
position_size = min(base_size * conviction_multiplier, max_position_cap)
```

Where `conviction_multiplier` ranges from 0.5 (low conviction passes the threshold but barely) to 2.0 (T3-endorsed highest conviction). Hard cap at 33% for any single position.

This produces concentrated portfolios when conviction is high and diversified portfolios when conviction is spread. It is structurally different from equal-weighting or fixed-fractional sizing.

## 3. What transfers from the current work

| Component | Status | Notes |
|---|---|---|
| `strategy/risk.py validate_order` | Transfers with parameter changes | Logic is correct; the caps change |
| `execution/alpaca_orders.py` | Transfers mostly | Need GTC bracket support added |
| Sentiment pipeline | Transfers with extension | Add filings + transcript ingestion |
| Alpaca integration | Transfers | Same broker, same API |
| Deployment infrastructure | Transfers | `WAVE_DEPLOY_CHECKLIST.md` still applies |
| Test framework | Transfers | New tests needed for new modules |
| LLM client factory | Transfers | `factory.build_tier_clients` is fine |
| `LLMContext` / `LLMDecision` types | Partial transfer | Schema needs catalyst-related fields; reasoning length needs expansion |
| Replay infrastructure | Partial transfer | Replay against daily bars + catalyst events, not 5-min bars |
| Tier 1/2/3 hierarchy | Transfers conceptually | But T1 cadence changes from per-5min-bar to per-research-loop |
| Risk validation | Transfers | |
| EOD report generator | Partial | Needs daily P&L attribution by catalyst |

## 4. What is discarded or fundamentally rebuilt

| Component | Status |
|---|---|
| `on_5min_bar` handler | Discard |
| `data/bar_aggregator.py` (1m→5m) | Discard for live; keep for backtest comparison |
| `data/watchlist_builder.py` (gap-and-go watchlist) | Discard, replace with catalyst-driven candidate generator |
| `data/pm_rvol_thresholds.py` | Discard |
| `strategy/signals/gap_and_go.py` | Discard |
| `strategy/signals/pullback.py` | Discard |
| 15:55 ET flatten | Discard |
| Current prompt set | Rewrite from scratch for daily decision cadence and catalyst framing |
| `analysis/indicators.py` | Keep but de-emphasize; daily indicators (RSI, MACD on daily bars) replace intraday |
| `strategy/llm/policy.py` | Rewrite for new cadence |
| `strategy/llm/context_builder.py` | Rewrite to assemble multi-day context including filings, transcripts, narrative state |

The current `strategy/llm/` codebase is approximately 1,300 lines of work. Estimate 30-40% of it ports forward with adaptation; the rest is replaced.

## 5. What is genuinely new

| Component | Purpose | Approximate scope |
|---|---|---|
| SEC EDGAR filings ingester | Pull 10-K/10-Q/8-K nightly | 1-2 weeks |
| Earnings transcript ingester | Source TBD (AlphaSense API, Seeking Alpha scrape, or manual) | 2-3 weeks |
| Vector database integration | Qdrant/Chroma setup, embedding pipeline | 1 week |
| Time-series database | Postgres+TimescaleDB or QuestDB | 1 week |
| Continuous research daemon | Long-running Qwen process consuming new docs | 2-3 weeks |
| Daily research loop | Pre-market scoring of universe | 2 weeks |
| Catalyst detection logic | Heuristic + LLM-based catalyst classifier | 2-3 weeks |
| Narrative tracking module | Cross-document narrative state per company | 3-4 weeks |
| Multi-day position manager | Holds, scales in, scales out, manages catalysts | 2 weeks |
| Daily attribution reporter | Replaces the 5-min EOD report | 1 week |
| New prompt set | Designed around daily cadence and catalyst framing | 1-2 weeks iterative |
| Backtest harness for daily strategy | Multi-year replay against historical filings + price | 2-3 weeks |

Total new work: roughly 20-30 weeks of focused engineering. Realistic elapsed timeline at 2-3 focused sessions per week: 6-9 months.

## 6. Honest return projection

### 6a. What >65% annual return requires

Three things, all simultaneously:

1. **Concentration.** Equal-weighted 10-stock portfolios with 2% risk per name cannot mathematically produce 65% annual returns. Need to size up on highest-conviction positions (25-33% positions when conviction warrants).
2. **Holding period that matches the catalyst.** Most multi-day catalyst moves do their work in 3-10 days. Selling too early or trading the noise around the move kills expectancy. The strategy has to actually hold.
3. **Real edge.** No architecture produces sustained 65% returns without an underlying inefficiency. The bet here is that deep-context LLM reading of primary sources produces an information advantage relative to consensus that has not yet been arbitraged away. That is a credible bet, given that hedge funds doing this work pay analysts $300k+ each and an LLM can read 100× faster, but it is not a guaranteed bet.

### 6b. Realistic outperformance distribution

The honest metric for an active strategy is alpha versus SPY, not absolute return. Absolute returns are dominated by market beta; the real choice is "this strategy versus buying SPY and going on vacation." Confidence bands for SPY outperformance under the proposed architecture executed cleanly:

| Outperformance threshold | Confidence in a given year |
|---|---|
| Beat SPY by any amount | 55-65% |
| Beat SPY by +5% annualized | 45-55% |
| Beat SPY by +10% annualized | 35-45% |
| Beat SPY by +20% annualized | 20-30% |
| Beat SPY by +30% annualized | 12-20% |
| Beat SPY by +50% annualized | 5-12% |

Reference base rates: active equity managers beat SPY ~40-45% of years and ~10-15% of decades. The numbers above are higher than base rate because the architecture has structural advantages most active managers do not: deep-context LLM reading at a speed and breadth no human analyst can match, no career-risk pressure to crowd into consensus, no fee drag, no client redemption risk forcing exits.

The numbers are roughly independent of what SPY does in absolute terms. Catalyst-driven event strategies have meaningful alpha decoupling from market beta, especially when concentrated.

The original "65% expected returns" target maps approximately to "beat SPY by +50% annualized" in a normal year. Honest confidence on that: 5-12%. The cleaner stretch target the architecture can credibly aim at: "beat SPY by +20% annualized," at 20-30% confidence. That is top-decile active-manager territory and is what the architecture is designed to chase.

Expected maximum drawdown in a normal year: 20-30%.
Expected maximum drawdown in a stress year: 35-50%.

Multi-year compounding heavily favors avoiding bad years. The risk framework (25% circuit breaker, sector caps, hard hold caps, earnings blackouts) is designed to truncate the left tail. That truncation costs some upside in good years and is the right trade for long-term compounding.

### 6c. What the projection depends on

- **Catalyst calendar density.** Earnings season + M&A waves + FDA cycles produce more opportunities than slow quarters. Strategy is regime-dependent.
- **Prompt and calibration quality.** Same risk as current architecture; mitigated by Phase 2-equivalent measurement infrastructure.
- **Data quality.** SEC EDGAR is reliable; earnings transcripts are inconsistent across sources; news flow has noise.
- **Execution costs.** Multi-day strategies are less sensitive to slippage than intraday, but options spreads and limit-order fills still matter.

## 7. The migration plan

### Phase R1 — Architectural decision and scope lock (this conversation forward)

1. Confirm with Neale: is the proposed architecture aligned with the original intent?
2. Lock the scope: which sources (filings only, or also transcripts and news), which universe size, which holding horizon range.
3. Decide what happens to the current `strategy/llm/` work. Recommendation: keep it on a `legacy-intraday` branch for reference; do not delete; do not continue developing.
4. Update `CLAUDE.md` and `PROJECT_BLUEPRINT.md` to reflect the reset.

### Phase R2 — Foundation infrastructure (weeks 1-4)

5. Stand up vector database, time-series database, relational tables.
6. Build SEC EDGAR nightly ingester.
7. Build sentiment + news pipeline extension (or migrate existing).
8. Deploy Qwen on Godzilla via LM Studio. Verify continuous-inference stability.

### Phase R3 — Research loop (weeks 5-10)

9. Build the daily research loop.
10. Write the new prompt set for daily catalyst-driven decisions.
11. Build the catalyst detection classifier.
12. Build narrative tracking.
13. End of Phase R3: a daily research loop produces a ranked watchlist with auditable rationales, even though no trades are happening yet.

### Phase R4 — Backtest (weeks 11-18)

14. Build the daily-bar backtest harness.
15. Multi-year backtest on filings + price data: 2015-2024.
16. Calibration analysis: do LLM confidence scores actually predict outcomes?
17. Walk-forward validation: train on years N-3 to N-1, test on year N, rolling.
18. Decision gate: if backtest shows credible edge, proceed. If not, calibration question reopens.

### Phase R5 — Paper trading (months 5-7)

19. Wire decisions into live execution against Alpaca paper account.
20. 90-day paper window with full measurement infrastructure.
21. Daily reviews; weekly performance attribution.

### Phase R6 — Live deploy decision (month 7+)

22. If paper performance is consistent with backtest, proceed to deploy checklist gates.
23. If paper diverges from backtest, diagnose: is it execution, is it regime, is it overfit.

### Total elapsed: 7-9 months to live-capable, assuming clean phase clears.

## 8. Decisions locked (2026-05-16)

1. **Alignment with original intent:** Confirmed. This proposal is the new direction.
2. **Source scope:** Filings + transcripts + news. Three ingestion pipelines, not one. Budget $200-500/month for data sources. Build order: filings first (SEC EDGAR free), then news (Polygon + Alpaca already in place, extend), then transcripts (paid source TBD in Phase R2).
3. **Current intraday LLM work:** Non-viable. Park on a `legacy-intraday` branch. Do not continue. Do not delete (reference value for risk validation, execution code, test patterns).
4. **Drawdown tolerance:** Accepted. 25-35% normal years, up to 50% in stress. Circuit breaker at 25% peak-to-trough triggers pause and review, not auto-shutdown.
5. **Timeline tolerance:** Accepted. 7-9 months to live-capable.
6. **Performance target framing:** SPY outperformance, not absolute return. Stretch target: beat SPY by +20% annualized (top-decile active-manager territory, 20-30% confidence). Aspirational target: beat SPY by +50% annualized (5-12% confidence). See §6b for full distribution.

## 9. What I am not asking

I am not asking permission to continue the current architecture. The current architecture is the wrong frame and I should have caught that earlier. The reset proposal is the recommendation regardless of what is most convenient relative to the work already done.

I am also not promising 65%+ returns. I am proposing the only architecture I can credibly argue gives 65%+ a credible probability. The probability is real but not guaranteed; the architecture is the bet.
