# Finnhub API — Compiled Reference

**Source files (cross-verified, both integrity-complete):**
- `Trading.Base.1/docs/Finnhub_api.html` — MHTML capture of Finnhub's live docs SPA, 193,783 chars decoded
- `Trading.Base.1/docs/API Documentation.docx` — text capture, 189,970 chars

**Compiled:** 2026-05-03. Every endpoint below was located in BOTH source files. Premium gating phrasing is verbatim from the docs.

## Account context (this project)

- **Plan:** Fundamental-1, $50/month/market, billed quarterly
- **Market:** US
- **Rate limit:** 300 API calls/minute (plan tier) + global 30 calls/second hard cap
- **Auth:** API key as `?token=APIKEY` query param OR `X-Finnhub-Token: APIKEY` HTTP header
- **Base URL:** `https://finnhub.io/api/v1`
- **WebSocket URL:** `wss://ws.finnhub.io?token=APIKEY` (one connection per API key)
- **HTTP 429 = rate limit exceeded**

### Plan inclusions (per the Fundamental-1 plan-comparison screenshot)

**Included on Fundamental-1, US:**
Company Profile v2 · Company Executives · Standardized Financial Statements (10y, 40q) · Financials As Reported · Dividends (10y) · SEC Filings · SEC Filings Sentiment · Ownership / Insider Transactions · Company News (3y + real-time) · News Sentiment · Basic Financials · Peers · Press Releases · Ticker/ISIN Changes · Bank Branch · Investment Themes · USPTO Patents · H1-B / Visa Applications · Senate Lobbying · Congressional Trading · USA Spending · Social Sentiment

**NOT included on Fundamental-1 (would need higher tier):**
Historical Market Cap · Historical Employee Count · Supply Chain Relationships · ESG Scores · Company Earnings Quality

For any endpoint not on either of those lists, treat its tier as not yet confirmed for this account — verify on the Finnhub dashboard before integrating.

## Official client libraries
Python (`finnhub-python`), Go, JavaScript (NPM), Ruby, Kotlin, PHP. Python install: `pip install finnhub-python`.

---

# WebSocket Endpoints

### Trades — Last Price Updates (WebSocket)
- **Method:** Websocket
- **URL:** `wss://ws.finnhub.io?token=APIKEY`
- **Premium:** none (free tier supports US stocks; FX/crypto coverage varies)
- **Coverage:** US stocks, forex, crypto. Not all FX brokers stream (FXCM, Forex.com, FHFX excluded — use Forex Candles or All Rates instead).
- **Limits:** 1 connection per API key. Trade messages may bundle multiple trades.
- **Subscribe:** `{"type":"subscribe","symbol":"AAPL"}`
- **Response message:** `{type:"trade", data:[{s:symbol, p:price, t:UNIX_ms, v:volume, c:[trade_conditions]}]}`

### News (WebSocket)
- **Method:** Websocket
- **URL:** `wss://ws.finnhub.io?token=APIKEY`
- **Premium:** Premium Access Required
- **Coverage:** US and Canadian stocks
- **Subscribe:** `{"type":"subscribe-news","symbol":"AAPL"}`
- **Response message:** `{type:"news", data:[{category, datetime, headline, urlId, image, related, source, summary, url}]}`

### Press Releases (WebSocket)
- **Method:** Websocket
- **URL:** `wss://ws.finnhub.io?token=APIKEY`
- **Premium:** Enterprise users only
- **Coverage:** Global companies
- **Subscribe:** `{"type":"subscribe-pr","symbol":"AAPL"}`
- **Response message:** `{type:"pr", data:[{datetime, headline, symbol, fullText, url}]}`

---

# Reference Data

### Symbol Lookup
- **Method:** GET `/search?q=<query>&exchange=<exchange>`
- **Premium:** none
- **Args:** `q` REQUIRED (symbol/name/ISIN/CUSIP), `exchange` optional
- **Response:** `count`, `result[]` with `description`, `displaySymbol`, `symbol`, `type`

### Stock Symbol
- **Method:** GET `/stock/symbol?exchange=<exchange>&mic=<mic>&securityType=<type>&currency=<ccy>`
- **Premium:** none
- **Args:** `exchange` REQUIRED; `mic`, `securityType`, `currency` optional
- **Response:** array of `{currency, description, displaySymbol, figi, isin, mic, shareClassFIGI, symbol, symbol2, type}`

### Market Status
- **Method:** GET `/stock/market-status?exchange=<exchange>`
- **Premium:** none
- **Args:** `exchange` REQUIRED
- **Response:** `{exchange, holiday, isOpen, session (pre-market/regular/post-market/null), t, timezone}`

### Market Holiday
- **Method:** GET `/stock/market-holiday?exchange=<exchange>`
- **Premium:** none
- **Args:** `exchange` REQUIRED
- **Response:** `data[]` of `{atDate, eventName, tradingHour}`; plus `exchange`, `timezone`

### Country Metadata
- **Method:** GET `/country`
- **Premium:** none
- **Args:** none
- **Response:** array of `{code2, code3, codeNo, country, countryRiskPremium, currency, currencyCode, defaultSpread, equityRiskPremium, rating, region, subRegion}`

---

# Company Data

