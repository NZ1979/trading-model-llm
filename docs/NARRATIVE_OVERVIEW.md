# Trading Platform — Narrative Overview

**Last updated:** 2026-05-03 (post-Wave-1A Finnhub Earnings deploy)

## Goal

The platform identifies and executes short-duration intraday equity trades on a 503-symbol S&P 500 watchlist, using news sentiment, price-action indicators, and date-bound catalyst events as signal inputs. It runs paper-only and is deliberately accumulating a clean track record before any consideration of going live.

A "successful trade" is one of two well-defined setups during regular trading hours: a **gap-and-go momentum continuation** in the first 25 minutes after the open (9:35-10:00 ET), or a **pullback reversal** during a trending session. Each trade carries a hard 2% stop loss and is sized for 0.5% portfolio risk per entry, with a 20% per-position cap and 90% total-exposure cap. Every position is flattened at 15:55 ET regardless of outcome — no overnight holds, no swing trades, no exceptions. A daily journal captures every decision the engine made and every order the broker accepted or rejected, written to `journals/<YYYY-MM-DD>.md` at 16:30 ET.

## Current deployment status

The platform runs as `trader.service` (systemd) on a Hetzner CPX21 VPS (Ashburn VA, Ubuntu 24.04). Code lives at `/opt/trader/app/`, the venv at `/opt/trader/.venv/`, and secrets at `/etc/trading-platform/env`. As of this writing the service has been running continuously since 2026-05-03 23:48 UTC, watching 503 symbols on the Alpaca SIP feed, with the news pipelines (Alpaca/Benzinga + Polygon News) polling 24/7.

## Data sources, end-to-end

The platform consumes five external data sources, each with a defined role.

**1. Alpaca SIP WebSocket (Algo Trader Plus, $99/month).** Real-time 1-minute bars for all 503 watchlist symbols, streaming continuously from pre-market through after-hours. The SIP feed is the primary market-data source — every signal evaluation is grounded in bars from this stream after they've been aggregated to 5-minute resolution by `data/bar_aggregator.py`. One Alpaca SIP connection per API key; a task supervisor in `main.py` restarts the stream if it dies.

**2. Alpaca News WebSocket (Benzinga firehose, free).** Headlines tagged with watchlist symbols stream over `wss://stream.data.alpaca.markets/v1beta1/news`. Each headline is enqueued for sentiment scoring; every 60 seconds the queue flushes in a single batched call to Anthropic's Claude Haiku 4.5, which returns an integer sentiment score on a -10 to +10 scale per headline. Burns approximately $5-15 per day in Anthropic credits at the 503-watchlist scale.

**3. Polygon Stocks Starter REST ($29/month).** Historical bars used only for backfill, never for real-time decisions: 300 daily bars per symbol for SMA200 / ATR / regime classification, plus 20 days of pre-market minute bars for the RVOL baseline. Loaded at 8:30 ET each weekday by the daily-routine loop.

**4. Polygon News REST (included in Stocks Starter).** Supplementary sentiment source added 2026-05-03 morning as Track 1. Polls `/v2/reference/news` every 5 minutes via `data/polygon_news.py`; maps Polygon's pre-scored categorical sentiment (positive/negative/neutral) to a conservative integer scale (+4/-4/0); persists to the same `sentiment` SQLite table that latest_sentiment() reads. Covers Benzinga gaps observed in the 2026-05-01 RCA (ORCL, PM, CLX coverage holes) without paying for additional Haiku scoring.

**5. Finnhub Fundamental-1 ($50/month, US market).** Subscribed 2026-05-03. Wave 1A activated the Earnings Calendar endpoint (free tier), which feeds the new catalysts table. Phase 0 plan-availability test confirmed 10 of the 12 highest-priority gap-and-go endpoints work on this tier. Two endpoints returned HTTP 403 (Finnhub blocks them at the auth layer for this tier — not a project decision but a plan limit): **Stock Upgrade/Downgrade** (granular analyst rating events) and **Newsroom** (direct-from-company text for ~1,250 US co's). Both are substituted by accessible endpoints that cover the same strategic shape at lower granularity: Stock Upgrade/Downgrade is replaced by **Recommendation Trends** (buy/hold/sell counts per period instead of individual events; loss is event timing, not signal); Newsroom is replaced by **Major Press Releases** (BusinessWire / AccessWire / GlobeNewswire / Newsfile / PRNewswire — ~85% overlap with Newsroom coverage; loss is the seconds-to-minutes lead time for IR-page-first announcements, which doesn't matter at 5-minute bar granularity). Revisiting a paid-tier upgrade is deferred until 2-4 weeks of paper-trading data shows whether the granularity loss is materially affecting signal quality. Future waves (1B/2A/2B/3) will add News Sentiment, Major Press Releases, Company News, Recommendation Trends, Insider Transactions, Social Sentiment, Basic Financials, Investment Themes, and FDA Calendar.

