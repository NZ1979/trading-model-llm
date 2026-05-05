# Wave 1A — Finnhub Earnings Calendar veto for gap-and-go — 2026-05-03

First Finnhub integration since the Track 2 plan was subscribed. Wires the
Finnhub Earnings Calendar endpoint as a hard veto on gap-and-go signals.
Pullback path is unaffected.

## Why

Per the gap-and-go evaluation (`docs/finnhub_gap_and_go_evaluation.md`),
trading gap-and-go on a stock with earnings before/during/after the bar is
binary-bet exposure that the strategy isn't designed for. The 2026-05-01
RCA didn't surface earnings-day trades as a specific failure, but the
strategy logic has no current automatic earnings filter — it's an open hole
that this wave closes before it bites.

Earnings Calendar was the highest-scored endpoint in the gap-and-go
evaluation (92/100) and is on the free tier of Fundamental-1.

## What

### New file: `data/finnhub_feed.py` (~250 lines)

`FinnhubClient` — async HTTP client with built-in dual-window rate limiter
(300 req/min plan cap, 30 req/sec global cap). Method-per-endpoint pattern;
Wave 1A only exposes `get_earnings_calendar(date_from, date_to, symbol=None)`.

`refresh_earnings_calendar(client, watchlist, db_path, days_forward=14)` —
fetches one window once (no symbol filter), filters in-memory to watchlist,
persists to new `catalysts` table. Idempotent: replay adds 0 rows via UNIQUE
constraint + INSERT OR IGNORE.

`is_earnings_day(db_path, ticker, date_str)` — fast catalyst lookup. Returns
False if the table doesn't exist (fail-soft on enrichment data).

### New SQLite table: `catalysts`

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

Unified store for any date-bound trade-relevant event. Wave 1A populates it
with `source='finnhub' event_type='earnings'`. Future waves will add FDA
events, rating changes, etc.

### Modified file: `main.py` (6 surgical edits)

1. Import: `from data.finnhub_feed import FinnhubClient, refresh_earnings_calendar, is_earnings_day`
2. Required env var: `finnhub_key: _require_env("FINNHUB_API_KEY")` — service refuses to boot without it
3. `TradingPlatform.finnhub_client: FinnhubClient | None = None` attribute
4. `boot()` initializes the client right after the Alpaca client
5. `_run_baseline_backfill()` calls `refresh_earnings_calendar` after Polygon backfills (still in the 8:30 ET window)
6. `_evaluate_and_execute()` checks `is_earnings_day(ticker, today)` BEFORE evaluating sentiment/walls/decision combiner; if `tech.setup == "gap_and_go"` and the ticker has earnings today, log a `[gap_and_go] VETO` line and return early. State dedup ensures the log fires once per state-change, not on every bar.

### New env var

`FINNHUB_API_KEY` — required at startup. Service refuses to boot if missing.
Goes into `/etc/trading-platform/env` on the VPS alongside the existing
`ALPACA_*`, `POLYGON_API_KEY`, `ANTHROPIC_API_KEY`, `DATABENTO_API_KEY`.

### Configuration — none

`config/settings.yaml` unchanged. Wave 1A doesn't introduce new tunables.

## Sandbox tests

7/7 passed (`/tmp/test_finnhub_feed.py`, ephemeral):

1. `FinnhubClient` rejects empty API key with `ValueError`
2. `_init_catalysts_table` creates schema idempotently (callable twice without error)
3. `is_earnings_day` returns False on empty table
4. `is_earnings_day` returns True for matching row, False for different date, case-insensitive on ticker
5. `is_earnings_day` returns False (not crash) when catalysts table doesn't exist yet
6. `refresh_earnings_calendar` with mocked client correctly filters to watchlist, deduplicates on replay, persists fields readable by `is_earnings_day`
7. Rate limiter waits ~1s when at the per-second cap (proves both windows enforce)

## Plan-availability test (Phase 0, prerequisite)

`scripts/test_finnhub_endpoints.py` confirmed Earnings Calendar returns HTTP
200 on the live Fundamental-1 plan. Sample response for AAPL window
2026-05-03..2026-05-17 returned the upcoming AAPL earnings event with
`epsEstimate`, `revenueEstimate`, `hour='amc'`, `quarter`, `year` populated.

## Not yet tested (Rule 12 disclosure)

- `main.py` could not be `py_compile`'d in the Linux sandbox due to OneDrive
  FUSE writeback delays (the kernel's view of the file lagged behind the
  actual Windows-side content). Will validate via `py_compile` on the VPS
  before service restart.