### Company Profile (full)
- **Method:** GET `/stock/profile?symbol=<sym>` or `?isin=` or `?cusip=`
- **Premium:** Premium Access Required
- **Args:** at least one of `symbol`, `isin`, `cusip`
- **Response:** `address, alias, city, country, currency, cusip, description, employeeTotal, estimateCurrency, exchange, finnhubIndustry, ggroup, gind, gsector, gsubind, ipo, irUrl, isin, lei, logo, marketCapCurrency, marketCapitalization, naics, naicsNationalIndustry, naicsSector, naicsSubsector, name, phone, sedol, shareOutstanding, state, ticker, weburl`

### Company Profile 2 (free version)
- **Method:** GET `/stock/profile2?symbol=<sym>` or `?isin=` or `?cusip=`
- **Premium:** none
- **Args:** at least one of `symbol`, `isin`, `cusip`
- **Response:** `country, currency, exchange, finnhubIndustry, ipo, logo, marketCapitalization, name, phone, shareOutstanding, ticker, weburl`

### Company Executive
- **Method:** GET `/stock/executive?symbol=<sym>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Args:** `symbol` REQUIRED
- **Response:** `executive[]` with `{age, compensation, currency, name, position, sex, since, title}`; plus `symbol`

### Peers
- **Method:** GET `/stock/peers?symbol=<sym>&grouping=<sector|industry|subIndustry>`
- **Premium:** none (included on Fundamental-1)
- **Args:** `symbol` REQUIRED; `grouping` optional (default `subIndustry`)
- **Response:** array of peer symbols (strings)

### Basic Financials
- **Method:** GET `/stock/metric?symbol=<sym>&metric=all`
- **Premium:** none (included on Fundamental-1)
- **Args:** `symbol` REQUIRED, `metric` REQUIRED (value: `all`)
- **Response:** `{metric (key/value), metricType, series (time-series ratios), symbol}`

### Recommendation Trends
- **Method:** GET `/stock/recommendation?symbol=<sym>`
- **Premium:** none
- **Args:** `symbol` REQUIRED
- **Response:** array of `{buy, hold, period, sell, strongBuy, strongSell, symbol}`

### Price Target
- **Method:** GET `/stock/price-target?symbol=<sym>`
- **Premium:** Premium required
- **Args:** `symbol` REQUIRED
- **Response:** `{lastUpdated, numberAnalysts, symbol, targetHigh, targetLow, targetMean, targetMedian}`

### Stock Upgrade/Downgrade
- **Method:** GET `/stock/upgrade-downgrade?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium Access Required
- **Args:** `symbol` optional (blank = latest market-wide), `from`/`to` optional
- **Response:** array of `{action (up/down/main/init/reit), company, fromGrade, gradeTime, symbol, toGrade}`

### Historical Market Cap
- **Method:** GET `/stock/historical-market-cap?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Accessible with Fundamental 2 or All-in-One (NOT Fundamental-1)
- **Args:** `symbol`, `from`, `to` all REQUIRED
- **Response:** `{currency, data[] of {atDate, marketCapitalization}, symbol}`

### Historical Employee Count
- **Method:** GET `/stock/historical-employee-count?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Accessible with Fundamental 2 or All-in-One (NOT Fundamental-1)
- **Args:** `symbol`, `from`, `to` all REQUIRED
- **Response:** `{data[] of {atDate, employee}, symbol}`

---

# News & Sentiment

### Market News
- **Method:** GET `/news?category=<cat>&minId=<id>`
- **Premium:** none
- **Args:** `category` REQUIRED (`general`, `forex`, `crypto`, `merger`); `minId` optional
- **Response:** array of `{category, datetime, headline, id, image, related, source, summary, url}`

### Company News
- **Method:** GET `/company-news?symbol=<sym>&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>`
- **Premium:** Free Tier covers 1 year of historical news + new updates (Fundamental-1 plan extends to 3 years per the plan screenshot)
- **Coverage:** North American companies only
- **Args:** `symbol`, `from`, `to` all REQUIRED
- **Response:** array of `{category, datetime, headline, id, image, related, source, summary, url}`

### Major Press Releases
- **Method:** GET `/press-releases?symbol=<sym>&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>`
- **Premium:** Premium Access Required (included on Fundamental-1). Full-text data is Enterprise only.
- **Sources:** exchanges, BusinessWire, AccessWire, GlobeNewswire, Newsfile, PRNewswire
- **Args:** `symbol` REQUIRED; `from`, `to` optional
- **Response:** `{majorDevelopment[] of {datetime (YYYY-MM-DD HH:MM:SS), description, headline, symbol, url}, symbol}`

### Newsroom
- **Method:** GET `/stock/newsroom?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium Access Required
- **Coverage:** 1,250 US companies
- **Args:** `symbol` REQUIRED; `from`, `to` optional
- **Response:** `{data[] of {atDate (EST), fullText, title, url}, symbol}`

### News Sentiment
- **Method:** GET `/news-sentiment?symbol=<sym>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Coverage:** US companies only
- **Args:** `symbol` REQUIRED
- **Response:** `{buzz: {articlesInLastWeek, buzz, weeklyAverage}, companyNewsScore, sectorAverageBullishPercent, sectorAverageNewsScore, sentiment: {bearishPercent, bullishPercent}, symbol}`

---

# Filings & Fundamentals

### Financial Statements (standardized)
- **Method:** GET `/stock/financials?symbol=<sym>&statement=<bs|ic|cf>&freq=<annual|quarterly|ttm|ytd>&preliminary=<bool>`
- **Premium:** Premium Access Required (included on Fundamental-1: 10y annual, 40q quarterly)
- **Notes:** TTM available for IC + CF only. YTD available for CF only. `preliminary=true` returns prelim US data within ~1 hour of earnings announcements (look for `"preliminary": true` in payload).
- **Args:** `symbol`, `statement`, `freq` all REQUIRED; `preliminary` optional
- **Response:** `{financials[] (key/value per period), symbol}`