A Databento ES MBP-10 subsystem was originally part of the design but was canceled 2026-04-28 (Standard tier excludes live MBP-10; Plus tier is $1,500/month, rejected as too expensive for a paper-trading prototype). Wall-detection code is dormant in `analysis/futures_walls.py` and `data/databento_feed.py` but `futures.enabled: false` in config keeps it inert.

## Signal engine end-to-end flow

The most active path through the system is the bar-handling pipeline. Each 1-minute bar from the Alpaca SIP feed is forwarded to a per-symbol bar aggregator that rolls 5 one-minute bars into a single 5-minute bar at every multiple-of-5 minute boundary. Each emitted 5-minute bar runs through these stages:

1. **Bar classification.** The orchestrator decides whether the bar falls inside RTH (9:30 to 16:00 ET) or pre-market. Pre-market bars accumulate in a buffer used to compute the per-symbol pre-market context at 9:30 ET. RTH bars feed the indicator pipeline directly.

2. **Indicators.** The combined PM-plus-RTH dataframe runs through `analysis/indicators.compute_intraday_indicators()`, which filters to RTH-only before computing SMA-20/50, EMA-9, RSI-14, MACD (12/26/9), Bollinger Bands (20, 2σ), ADX-DMI (14), and session-anchored VWAP.

3. **Technical signal.** `analysis/indicators.generate_signal()` evaluates the two strategy paths in priority order. Gap-and-go fires only in the 9:35-10:00 ET window with ≥1% gap, RVOL ≥5x baseline, and price holding above the pre-market low. Pullback fires when daily ADX-14 is above 20 and intraday RSI < 35 with VWAP support and a MACD histogram cross. The function returns a `TechnicalSignal(action, confidence, setup, reasons)`.

4. **Earnings-day veto (NEW, 2026-05-03 Wave 1A).** Before consulting sentiment or walls, `_evaluate_and_execute` checks `is_earnings_day(ticker, today)` against the catalysts table. If `tech.setup == "gap_and_go"` and the ticker has an earnings event today, the signal is silently vetoed and a `[gap_and_go] VETO: earnings on <date>` line is logged. Pullback path is intentionally NOT vetoed (mean-reversion can be valid signal even on earnings days). State dedup ensures the log fires once per state-change per ticker, not per bar.

5. **Sentiment lookup.** `latest_sentiment(db_path, ticker, max_age_sec=86400)` reads the most recent sentiment score from the `sentiment` table within the 24-hour window (widened from 1 hour on 2026-05-03 Phase A patch). Three pipelines write to this table: Benzinga+Haiku, Polygon News (with mapped categorical scoring), and (after Wave 1B) Finnhub News Sentiment.

6. **Decision combiner.** `strategy/signal_engine.evaluate_trade()` combines technical signal + sentiment + futures walls into a `TradeDecision`. Sentiment thresholds are setup-aware: gap-and-go requires ±3 (Polygon's +4 mapping clears it); pullback requires ±5 (Polygon's mapping does NOT clear it, so pullback still depends on Haiku-grade scoring of Benzinga news). Walls are confirming-only since Databento was canceled.

7. **Risk validation.** `strategy/risk.validate_order()` sizes from 0.5% portfolio risk per trade, checks 20% per-position cap and 90% total-exposure cap, computes the 2% stop loss, and approves or rejects.

8. **Order placement.** `execution/alpaca_orders.submit_bracket_order()` submits a one-triggers-other (OTO) bracket: parent limit order at the latest 5-minute close, child stop-loss leg 2% adverse to entry. Multi-status response parsing (Bug E fix from 2026-05-02) ensures per-item failures don't get hidden behind aggregate success counts.

## Sentiment system — multi-source merge

