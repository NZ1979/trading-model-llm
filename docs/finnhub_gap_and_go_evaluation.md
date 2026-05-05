# Finnhub API Evaluation — Gap-and-Go Strategy Fit

**Date:** 2026-05-03
**Source reference:** `docs/finnhub_api_compiled.md` (compiled from `Finnhub_api.html` + `API Documentation.docx`)
**Account:** Finnhub Fundamental-1 plan, $50/month/market, US, 300 calls/min
**Strategy scoped:** Gap-and-go (9:35-10:00 ET window, ≥1% gap, RVOL ≥5x baseline, sentiment ±3, hold-above-PM-low)

## Verification status (2026-05-03)

Top-12 endpoints probed via `scripts/test_finnhub_endpoints.py` against the live API. Results:

- **10/12 PASS (HTTP 200)**: Earnings Calendar, Major Press Releases, Company News, News Sentiment, Recommendation Trends, Insider Transactions, Social Sentiment, Basic Financials, Investment Themes, FDA Committee Calendar
- **2/12 FAIL (HTTP 403, NOT on plan)**: Stock Upgrade/Downgrade, Newsroom

The 2 failures are mitigated:
- Stock Upgrade/Downgrade is replaced by Recommendation Trends (same domain, lower granularity, included)
- Newsroom is replaced by Major Press Releases (overlapping coverage, included)

Net usable scope: 10 endpoints, score-weighted strategic value ≥ 90% of original Top 12.

## Purpose

Score each Finnhub endpoint section by its plausible contribution to gap-and-go signal quality, on a 1-100 scale. Confirm plan inclusion endpoint-by-endpoint. Inform integration ordering for Track 2.

## Scoring scale

- **80-100** — transformative; directly closes a known strategy gap or adds a primary signal channel
- **60-79** — significant edge add; secondary signal or strong filter
- **40-59** — moderate; indirect input or context-only
- **20-39** — marginal; covers an edge case or duplicates existing data
- **1-19** — irrelevant for gap-and-go (wrong asset class, wrong timescale, redundant)

## Plan inclusion convention

- **YES** — explicitly listed as included on Fundamental-1 in the plan-comparison page
- **NO** — explicitly listed as excluded on Fundamental-1
- **UNCONFIRMED** — Premium-gated in the docs but not on either confirmed list. Verify by sample API call before depending on it.

---

# Section-by-Section Analysis

## 1. WebSocket Endpoints (Trades, News, Press Releases)

**Score: 38/100**

Trades WebSocket is free-tier but constrained to one connection per API key — Alpaca SIP already streams 1-min bars for the full 503-symbol watchlist, so adding Finnhub Trades WS forces a swap with no edge gain. News WS is Premium and UNCONFIRMED for Fundamental-1; even if available, it duplicates Benzinga (via Alpaca News WS) and Polygon News (via 5-min polling). Press Releases WS is Enterprise-only and unavailable on this plan.

| Endpoint | Plan inclusion | Notes |
|---|---|---|
| Trades WS | YES (free) | 1 conn/key constraint; Alpaca SIP wins |
| News WS | UNCONFIRMED | Duplicates existing news sources |
| Press Releases WS | NO (Enterprise) | Unavailable |

## 2. Reference Data (Symbol Lookup, Stock Symbol, Market Status, Market Holiday, Country Metadata)

**Score: 32/100**

Operational utility, not a signal source. All free tier and available on Fundamental-1. Market Holiday is the only one with concrete gap-and-go relevance — early-close days (Black Friday, Christmas Eve) distort the 9:35-10:00 ET gap window, so wiring Market Holiday into the schedule loop avoids trading those days as if they were normal sessions.

| Endpoint | Plan inclusion | Notes |
|---|---|---|
| Symbol Lookup | YES | Watchlist construction |
| Stock Symbol | YES | Watchlist construction |
| Market Status | YES | Real-time market open/closed |
| Market Holiday | YES | Wire into schedule loop |
| Country Metadata | YES | Irrelevant for US-only |

## 3. Company Data (10 endpoints)

**Score: 65/100**