### Financials As Reported
- **Method:** GET `/stock/financials-reported?symbol=<sym>&cik=<cik>&accessNumber=<n>&freq=<annual|quarterly>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1; bulk Kaggle dataset also available)
- **Args:** all optional but at least one identifier needed
- **Response:** `{cik, data[] of {acceptedDate, accessNumber, cik, endDate, filedDate, form, quarter, report, startDate, symbol, year}, symbol}`

### Revenue Breakdown
- **Method:** GET `/stock/revenue-breakdown?symbol=<sym>&cik=<cik>`
- **Premium:** Premium (US companies that disclose breakdown). Global standardized data is Enterprise.
- **Args:** `symbol` or `cik`
- **Response:** `{cik, data[] of {accessNumber, breakdown}, symbol}`

### Revenue Breakdown & KPI (standardized)
- **Method:** GET `/stock/revenue-breakdown2?symbol=<sym>`
- **Premium:** Premium
- **Coverage:** 30,000+ global companies
- **Args:** `symbol` REQUIRED
- **Response:** `{currency, data, symbol}`

### SEC Filings
- **Method:** GET `/stock/filings?symbol=<sym>&cik=<cik>&accessNumber=<n>&form=<f>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1; bulk Kaggle dataset available). Limit 250 docs/call. `form=NT 10-K` finds non-timely filings.
- **Args:** all optional (blank lists latest)
- **Response:** array of `{acceptedDate, accessNumber, cik, filedDate, filingUrl, form, reportUrl, symbol}`

### SEC Sentiment Analysis (10-K, 10-Q)
- **Method:** GET `/stock/filings-sentiment?accessNumber=<n>`
- **Premium:** Premium Access Required (included on Fundamental-1). Uses Loughran & McDonald word lists.
- **Args:** `accessNumber` REQUIRED
- **Response:** `{accessNumber, cik, sentiment: {constraining, litigious, modal-moderate, modal-strong, modal-weak, negative, polarity, positive, uncertainty}, symbol}` (each is % of words in filing)

### Similarity Index (10-K, 10-Q)
- **Method:** GET `/stock/similarity-index?symbol=<sym>&cik=<cik>&freq=<annual|quarterly>`
- **Premium:** Premium Access Required
- **Args:** `symbol` or `cik`; `freq` optional (default `annual`)
- **Response:** `{cik, similarity[] of {acceptedDate, accessNumber, cik, filedDate, filingUrl, form, item1, item1a, item2, item7, item7a, reportUrl, symbol}}` (cosine similarity of each section vs prior year)

### International Filings
- **Method:** GET `/stock/international-filings?symbol=<sym>&country=<XX>&from=<date>&to=<date>`
- **Premium:** Access approved case-by-case. Limit 500 docs/call.
- **Args:** all optional
- **Response:** array of `{category, companyName, country, description, filedDate, language, symbol, title, url}`

### Global Filings Search
- **Method:** POST `/global-filings/search`
- **Premium:** Premium Access Required
- **Payload:** `query` REQUIRED; many optional filters: `acts, caps, chIds, ciks, countries, cusips, exchanges, exhibits, forms, fromDate, gics, highlighted, isins, naics, page, sedarIds, sedols, sort, sources, symbols, toDate`
- **Response:** `{count, filings[] of {acceptanceDate, amend, documentCount, filedDate, filerId, filingId, form, name, pageCount, reportDate, source, symbol, title}, page, took}`

### Search In Filing
- **Method:** POST `/global-filings/search-in-filing`
- **Premium:** Premium Access Required
- **Payload:** `filingId`, `query` REQUIRED
- **Response:** filing detail with `documents[]` containing `excerpts[]` (highlighted snippets with start/end offsets)

### Search Filter
- **Method:** GET `/global-filings/filter?field=<field>&source=<src>`
- **Premium:** Premium Access Required
- **Args:** `field` REQUIRED (`countries`, `exchanges`, `exhibits`, `forms`, `gics`, `naics`, `caps`, `acts`, `sort`); `source` optional
- **Response:** array of `{id, name}`

### Download Filings
- **Method:** GET `/global-filings/download?documentId=<id>`
- **Premium:** Premium Access Required
- **Args:** `documentId` REQUIRED (note: different from filingId; one filing can contain multiple documents)
- **Response:** raw filing document

---

# Ownership & Insiders