- End-to-end behavior on a real earnings day not yet observed in production.
  First real-world test will happen when any watchlist ticker has an earnings
  event during the next 14 days (AAPL is on 2026-05-15 per the Phase 0 probe).

## Files changed (this deploy)

```
data/finnhub_feed.py   # NEW (~250 lines)
main.py                # MODIFIED (6 edits in 4 methods)
```

VPS sync via `paste.rs` + `curl` (same channel as Track 1 deploy on
2026-05-03 morning).

## Operational notes

- The 8:30 ET refresh fetches a 14-day forward window in a single API call
  (no symbol filter), then filters in-memory. At 503-symbol watchlist scale,
  this is far cheaper than 503 per-symbol calls.
- The veto logs once per state transition per ticker (state.last_decision_action
  dedup), so the journal won't be spammed with one log per bar during the
  9:35-10:00 ET signal window.
- Pullback path is intentionally NOT vetoed: mean-reversion can be valid
  signal even on earnings days when price overreacts.
- Catalysts table is shared infrastructure — future waves (FDA Calendar,
  Recommendation Trends migrations to event-style storage) will reuse it.

## Track 2 progression (post-Wave 1A)

| Wave | Endpoints | Status |
|---|---|---|
| 1A | Earnings Calendar | **DEPLOYED 2026-05-03** |
| 1B | News Sentiment, Major Press Releases, Company News | Next |
| 2A | Recommendation Trends (Stock Upgrade/Downgrade dropped — 403) | Pending Wave 1B soak |
| 2B | Insider Transactions, Social Sentiment (Newsroom dropped — 403) | Pending Wave 2A |
| 3 | Basic Financials, Investment Themes, FDA Calendar | Pending Wave 2B |

Original Wave 2A had Stock Upgrade/Downgrade and Wave 2B had Newsroom; both
came back HTTP 403 in the Phase 0 probe and are dropped from scope.
Replacement signals: Recommendation Trends (already in 2A) covers ratings;
Major Press Releases (1B) covers what Newsroom would have provided.

## Same-day follow-up patch — weekend decoupling

After the initial Wave 1A deploy on Sunday 2026-05-03, the user observed that
the earnings refresh wouldn't fire over the weekend because it was nested inside
`_run_baseline_backfill` which is gated by `is_weekday`. The earnings calendar
is forward-looking (14-day window), so weekend refreshes are useful — they
keep the catalysts table current ahead of Monday's first signal evaluation.

**Patch contents:**

1. New method `TradingPlatform._run_finnhub_earnings_refresh()` — small wrapper
   around `data.finnhub_feed.refresh_earnings_calendar` that's a no-op if the
   client isn't initialized.
2. New variable `earnings_done_for: str | None = None` in
   `_daily_routine_loop`'s init block, separate from `backfill_done_for`.
3. New ungated trigger block in `_daily_routine_loop`, placed after the
   weekday-gated `_run_baseline_backfill` block:
   ```python
   if earnings_done_for != today and now_t >= backfill_time:
       try:
           await self._run_finnhub_earnings_refresh()
           earnings_done_for = today
       except Exception:
           logger.exception("Finnhub earnings refresh failed; will retry")
   ```
4. Removed the original earnings-refresh block from `_run_baseline_backfill`
   (it now lives only in the loop).

**Validation (production, Sunday 2026-05-03 23:48 UTC):**

After redeploying the patched `main.py` and restarting the service:

```
__main__ | Finnhub client initialized (earnings calendar veto enabled)
data.finnhub_feed | Finnhub earnings refresh: 1500 events fetched,
                    15 in watchlist, 0 new rows persisted (2026-05-03..2026-05-17)
```

The `0 new rows persisted` line is idempotency proof — the 15 watchlist
earnings events were already persisted via the manual_trigger script earlier
the same day, so the auto-refresh correctly inserted nothing. Any actual
new earnings announcements arriving via Finnhub between manual trigger and
auto-refresh would have been counted.

**Behavior summary post-patch:**

- Earnings refresh fires once per day at >= 08:30 ET, weekdays AND weekends
- Fires immediately at boot if current time >= 08:30 ET (loop's first iteration)
- Idempotent on replay (`INSERT OR IGNORE`)
- `_run_baseline_backfill` is unchanged for everything else (Polygon PM
  baselines + daily bars), still weekday-gated as before