Recommendation Trends and Stock Upgrade/Downgrade are the gap-and-go-relevant endpoints — analyst rating shifts are a top-3 common gap catalyst. Basic Financials gives 52-week high/low context for whether a gap is breakout-into-thin-air or technically warranted. Profile/Profile2/Executive/Peers are reference-only.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Company Profile | YES | 40 | Sector/industry classifier |
| Company Profile 2 | YES (free) | 35 | Subset of full profile |
| Company Executive | YES | 25 | Irrelevant intraday |
| Peers | YES (free) | 42 | Peer-relative gap detection |
| Basic Financials | YES | 55 | 52w high/low context |
| Recommendation Trends | YES (verified 2026-05-03) | 78 | Top-3 gap catalyst |
| Price Target | UNCONFIRMED | 60 | Catalyst proxy |
| Stock Upgrade/Downgrade | NO (verified 2026-05-03) | 75 | Not on Fundamental-1 |
| Historical Market Cap | NO | — | Fundamental 2 / All-in-One only |
| Historical Employee Count | NO | — | Fundamental 2 / All-in-One only |

## 4. News & Sentiment (5 endpoints)

**Score: 88/100** — highest section after Calendar.

This is the section that most directly improves gap-and-go's sentiment-gating signal. Major Press Releases sources from BusinessWire, GlobeNewswire, AccessWire, Newsfile, PRNewswire — these wires announce the events that drive most gaps (earnings, M&A, FDA, contracts, capital raises). Company News with 3-year history adds catalyst-text source diversity beyond Benzinga and Polygon News, plus enables historical backtest validation. News Sentiment is a direct alternative to current Polygon-News-categorical mapping — pre-scored bullish/bearish percent + buzz statistics.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Market News | YES (free) | 25 | Too broad (general/forex/crypto/merger) |
| Company News | YES (3y + RT) | 85 | Source diversity + backtest history |
| Major Press Releases | YES | 88 | Catalyst wires |
| Newsroom | NO (verified 2026-05-03) | 70 | Not on Fundamental-1 |
| News Sentiment | YES | 82 | Pre-scored bullish/bearish percent |

## 5. Filings & Fundamentals (10 endpoints)

**Score: 30/100**

Strong for swing/value strategies, weak for intraday gap-and-go. SEC Filings could be wired as a catalyst classifier (8-K → "is this gap from an 8-K filing?") but you can already reach the same data via Alpaca News and SEC EDGAR. Sentiment Analysis and Similarity Index are quarterly cadence — too slow for intraday signals. Global Filings Search is interesting for offline catalyst archaeology, not real-time.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Financial Statements | YES (10y/40q) | 30 | Quarterly cadence |
| Financials As Reported | YES | 28 | Quarterly cadence |
| Revenue Breakdown | UNCONFIRMED | 22 | Slow |
| Revenue Breakdown & KPI | UNCONFIRMED | 25 | Slow |
| SEC Filings | YES | 28 | Already via Alpaca News + EDGAR |
| SEC Sentiment Analysis | YES | 25 | Quarterly |
| Similarity Index | UNCONFIRMED | 20 | Quarterly |
| International Filings | NO (case-by-case) | — | Not on plan default |
| Global Filings Search | UNCONFIRMED | 25 | Offline research only |
| Search In Filing / Filter / Download | UNCONFIRMED | 20 | Power-user research |

## 6. Ownership & Insiders (6 endpoints)

**Score: 50/100**

Most ownership endpoints are quarterly 13F data — too slow for intraday gap-and-go. Insider Transactions is the standout: Form 4 filings are next-day-ish, and a cluster of insider buys before a gap-up is a high-conviction confirmation signal (or a fade signal if sells precede a gap-up). Use as a confidence multiplier on gap-and-go signals, not a primary gate.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Ownership | YES | 25 | Quarterly 13F |
| Fund Ownership | UNCONFIRMED | 22 | Quarterly |
| Institutional Profile | UNCONFIRMED | 18 | 60+ profiles, slow |
| Institutional Portfolio | UNCONFIRMED | 22 | Quarterly |
| Institutional Ownership | UNCONFIRMED | 22 | Quarterly |
| Insider Transactions | YES | 65 | Confidence multiplier |
| Insider Sentiment | YES (free) | 50 | Pre-aggregated MSPR, monthly |