### Ownership
- **Method:** GET `/stock/ownership?symbol=<sym>&limit=<n>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Sources:** 13F, 13D, 13G (US); UK Share Register; SEDI (Canada); equivalents elsewhere
- **Args:** `symbol` REQUIRED; `limit` optional
- **Response:** `{ownership[] of {change, filingDate, name, share}, symbol}`

### Fund Ownership
- **Method:** GET `/stock/fund-ownership?symbol=<sym>&limit=<n>`
- **Premium:** Premium Access Required
- **Args:** `symbol` REQUIRED; `limit` optional
- **Response:** `{ownership[] of {change, filingDate, name, portfolioPercent, share}, symbol}`

### Institutional Profile
- **Method:** GET `/institutional/profile?cik=<cik>`
- **Premium:** Premium Access Required. 60+ profiles supported.
- **Args:** `cik` optional (blank = full list)
- **Response:** `{cik, data[] of {cik, firmType, manager, philosophy, profile, profileImg}}`

### Institutional Portfolio
- **Method:** GET `/institutional/portfolio?cik=<cik>&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>`
- **Premium:** Premium Access Required. Limit: 1 year of data per call.
- **Args:** `cik`, `from`, `to` all REQUIRED
- **Response:** `{cik, data[] per filingDate of {portfolio[] of {change, cusip, name, noVoting, percentage, putCall, share, sharedVoting, soleVoting, symbol, value}, reportDate}, name}`

### Institutional Ownership
- **Method:** GET `/institutional/ownership?symbol=<sym>&cusip=<cusip>&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>`
- **Premium:** Premium Access Required. Limit: 1 year per call.
- **Args:** `symbol`, `cusip`, `from`, `to` all REQUIRED
- **Response:** `{cusip, data[] per timestamp of {ownership[] of {change, cik, name, noVoting, percentage, putCall, share, sharedVoting, soleVoting, value}, reportDate}, symbol}`

### Insider Transactions
- **Method:** GET `/stock/insider-transactions?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1)
- **Sources:** Forms 3/4/5, SEDI, equivalent intl filings (US, UK, CA, AU, IN, EU)
- **Limit:** 100 transactions/call. `symbol` blank returns latest market-wide.
- **Args:** `symbol` REQUIRED; `from`, `to` optional
- **Response:** `{data[] of {change (+ = BUY, - = SELL), filingDate, name, share, symbol, transactionCode, transactionDate, transactionPrice}, symbol}`

### Insider Sentiment
- **Method:** GET `/stock/insider-sentiment?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none
- **Coverage:** US companies. MSPR (Monthly Share Purchase Ratio), -100 to +100.
- **Args:** `symbol`, `from`, `to` all REQUIRED
- **Response:** `{data[] of {change, month, mspr, symbol, year}, symbol}`

---

# Calendar

### IPO Calendar
- **Method:** GET `/calendar/ipo?from=<date>&to=<date>`
- **Premium:** none
- **Args:** `from`, `to` REQUIRED
- **Response:** `{ipoCalendar[] of {date, exchange, name, numberOfShares, price, status (`expected`/`priced`/`withdrawn`/`filed`), symbol, totalSharesValue}}`

### Earnings Calendar
- **Method:** GET `/calendar/earnings?from=<date>&to=<date>&symbol=<sym>&international=<bool>`
- **Premium:** Free Tier covers 1 month historical + new updates
- **Args:** all optional
- **Response:** `{earningsCalendar[] of {date, epsActual, epsEstimate, hour (`bmo`/`amc`/`dmh`), quarter, revenueActual, revenueEstimate, symbol, year}}`

### Economic Calendar
- **Method:** GET `/calendar/economic?from=<date>&to=<date>`
- **Premium:** Premium Access Required (historical + surprises = Enterprise)
- **Args:** `from`, `to` optional
- **Response:** `{economicCalendar[] of {actual, country, estimate, event, impact, prev, time, unit}}`

---

# Estimates (all Premium)

| Endpoint | Path | Returns avg/high/low + analyst count + period |
|---|---|---|
| Revenue Estimates | `/stock/revenue-estimate?symbol=<sym>&freq=<annual\|quarterly>` | `revenueAvg, revenueHigh, revenueLow` |
| Earnings (EPS) Estimates | `/stock/eps-estimate?symbol=<sym>&freq=` | `epsAvg, epsHigh, epsLow` |
| EBITDA Estimates | `/stock/ebitda-estimate?symbol=<sym>&freq=` | `ebitdaAvg, ebitdaHigh, ebitdaLow` |
| EBIT Estimates | `/stock/ebit-estimate?symbol=<sym>&freq=` | `ebitAvg, ebitHigh, ebitLow` |
| Net Income Estimates | `/stock/net-income-estimate?symbol=<sym>&freq=` | `netIncomeAvg, netIncomeHigh, netIncomeLow` |
| Pretax Income Estimates | `/stock/pretax-income-estimate?symbol=<sym>&freq=` | `pretaxIncomeAvg, pretaxIncomeHigh, pretaxIncomeLow` |
| Gross Income Estimates | `/stock/gross-income-estimate?symbol=<sym>&freq=` | `grossIncomeAvg, grossIncomeHigh, grossIncomeLow` |
| DPS Estimates | `/stock/dps-estimate?symbol=<sym>&freq=` | `dpsAvg, dpsHigh, dpsLow` |

All require `symbol`; `freq` optional, default `quarterly` (also accepts `annual`). All gated as **Premium Access Required**.

### Earnings Surprises
- **Method:** GET `/stock/earnings?symbol=<sym>&limit=<n>`
- **Premium:** Free Tier returns last 4 quarters
- **Args:** `symbol` REQUIRED; `limit` optional
- **Response:** array of `{actual, estimate, period, quarter, surprise, surprisePercent, symbol, year}`

---

# Real-time / Pricing

### Quote
- **Method:** GET `/quote?symbol=<sym>`
- **Premium:** none (US stocks)
- **Note:** Use websocket for real-time. International real-time = Enterprise via partner feed.
- **Args:** `symbol` REQUIRED
- **Response:** `{c (current), d (change), dp (% change), h (high), l (low), o (open), pc (prev close), t (timestamp)}`

### Stock Candles
- **Method:** GET `/stock/candle?symbol=<sym>&resolution=<r>&from=<unix>&to=<unix>`
- **Premium:** Premium Access Required
- **Resolutions:** `1`, `5`, `15`, `30`, `60`, `D`, `W`, `M`. Daily is split-adjusted; intraday is unadjusted; intraday limited to 1 month per call.
- **Args:** all REQUIRED
- **Response:** `{c[], h[], l[], o[], s (`ok`/`no_data`), t[], v[]}`

### Tick Data
- **Method:** GET `/stock/tick?symbol=<sym>&date=<YYYY-MM-DD>&limit=<n>&skip=<n>&format=json`
- **Premium:** Premium Access Required
- **Limits:** `limit` max 25,000. Coverage: US Full SIP (end-of-day), TSX (EOD), LSE (15-min delayed), Euronext (EOD), Deutsche Börse (EOD).
- **Args:** all REQUIRED
- **Response:** `{c[] (conditions), count, p[] (prices), s (symbol), skip, t[] (UNIX ms), total, v[] (volumes), x[] (venues)}`

### Historical NBBO
- **Method:** GET `/stock/bbo?symbol=<sym>&date=<YYYY-MM-DD>&limit=<n>&skip=<n>&format=json`
- **Premium:** Premium Access Required
- **Coverage:** US (from 2023+ via API; older via bulk download), LSE, TSX, Euronext, Deutsche Börse
- **Args:** all REQUIRED
- **Response:** `{a[] (ask price), av[] (ask vol), ax[] (ask venue), b[] (bid), bv[], bx[], c[] (conditions), count, s, skip, t[] (UNIX ms), total}`

### Last Bid-Ask
- **Method:** GET `/stock/bidask?symbol=<sym>`
- **Premium:** Premium Access Required
- **Coverage:** US stocks
- **Args:** `symbol` REQUIRED
- **Response:** `{a (ask), av (ask vol), b (bid), bv (bid vol), t}`

### Splits
- **Method:** GET `/stock/split?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium required
- **Args:** all REQUIRED
- **Response:** array of `{date, fromFactor, symbol, toFactor}`