The `sentiment` SQLite table is the canonical store, schema:

```sql
CREATE TABLE sentiment (
    news_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    sentiment INTEGER NOT NULL,
    reasoning TEXT,
    headline TEXT,
    scored_at REAL NOT NULL
);
CREATE INDEX idx_sentiment_ticker_time ON sentiment(ticker, scored_at DESC);
```

The `news_id` PRIMARY KEY is shared across pipelines via namespace separation. Alpaca news_ids occupy the positive integer space. Polygon article IDs are UUID strings hashed via Blake2b-8byte to negative 63-bit integers, with the (article_id, ticker) tuple as the hash key so multi-ticker articles don't collide on the PRIMARY KEY (this fix verified working in production on 2026-05-03 — burst polls show rows-added > articles-fetched, proving multi-ticker articles produce multiple rows). Each pipeline uses INSERT OR IGNORE for idempotency.

`latest_sentiment(db_path, ticker, max_age_sec)` returns the most recent score for a ticker across ALL pipelines, regardless of source. The signal engine doesn't care which pipeline produced the score; it only sees the integer.

**Pipeline cadences and characteristics:**

- **Benzinga via Alpaca News WS + Haiku** — real-time, batched 60-second flush, -10 to +10 granularity, ~$5-15/day spend. Best for pullback (passes the ±5 threshold).
- **Polygon News (5-min poll)** — semi-real-time (Polygon updates ~hourly), categorical 3-class mapped to ±4/0, no per-headline cost. Best for gap-and-go redundancy when Benzinga misses a name.
- **Finnhub News Sentiment (deferred to Wave 1B)** — pre-scored bullish/bearish percent + buzz statistics, US-only, single-call-per-symbol overhead. Will provide a third signal channel.

## Catalyst system (NEW, 2026-05-03 Wave 1A)

A new `catalysts` table holds date-bound events that gate or contextualize trading decisions:

```sql
CREATE TABLE catalysts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    event_date TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    UNIQUE(ticker, event_date, source, event_type)
);
CREATE INDEX idx_catalysts_ticker_date ON catalysts(ticker, event_date);
```

Wave 1A populates this table with `source='finnhub' event_type='earnings'` rows. The 14-day forward window auto-refreshes once daily at 08:30 ET via `_run_finnhub_earnings_refresh` in `main.py`'s daily-routine loop. Critically, the earnings refresh is NOT weekday-gated (separate from `_run_baseline_backfill`) — weekend refreshes keep the table current for Monday's first signal evaluation.

`is_earnings_day(db_path, ticker, date_str)` is a fast catalyst-table lookup used as the gap-and-go veto in the signal engine. Returns False if the table doesn't exist (fail-soft on enrichment data).

The infrastructure is shared. Future waves will add rows from other sources (`source='finnhub' event_type='upgrade'` for Recommendation Trends shifts, `source='finnhub' event_type='fda'` for FDA committee meetings, etc.). The same UNIQUE constraint pattern keeps everything idempotent on replay.

## Indicator suite

Two scopes of indicators run separately.