## 7. Calendar (IPO, Earnings, Economic)

**Score: 92/100** — single highest-value section.

The Earnings Calendar endpoint plugs the most glaring open hole in the current signal engine: no automated earnings filter. Right now gap-and-go can fire on a stock with earnings before/during/after the bar, which is binary-bet exposure the strategy isn't designed for. The Earnings Calendar fix is a hard veto: if `earningsCalendar` shows AAPL has earnings today (`bmo`/`amc`/`dmh`), skip gap-and-go for AAPL. Free tier covers 1 month historical + new updates, which is enough for daily refresh. IPO Calendar is for a different pattern (IPO pop-and-fade). Economic Calendar handles macro days but is Premium and UNCONFIRMED.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| IPO Calendar | YES (free) | 35 | Different pattern |
| Earnings Calendar | YES (free) | 92 | Hard veto on earnings days |
| Economic Calendar | UNCONFIRMED | 35 | Macro-day filter |

## 8. Estimates (8 endpoints — Revenue, EPS, EBITDA, EBIT, Net Income, Pretax, Gross, DPS)

**Score: 35/100**

All 8 are Premium Access Required. UNCONFIRMED for Fundamental-1 (estimates are typically a separate-tier in Finnhub's structure). Useful for swing/event trades but weak for intraday gap-and-go. Earnings Surprises (separate endpoint, in next section) gives the same surprise-vs-estimate signal more directly.

All UNCONFIRMED. All score ~35 if available.

## 9. Real-time / Pricing (Quote, Candles, Tick, NBBO, Bid-Ask, Splits, Dividends, Sector Metrics, Price Metrics, Symbol Change, ISIN Change)

**Score: 28/100**

Mostly redundant with current data sources. Quote duplicates Alpaca SIP. Stock Candles duplicates Polygon. Tick/NBBO/Bid-Ask are too granular for 5-min gap-and-go bar logic. The valuable picks here are narrow: Dividends (skip ex-div day gaps, since dividend-induced gaps shouldn't trigger gap-and-go) and Symbol Change (catches ticker changes that would otherwise look like a "new" gap).

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Quote | YES (free) | 12 | Alpaca SIP duplicate |
| Stock Candles | UNCONFIRMED | 10 | Polygon duplicate |
| Tick Data | UNCONFIRMED | 8 | Too granular |
| Historical NBBO | UNCONFIRMED | 8 | Too granular |
| Last Bid-Ask | UNCONFIRMED | 12 | Too granular |
| Splits | UNCONFIRMED | 35 | Alpaca handles split-adj |
| Dividends | YES | 30 | Skip ex-div gaps |
| Dividends 2 (global) | UNCONFIRMED | 5 | Wrong region |
| Sector Metrics | UNCONFIRMED | 38 | Sector-relative gap |
| Price Metrics | UNCONFIRMED | 48 | 52w high/low + YTD |
| Symbol Change | YES | 30 | Avoid false "new" gaps |
| ISIN Change | YES | 18 | EU only |

## 10. Indices (Constituents, Historical Constituents)

**Score: 35/100**

Useful for Phase B (dynamic watchlist construction) — programmatic source of S&P 500 / NASDAQ / DJI membership. Both Premium, both UNCONFIRMED for Fundamental-1. Wikipedia + manual curation is the fallback if not available.

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Indices Constituents | UNCONFIRMED | 35 | Phase B watchlist |
| Indices Historical Constituents | UNCONFIRMED | 30 | Backtest accuracy |

## 11. ETFs (5 endpoints)

**Score: 22/100**

All Premium, all UNCONFIRMED. Sector exposure / country exposure could give "is the sector gapping?" context, but Peers (free) and Sector Metrics already provide similar signal more directly. Skip.

## 12. Mutual Funds (6 endpoints)

**Score: 1/100** — wrong asset class. Skip entirely.

## 13. Bonds (4 endpoints)

**Score: 1/100** — wrong asset class. Skip entirely.

## 14. Forex (5 endpoints)

**Score: 1/100** — wrong asset class. Skip entirely.

## 15. Crypto (4 endpoints)

**Score: 1/100** — wrong asset class. Skip entirely.

## 16. Technical Analysis (Pattern Recognition, Support/Resistance, Aggregate Indicators, Technical Indicators)

**Score: 18/100**

All Premium, all UNCONFIRMED. Pattern Recognition + Support/Resistance are useful for the *pullback* path, not gap-and-go. Aggregate/Technical Indicators duplicate `analysis/indicators.py`, which already runs SMA, EMA, RSI, MACD, BBANDS, ATR, ADX, VWAP locally without API calls. Local computation is cheaper, faster, more controllable. Skip.

## 17. Earnings Calls (Transcripts List, Transcripts, Audio Live, Company Presentation)

**Score: 22/100**

All Premium, all UNCONFIRMED. Transcripts and Audio are post-call — by the time text is processed, the gap is already happening. Possible Phase 8 input if a swing-overlay layer is ever added.

## 18. Alternative Data (Social Sentiment, Investment Themes, Supply Chain, ESG, Earnings Quality, AI Copilot)

**Score: 50/100**

Social Sentiment is the standout. Reddit + Twitter mentions/sentiment serves as a meme-stock detector — gap-and-go on GME/AMC-pattern names is a known fade, and Social Sentiment's score field can act as a negative gate (extreme retail buzz → skip gap-and-go).

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Social Sentiment | YES | 62 | Meme-stock filter |
| Investment Themes | YES | 50 | Theme rotation context |
| Supply Chain | NO | — | Excluded |
| Company ESG | NO | — | Excluded |
| Historical ESG | NO | — | Excluded |
| Earnings Quality | NO | — | Excluded |
| AI Copilot | UNCONFIRMED | 5 | Interactive only, not for production |

## 19. Government & Public-Records (USPTO, H1-B, Senate Lobbying, USA Spending, Congressional Trading, Bank Branch, FDA Calendar)

**Score: 30/100**

Mostly slow alpha sources. FDA Committee Meeting Calendar is the only actionable real-time-relevant one — biotech-specific catalyst calendar. If/when biotech enters the watchlist (currently the watchlist is mega-cap S&P, no biotech), this is the binary-event filter you need (bio names gap on FDA outcomes).

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| USPTO Patents | YES | 25 | Patent grants, occasional |
| H1-B Visa Application | YES | 8 | Quarterly DOL, slow |
| Senate Lobbying | YES | 12 | Slow / political |
| USA Spending | YES | 10 | Government contracts, lagged |
| Congressional Trading | YES | 22 | 30-45d lagged |
| Bank Branch List | YES | 5 | Irrelevant |
| FDA Committee Calendar | YES (free) | 48 | Biotech catalysts |

## 20. Economic Data (Economic Calendar, Economic Code, Economic Data)

**Score: 25/100**

Macro context, not single-name catalysts. Macro events (CPI, NFP, FOMC) drive index-wide moves that distort single-name gap behavior; a light wire-in could dampen gap-and-go signals on macro-event days. Economic Code/Data are likely on Fundamental-1 (the docs say "Accessible with Fundamental data" which matches plan name).

| Endpoint | Plan inclusion | Score | Notes |
|---|---|---|---|
| Economic Calendar | UNCONFIRMED | 35 | Macro-day filter |
| Economic Code | likely YES (Fundamental data) | 15 | Slow macro indices |
| Economic Data | likely YES (Fundamental data) | 15 | Slow macro |

---

# Top 12 Endpoints — Ranked

| Rank | Endpoint | Score | Plan inclusion |
|---|---|---|---|
| 1 | Earnings Calendar | 92 | YES (free) |
| 2 | Major Press Releases | 88 | YES |
| 3 | Company News (3y + real-time) | 85 | YES |
| 4 | News Sentiment | 82 | YES |
| 5 | Recommendation Trends | 78 | **YES (verified 2026-05-03)** |
| 6 | Stock Upgrade/Downgrade | 75 | **NO (verified 2026-05-03)** |
| 7 | Newsroom (1,250 US co's) | 70 | **NO (verified 2026-05-03)** |
| 8 | Insider Transactions | 65 | YES |
| 9 | Social Sentiment | 62 | YES |
| 10 | Basic Financials (52w high/low) | 55 | YES |
| 11 | Investment Themes | 50 | YES |
| 12 | FDA Committee Calendar | 48 | YES (free) |

---

# Plan Inclusion Summary

**CONFIRMED YES on Fundamental-1, US** (per plan-comparison page + 2026-05-03 live test):
Company Profile v2 · Company Executives · Standardized Financial Statements (10y, 40q) · Financials As Reported · Dividends (10y) · SEC Filings · SEC Filings Sentiment · Ownership / Insider Transactions · Company News (3y + real-time) · News Sentiment · Basic Financials · Peers · Press Releases · Ticker/ISIN Changes · Bank Branch · Investment Themes · USPTO Patents · H1-B / Visa Applications · Senate Lobbying · Congressional Trading · USA Spending · Social Sentiment · **Recommendation Trends (verified 2026-05-03)**

**CONFIRMED NO on Fundamental-1** (per plan-comparison page + 2026-05-03 live test):
Historical Market Cap · Historical Employee Count · Supply Chain Relationships · Company ESG Scores · Historical ESG Scores · Company Earnings Quality Score · **Stock Upgrade/Downgrade (verified 2026-05-03 — HTTP 403)** · **Newsroom (verified 2026-05-03 — HTTP 403)**

**UNCONFIRMED** (Premium-gated in docs but not yet probed):
Estimates (Revenue, EPS, EBITDA, EBIT, Net Income, Pretax, Gross, DPS — 8 endpoints) · Price Target · News WS · Quote-tier real-time data (Stock Candles, Tick, NBBO, Bid-Ask) · Splits · Dividends 2 · Sector Metrics · Price Metrics · Indices Constituents (×2) · all 5 ETF endpoints · all 6 Mutual Fund endpoints · all 4 Bond endpoints · Forex Candles · Forex Rates · Crypto Profile · Crypto Candles · all 4 Technical Analysis endpoints · all 4 Earnings Call endpoints · AI Copilot · Economic Calendar · Revenue Breakdown / Revenue Breakdown & KPI · Similarity Index · Fund Ownership · Institutional Profile / Portfolio / Ownership · Global Filings Search and related (Search In Filing, Filter, Download Filings, International Filings)

Verify UNCONFIRMED endpoints by sample API call before depending on them in production code.

---

# Decision Recommendations

**If integrating one endpoint:** Earnings Calendar. Free tier, plugs a real strategy hole (no current earnings-day filter), implementation ~30 lines.

**If integrating three:** add Major Press Releases and News Sentiment for sentiment-source diversification beyond Benzinga + Polygon News.

**If integrating the top 12 (score 48+):** stage in 5 waves over ~7 days of work:

| Wave | Endpoints | Rationale |
|---|---|---|
| 1A | Earnings Calendar | Single highest-impact, free tier |
| 1B | News Sentiment, Major Press Releases, Company News | Sentiment diversification |
| 2A | Recommendation Trends, Stock Upgrade/Downgrade | Catalyst classification (conditional on UNCONFIRMED → YES) |
| 2B | Insider Transactions, Newsroom, Social Sentiment | Confidence multipliers |
| 3 | Basic Financials, Investment Themes, FDA Calendar | Context layer |

Each wave: code → sandbox test → deploy to VPS → 24-48h soak → next wave.

# Out of scope for gap-and-go

Skip entirely (score ≤22 across the board, or wrong asset class):
Mutual Funds (6) · Bonds (4) · Forex (5) · Crypto (4) · ETFs (5) · Technical Analysis suite (4) · Earnings Calls (4) · most Estimates · most real-time pricing duplicates · most Government records · all ESG · Supply Chain · Earnings Quality · AI Copilot

That's ~50 endpoints excluded by design — Finnhub's full surface area is broad, but a focused intraday equity strategy needs ~12 of them at most.