### Dividends
- **Method:** GET `/stock/dividend?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium Access Required (included on Fundamental-1: 10y history)
- **Args:** all REQUIRED
- **Response:** array of `{adjustedAmount, amount, currency, date (ex-div), declarationDate, freq (0=Annual, 1=Monthly, 2=Quarterly, 3=Semi-annual, 4=Other, 5=Bimonthly, 6=Trimesterly, 7=Weekly), payDate, recordDate, symbol}`

### Dividends 2 (Basic — global)
- **Method:** GET `/stock/dividend2?symbol=<sym>`
- **Premium:** Premium required
- **Args:** `symbol` REQUIRED
- **Response:** `{data[] of {amount, exDate}, symbol}`

### Sector Metrics
- **Method:** GET `/sector/metrics?region=<region>`
- **Premium:** Premium Access Required
- **Args:** `region` REQUIRED
- **Response:** `{data[] per sector of {metrics (key-value, with `a`=avg and `m`=median), sector}, region}`

### Price Metrics
- **Method:** GET `/stock/price-metric?symbol=<sym>&date=<date>`
- **Premium:** Premium Access Required
- **Notes:** 52-week high/low, YTD return, etc. Weekly granularity.
- **Args:** `symbol` REQUIRED; `date` optional (auto-snapped to last day of week)
- **Response:** `{atDate, data (key-value), symbol}`

### Symbol Change
- **Method:** GET `/ca/symbol-change?from=<date>&to=<date>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Coverage:** US, EU, NSE, ASX. Limit 2,000 events/call.
- **Args:** `from`, `to` REQUIRED
- **Response:** `{data[] of {atDate, newSymbol, oldSymbol}, fromDate, toDate}`