**Daily (computed once per ticker per day from Polygon's daily bars during the 8:30 ET backfill):**
- Regime classification (bull if close > SMA200 + 0.5%, bear if < SMA200 - 0.5%, neutral otherwise)
- Trending flag (true when daily ADX-14 > 20)

These gate the pullback path: only fires on bull-trending stocks for buys, bear-trending for sells.

**Intraday (computed on every new RTH 5-minute bar):**
- SMA-20, SMA-50, EMA-9
- RSI-14
- MACD (12/26/9), with histogram for the cross signal
- Bollinger Bands (20, 2σ)
- ADX-DMI (14)
- Session-anchored VWAP

The pullback path uses RSI for overbought/oversold, MACD histogram for the cross signal, SMA-20 as trend filter, ADX/DMI for confidence boost (extra points when ADX > 20 and trend-aligned DMI dominates), volume vs trailing 20-bar average for volume-spike bonus, and VWAP as price floor (price must be at or above 99.7% of VWAP).

The gap-and-go path is more streamlined and uses fewer intraday indicators. It reads only the latest close vs the pre-market high/low and the gap percentage from prior daily close. It explicitly does not depend on SMA-50 or longer-warmup indicators, which is what allows it to fire from the very first 5-minute bar after 9:35 ET.

## Daily operational timeline (ET)

| Time | Action |
|---|---|
| 08:30 | **Polygon REST backfill** (weekdays only) — 300 daily bars + 20-day PM volume baselines |
| 08:30 | **Finnhub Earnings Calendar refresh** (every day, including weekends) — 14-day forward window |
| 09:00 | Alpaca SIP + Alpaca News WS connect (already-connected services tolerate restart) |
| 09:30 | PremarketContext computed per symbol from buffered PM bars |
| 09:30-09:35 | Skip the first 5 minutes of RTH (too volatile for either setup) |
| 09:35 | Signal engine begins evaluating; gap-and-go window opens |
| 10:00 | Gap-and-go window closes (continuation thesis no longer applies); pullback continues |
| 15:55 | Flatten all positions (cancel orders first, sleep 500ms, then close — race-fix from 2026-05-02) |
| 16:30 | EOD journal markdown report written to `journals/<YYYY-MM-DD>.md` |
| 24/7 | News pipelines (Alpaca News WS + Polygon News 5-min poll) run continuously |

## Effectiveness assessment of 2026-05-03 additions

Two major changes deployed today. Each is assessed below by what it directly improves, what it costs, and what's been verified vs what remains observation-pending.

### Polygon News supplementary feed (Track 1, deployed morning)

**What it improves.** Adds source diversity for sentiment data. The 2026-05-01 RCA found that Benzinga (via Alpaca News WS) missed coverage on several big-mover tickers (ORCL, PM, CLX) — the signal engine's `latest_sentiment()` returned None for those tickers despite real news driving the moves, so sentiment-gated signals silently no-fired. Polygon's coverage is broad and includes those names. Pre-scored sentiment per ticker means we skip the Haiku scoring step for Polygon-sourced articles, saving API spend.

**What it costs.** No additional subscription cost (included in existing Polygon Stocks Starter). One additional polling task on the asyncio event loop, 5-minute interval, ~10MB/day in HTTP traffic. ~250 lines of new code in `data/polygon_news.py`.

**What's verified (Rule 11: integration-tested in production).** 11.5 hours of continuous polling 2026-05-03 02:37-14:08 UTC — 138 polls fired without a single gap or error. 64 sentiment rows persisted across the period. Multi-ticker collision fix proven: at 06:02 UTC the poll fetched 7 articles → wrote 12 rows (5 articles had multiple ticker insights). Without the fix this would have collided to 7 rows max via the news_id PRIMARY KEY constraint. Idempotent replay confirmed: "1 articles, 0 rows added" non-burst polls show INSERT OR IGNORE working.

**What's monitoring-pending.** Whether the gap-and-go strategy's no-fire rate on big-mover tickers actually drops as a result. Need 1+ week of paper-trading data to compare pre-Polygon-News vs post-Polygon-News no-fire counts on tickers where Polygon scored sentiment but Benzinga didn't. Ratio target: at least 3 trades per week that previously would not have fired now firing.

**Net effect.** Modest positive. Closes a known data-coverage gap with no incremental dollar cost and minor code-complexity cost. Granularity tradeoff is intentional (Polygon's 3-class score clears gap-and-go's ±3 threshold but NOT pullback's ±5, preserving Haiku as the sole signal source for the more-conservative path).

### Finnhub Earnings Calendar veto (Wave 1A, deployed evening)

**What it improves.** Closes a real strategy hole. Gap-and-go on a stock with earnings before/during/after the bar is binary-bet exposure that the strategy isn't designed for. Earnings-day gaps have bimodal payoffs (huge winners or huge losers) that don't fit the continuation thesis. Without an earnings filter, the strategy was implicitly trading those days as if they were normal — a hidden source of variance.

**What it costs.** $50/month Finnhub Fundamental-1 subscription (the full subscription is shared across all current and future waves; Earnings Calendar specifically is on the free tier and would not require the paid plan in isolation). One additional API call per day (~5 minutes after backfill). ~250 lines of new code in `data/finnhub_feed.py` plus 6 surgical edits to `main.py`.

**What's verified (Rule 11: integration-tested in production).** Manual trigger script ran end-to-end against the live API at 2026-05-03 23:28 UTC: 1,500 events fetched for the 14-day window, 15 in the 503-symbol watchlist, 15 new rows persisted. Idempotent replay verified: post-restart auto-refresh fetched the same window, found 0 new rows because the 15 were already present. Sandbox tests passed 7/7 covering rate limiting, schema init, idempotency, and full poll cycle.

**What's monitoring-pending.** First real-world veto event. The 15 catalyst tickers have earnings between 2026-05-04 and 2026-05-17. The first time gap-and-go would have fired on one of those tickers and gets vetoed will produce a `[gap_and_go] VETO: earnings on <date>` log line — that's the validation event we're watching for. Expected frequency: ~3 vetoes per trading day during earnings-heavy weeks, dropping to ~0-1 in calm weeks.

**Net effect.** High positive. Plugs a structural strategy gap with proportionate cost. The veto is conservative by design (only blocks gap-and-go, leaves pullback unaffected), so worst case the platform skips trades it would have taken — never a source of new losses, only avoided ones.

### Combined assessment

The two 2026-05-03 additions move the platform from "single sentiment source + no event filter" to "multi-source sentiment + binary-event filter." Each addition follows the project's CLAUDE_PREFLIGHT principles: fail-loud error logging (Rule 18), dual-window rate limiting on the new Finnhub client, integration-tested before deploy (Rule 11/12), persistence designed for idempotent replay (Rules 19/21), and execution-context labels throughout the deploy commands (Rule 16).

Code-complexity bill for both: ~500 lines of new code, 10 surgical edits to `main.py`, two new SQLite tables. No removed code. No net dependency additions (uses existing `aiohttp` and `sqlite3`).

API spend bill: zero additional (Polygon News is included; Finnhub Earnings Calendar is free tier).

Monthly subscription bill: +$50/month for Finnhub Fundamental-1 (shared with future waves 1B/2A/2B/3 — this Wave 1A is using ~5% of the plan's surface area, so the marginal cost-per-endpoint drops as more waves activate).

## What's deferred and why

- **Databento ES futures walls subsystem** — code dormant since 2026-04-28. Plus tier ($1,500/month) too expensive for paper-trading prototype. Per-stock options-walls successor (Polygon Options Starter, $29/month) deferred until 1+ week of clean paper data.
- **Wave 1B (Finnhub News Sentiment + Major Press Releases + Company News)** — next in queue. Will provide a third sentiment channel and direct catalyst-text source.
- **Wave 2A (Recommendation Trends)** — analyst rating shift catalysts. Stock Upgrade/Downgrade was originally in this wave but is NOT on Fundamental-1 (Phase 0 test confirmed 403); Recommendation Trends covers the same domain at lower granularity.
- **Wave 2B (Insider Transactions + Social Sentiment)** — confidence multipliers, not gates.
- **Wave 3 (Basic Financials + Investment Themes + FDA Calendar)** — context layer.
- **Phase B (dynamic watchlist construction)** — replace the static 503-symbol S&P 500 list with a daily-rebuilt "S&P 500 + NASDAQ + DJI top 500 by 30-day ADV" list.
- **Phase C (per-ticker PM RVOL threshold + sandbox sweep)** — currently the 5x baseline is uniform across all 503 symbols, which biases against mega-caps.

## Open backlog

Tracked in the project task list:

| Task | Status |
|---|---|
| RCA: 2026-05-01 signal engine failures | in_progress (Phase A widening of sentiment_max_age complete; Phase B/C pending) |
| Per-ticker PM RVOL threshold + sandbox sweep | pending (Phase C) |
| Build dynamic watchlist (S&P 500 + NASDAQ + DJI top 500 by 30D ADV) | pending (Phase B) |
| Decide on the original cowork_migration/ folder | pending (housekeeping) |

## Reference

- VPS: 5.161.199.155, user `root`, service `trader.service`, code `/opt/trader/app/`
- Primary docs: `docs/finnhub_api_compiled.md` (full API reference), `docs/finnhub_gap_and_go_evaluation.md` (gap-and-go-specific endpoint scoring), `docs/audits/EMPIRICAL_AUDIT_2026-04-29.md` (post-Bug-A/B audit)
- Per-deploy patches: `docs/patches/<YYYY-MM-DD>-*.md`
- Operational rules and lessons: `CLAUDE_PREFLIGHT.md` (currently 21 rules, last addition 2026-05-03 — never request command output that would expose credentials)