### ISIN Change
- **Method:** GET `/ca/isin-change?from=<date>&to=<date>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Coverage:** EU listings. Limit 2,000 events/call.
- **Args:** `from`, `to` REQUIRED
- **Response:** `{data[] of {atDate, newIsin, oldIsin}, fromDate, toDate}`

---

# Indices

### Indices Constituents
- **Method:** GET `/index/constituents?symbol=<idx>`
- **Premium:** Premium Access Required
- **Args:** `symbol` REQUIRED
- **Response:** `{constituents[], constituentsBreakdown[] of {cusip, isin, name, shareClassFIGI, symbol, weight}, symbol}`

### Indices Historical Constituents
- **Method:** GET `/index/historical-constituents?symbol=<idx>`
- **Premium:** Premium required
- **Args:** `symbol` REQUIRED
- **Response:** `{historicalConstituents[] of {action (add/remove), date, symbol}, symbol}`

---

# ETFs

### ETFs Profile
- **Method:** GET `/etf/profile?symbol=<etf>` or `?isin=`
- **Premium:** Premium required
- **Response:** `{profile: {assetClass, aum, avgVolume, cusip, description, dividendYield, domicile, etfCompany, expenseRatio, inceptionDate, investmentSegment, isInverse, isLeveraged, isin, leverageFactor, logo, name, nav, navCurrency, priceToBook, priceToEarnings, trackingIndex, website}, symbol}`

### ETFs Holdings
- **Method:** GET `/etf/holdings?symbol=<etf>&isin=<isin>&skip=<n>&date=<date>`
- **Premium:** Premium required
- **Notes:** Widget only shows top 10. Use `skip` for historical, or `date` (not both).
- **Response:** `{atDate, holdings[] of {assetType (Equity/ETP/Fund/Bond/Other), cusip, isin, name, percent, share, symbol, value}, numberOfHoldings, symbol}`

### ETFs Sector Exposure
- **Method:** GET `/etf/sector?symbol=<etf>&isin=<isin>`
- **Premium:** Premium Access Required
- **Response:** `{sectorExposure[] of {industry, exposure}, symbol}`

### ETFs Country Exposure
- **Method:** GET `/etf/country?symbol=<etf>&isin=<isin>`
- **Premium:** Premium Access Required
- **Response:** `{countryExposure[] of {country, exposure}, symbol}`

### ETFs Equity Allocation
- **Method:** GET `/etf/allocation?symbol=<etf>&isin=<isin>`
- **Premium:** Premium Access Required
- **Response:** `{data: {largeBlend, largeGrowth, largeValue, midBlend, midGrowth, midValue, smallBlend, smallGrowth, smallValue} (percentages), symbol}`

---

# Mutual Funds

### Mutual Funds Profile
- **Method:** GET `/mutual-fund/profile?symbol=<sym>` or `?isin=`
- **Premium:** Premium required
- **Response:** `{profile: {benchmark, beta, category, classId, className, currency, cusip, deferredLoad, description, expenseRatio, fee12b1, frontLoad, fundCompany, fundFamily, inceptionDate, investmentSegment, iraMinInvestment, isin, manager, maxRedemptionFee, name, seriesId, seriesName, sfdrClassification (Article 6/8/9 EU), standardMinInvestment, status, totalNav, turnover}, symbol}`

### Mutual Funds Holdings
- **Method:** GET `/mutual-fund/holdings?symbol=<sym>&isin=<isin>&skip=<n>`
- **Premium:** Premium required
- **Response:** same shape as ETFs Holdings

### Mutual Funds Sector Exposure
- **Method:** GET `/mutual-fund/sector?symbol=<sym>` or `?isin=`
- **Premium:** Premium required
- **Response:** `{sectorExposure[] of {sector, exposure}, symbol}`

### Mutual Funds Country Exposure
- **Method:** GET `/mutual-fund/country?symbol=<sym>` or `?isin=`
- **Premium:** Premium required
- **Response:** `{countryExposure[] of {country, exposure}, symbol}`

### Mutual Funds EET (EU regulatory data)
- **Method:** GET `/mutual-fund/eet?isin=<isin>`
- **Premium:** Premium Access Required
- **Args:** `isin` REQUIRED
- **Response:** `{data, isin}`

### Mutual Funds EET PAI
- **Method:** GET `/mutual-fund/eet-pai?isin=<isin>`
- **Premium:** Premium Access Required
- **Args:** `isin` REQUIRED
- **Response:** `{data, isin}`

---

# Bonds

### Bond Profile
- **Method:** GET `/bond/profile?figi=<figi>&isin=<isin>&cusip=<cusip>`
- **Premium:** Premium Access Required
- **Args:** at least one of `isin`, `cusip`, `figi`
- **Response:** `{amountOutstanding, asset, assetType, bondType, callable, coupon, couponType, cusip, datedDate, debtType, figi, firstCouponDate, industryGroup, industrySubGroup, isin, issueDate, maturityDate, offeringPrice, originalOffering, paymentFrequency, securityLevel}`

### Bond Price Data
- **Method:** GET `/bond/price?isin=<isin>&from=<unix>&to=<unix>`
- **Premium:** Premium Access Required
- **Coverage:** US Government Bonds (EOD), FINRA Trace BTDS Corporate (4h delay), 144A (4h), International (EOD)
- **Args:** all REQUIRED
- **Response:** `{c[] (close), s (status), t[] (timestamps)}`

### Bond Tick Data
- **Method:** GET `/bond/tick?isin=<isin>&date=<date>&limit=<n>&skip=<n>&format=json&exchange=trace`
- **Premium:** Premium Access Required
- **Coverage:** FINRA Trace BTDS Corporate, 144A (both 4h delayed)
- **Args:** all REQUIRED
- **Response:** `{ats (ATS flag), c[] (conditions), count, cp[] (counterparty), p[] (price), rp[] (reporting party), si[] (Buy/Sell side), skip, t[] (UNIX ms), total, v[] (volume), y[] (yield)}`

### Bond Yield Curve
- **Method:** GET `/bond/yield-curve?code=<code>`
- **Premium:** Premium Access Required
- **Coverage:** Treasury bonds
- **Args:** `code` REQUIRED (e.g. `10y`)
- **Response:** `{code, data[] of {d (date), v (value)}}`

---

# Forex

### Forex Exchanges
- **Method:** GET `/forex/exchange`
- **Premium:** none
- **Args:** none
- **Response:** array of supported exchange names

### Forex Symbol
- **Method:** GET `/forex/symbol?exchange=<exchange>`
- **Premium:** none
- **Args:** `exchange` REQUIRED
- **Response:** array of `{description, displaySymbol, symbol}`

### Forex Candles
- **Method:** GET `/forex/candle?symbol=<sym>&resolution=<r>&from=<unix>&to=<unix>`
- **Premium:** Premium Access Required
- **Args:** all REQUIRED. `symbol` from `/forex/symbol`. Resolutions: `1, 5, 15, 30, 60, D, W, M`.
- **Response:** `{c[], h[], l[], o[], s, t[], v[]}`

### Forex Rates (All Rates)
- **Method:** GET `/forex/rates?base=<ccy>&date=<date>`
- **Premium:** Premium Access Required
- **Args:** `base` optional (default EUR), `date` optional (default latest)
- **Response:** `{base, quote (map base/quote to rate)}`

---

# Crypto

### Crypto Exchanges
- **Method:** GET `/crypto/exchange`
- **Premium:** none
- **Args:** none
- **Response:** array of supported crypto exchanges

### Crypto Symbol
- **Method:** GET `/crypto/symbol?exchange=<exchange>`
- **Premium:** none
- **Args:** `exchange` REQUIRED (e.g. `binance`)
- **Response:** array of `{description, displaySymbol, symbol}`

### Crypto Profile
- **Method:** GET `/crypto/profile?symbol=<sym>`
- **Premium:** Premium Access Required
- **Args:** `symbol` REQUIRED (e.g. `BTC`, `ETH`)
- **Response:** `{circulatingSupply, description, launchDate, logo, longName, marketCap, maxSupply, name, proofType, totalSupply, website}`

### Crypto Candles
- **Method:** GET `/crypto/candle?symbol=<sym>&resolution=<r>&from=<unix>&to=<unix>`
- **Premium:** Premium Access Required
- **Args:** all REQUIRED. `symbol` from `/crypto/symbol`. Resolutions: `1, 5, 15, 30, 60, D, W, M`.
- **Response:** `{c[], h[], l[], o[], s, t[], v[]}`

---

# Technical Analysis

### Pattern Recognition
- **Method:** GET `/scan/pattern?symbol=<sym>&resolution=<r>`
- **Premium:** Premium Access Required
- **Detects:** double top/bottom, triple top/bottom, head & shoulders, triangle, wedge, channel, flag, candlestick patterns
- **Args:** `symbol`, `resolution` REQUIRED
- **Response:** `{points[] of pattern coordinates}`

### Support/Resistance
- **Method:** GET `/scan/support-resistance?symbol=<sym>&resolution=<r>`
- **Premium:** Premium Access Required
- **Args:** `symbol`, `resolution` REQUIRED
- **Response:** `{levels[]}`

### Aggregate Indicators
- **Method:** GET `/scan/technical-indicator?symbol=<sym>&resolution=<r>`
- **Premium:** Premium Access Required
- **Args:** `symbol`, `resolution` REQUIRED
- **Response:** `{technicalAnalysis: {count: {buy, neutral, sell}, signal}, trend: {adx, trending}}`

### Technical Indicators
- **Method:** GET `/indicator?symbol=<sym>&resolution=<r>&from=<unix>&to=<unix>&indicator=<name>&[indicator_fields]`
- **Premium:** Premium Access Required
- **Args:** `symbol`, `resolution`, `from`, `to`, `indicator` REQUIRED; `indicator_fields` optional
- **Notes:** Indicators include SMA, EMA, RSI, MACD, BBANDS, ATR, ADX, etc. Full list in live docs.
- **Response:** indicator values + price candle data

---

# Earnings Calls

### Earnings Call Transcripts List
- **Method:** GET `/stock/transcripts/list?symbol=<sym>`
- **Premium:** Premium required
- **Coverage:** US, UK, EU, AU, CA. `symbol` blank = latest market-wide.
- **Args:** `symbol` REQUIRED
- **Response:** `{symbol, transcripts[] of {id, quarter, time, title, year}}`

### Earnings Call Transcripts
- **Method:** GET `/stock/transcripts?id=<transcript_id>`
- **Premium:** Premium required. 15+ years, 220K+ audio, ~7TB total.
- **Args:** `id` REQUIRED
- **Response:** `{audio, id, participant[] of {description, name, role}, quarter, symbol, time, title, transcript[] of {name, session (mgmt or Q&A), speech}, year}`

### Earnings Call Audio Live
- **Method:** GET `/stock/earnings-call-live?from=<date>&to=<date>&symbol=<sym>`
- **Premium:** Premium required
- **Notes:** Live m3u8; mp3 in `recording` field after call ends.
- **Args:** all optional
- **Response:** `{event[] of {event, liveAudio, quarter, recording, symbol, time (UTC), year}}`

### Company Presentation
- **Method:** GET `/stock/presentation?symbol=<sym>`
- **Premium:** Premium required
- **Args:** `symbol` REQUIRED
- **Response:** `{res[] of {atTime, quarter, title, url, year}, symbol}`

---

# Alternative Data

### Social Sentiment
- **Method:** GET `/stock/social-sentiment?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium required (included on Fundamental-1)
- **Sources:** Reddit, Twitter
- **Args:** `symbol` REQUIRED; `from`, `to` optional
- **Response:** `{data[] of {atTime, mention, negativeMention, negativeScore (0-1), positiveMention, positiveScore (0-1), score (-1 to +1)}, symbol}`

### Investment Themes (Thematic Investing)
- **Method:** GET `/stock/investment-theme?theme=<theme>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Notes:** Bi-weekly analyst updates. Excludes penny / super-small-cap / illiquid.
- **Args:** `theme` REQUIRED
- **Response:** `{data[] of {symbol, theme}}`

### Supply Chain Relationships
- **Method:** GET `/stock/supply-chain?symbol=<sym>`
- **Premium:** Premium Access Required (NOT included on Fundamental-1)
- **Args:** `symbol` REQUIRED
- **Response:** `{data[] of {country, customer (bool), industry, name, oneMonthCorrelation, oneYearCorrelation, sixMonthCorrelation, supplier (bool), symbol, threeMonthCorrelation, twoWeekCorrelation, twoYearCorrelation}, symbol}`

### Company ESG Scores (latest)
- **Method:** GET `/stock/esg?symbol=<sym>`
- **Premium:** Premium Access Required (NOT included on Fundamental-1). 7,000+ companies.
- **Args:** `symbol` REQUIRED
- **Response:** `{data: {environmentScore, governanceScore, socialScore, totalESGScore}, symbol}`

### Historical ESG Scores
- **Method:** GET `/stock/historical-esg?symbol=<sym>`
- **Premium:** Premium Access Required (NOT included)
- **Args:** `symbol` REQUIRED
- **Response:** `{data[] of {data: {environmentScore, governanceScore, socialScore, totalESGScore}, period}, symbol}`

### Company Earnings Quality Score
- **Method:** GET `/stock/earnings-quality-score?symbol=<sym>&freq=<annual|quarterly>`
- **Premium:** Premium Access Required (NOT included on Fundamental-1)
- **Criteria:** Profitability, Growth, Cash Generation & Capital Allocation, Leverage
- **Args:** `symbol`, `freq` REQUIRED
- **Response:** `{data[] of {cashGenerationCapitalAllocation, growth, letterScore (C- to A+), leverage, period, profitability, score}, freq, symbol}`

### AI Copilot
- **Method:** POST `/ai-chat`
- **Premium:** Premium Access Required
- **Notes:** Chat with Finnhub's LLM trained on their data; returns text + widgets
- **Payload:** `messages` REQUIRED; `stream` optional
- **Response:** `{chatId, content, querySummary, relatedQueries, sources, tickers, widgets}`

---

# Government & Public-Records Alpha

### USPTO Patents
- **Method:** GET `/stock/uspto-patent?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1). Limit 250 records/call.
- **Args:** all REQUIRED
- **Response:** `{data[] of {applicationNumber, companyFilingName[], description, filingDate, filingStatus, patentNumber, patentType, publicationDate, url}, symbol}`

### H1-B / Permanent Visa Application
- **Method:** GET `/stock/visa-application?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1). Updated quarterly from DOL.
- **Args:** all REQUIRED (filter on `beginDate`)
- **Response:** `{data[] of {beginDate, caseNumber, caseStatus, employerName, endDate, fullTimePosition, h1bDependent, jobTitle, quarter, receivedDate, socCode, symbol, visaClass, wageLevel, wageRangeFrom, wageRangeTo, wageUnitOfPay, worksiteAddress, worksiteCity, worksiteCounty, worksitePostalCode, worksiteState, year}, symbol}`

### Senate Lobbying
- **Method:** GET `/stock/lobbying?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1)
- **Args:** all REQUIRED
- **Response:** `{data[] of {clientId, country, date, description, documentUrl, expenses, houseregistrantId, income, name, period, postedName, registrantId, senateId, symbol, year}, symbol}`

### USA Spending
- **Method:** GET `/stock/usa-spending?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** none (included on Fundamental-1)
- **Args:** all REQUIRED (filter on `actionDate`)
- **Response:** `{data[] of {actionDate, awardDescription, awardingAgencyName, awardingOfficeName, awardingSubAgencyName, country, naicsCode, performanceCity, performanceCongressionalDistrict, performanceCountry, performanceCounty, performanceEndDate, performanceStartDate, performanceState, performanceZipCode, permalink, recipientName, recipientParentName, symbol, totalValue}, symbol}`

### Congressional Trading
- **Method:** GET `/stock/congressional-trading?symbol=<sym>&from=<date>&to=<date>`
- **Premium:** Premium Access Required (included on Fundamental-1)
- **Args:** all REQUIRED
- **Response:** `{data[] of {amountFrom, amountTo, assetName, filingDate, name, ownerType, position, symbol, transactionDate, transactionType (Sale/Purchase)}, symbol}`

### Bank Branch List
- **Method:** GET `/bank-branch?symbol=<sym>`
- **Premium:** Accessible with Fundamental or All-in-One subscription (included on Fundamental-1)
- **Args:** `symbol` REQUIRED
- **Response:** `{data[] of {address, branchId, date (opened), state, zipCode}, symbol}`

### FDA Committee Meeting Calendar
- **Method:** GET `/fda-advisory-committee-calendar`
- **Premium:** none
- **Args:** none
- **Response:** array of `{eventDescription, fromDate (EST), toDate (EST), url}`

---

# Economic Data

### Economic Code
- **Method:** GET `/economic/code`
- **Premium:** Accessible with Fundamental data or All-in-One subscription
- **Args:** none
- **Response:** array of `{code, country, name, unit}`

### Economic Data
- **Method:** GET `/economic?code=<code>`
- **Premium:** Accessible with Fundamental data or All-in-One subscription
- **Args:** `code` REQUIRED
- **Response:** `{code, data[] of {date, value}}`

---

# Open Datasets (free, bulk download via Kaggle)

| Dataset | Source |
|---|---|
| SEC Financials As Reported | Kaggle |
| SEC Filings Metadata | Kaggle |
| S&P 500 futures tick data | Kaggle |

---

# Cross-link

Live docs (always authoritative when conflict): https://finnhub.io/docs/api
Pricing/tiers: https://finnhub.io/pricing
