# Phase B — Dynamic watchlist (S&P 500 + NASDAQ-100 + DJIA top 500 by 30D ADV) — 2026-05-04

Replaces the static 503-symbol S&P 500 watchlist with a daily-rebuilt
union-then-top-N selection. Closes a known coverage gap (the static list
misses NASDAQ-100 names that aren't in S&P 500, and inherits whatever
membership state was current when the file was last hand-edited).

## Why

Two motivations:

1. **Coverage.** The 2026-05-01 RCA noted that 3 of Friday's top 10 S&P
   gainers weren't in the watchlist at all. A union with NASDAQ-100 +
   DJIA brings in liquid names the strategy currently ignores (DJIA is
   already a subset of S&P 500, so contributes ~0 net new; NASDAQ-100
   contributes ~10-20 net new names like ASML/MELI/JD/ARM that aren't in
   S&P 500).

2. **Auto-currency.** The static watchlist requires manual edits when
   index membership changes (S&P 500 swaps roughly twice a quarter).
   Polygon doesn't include constituent lists at the Stocks Starter tier
   we're on, so the daily rebuild sources from Wikipedia (free, current
   within ~1 day of any membership change).

The "top 500 by 30-day average dollar volume" filter screens out illiquid
NASDAQ-100 ADRs and similar. With the current union of ~520-525 symbols,
this trims the ~20-25 lowest-ADV names. As future waves expand scope
(possibly to NASDAQ Composite or Russell 2000), the same top-N filter
keeps the watchlist size bounded at the level the Alpaca SIP subscription
can handle.

## What

### New file: `data/watchlist_builder.py` (~310 lines)

Pure module, no global state. Functions:

- `get_sp500_symbols()` / `get_nasdaq100_symbols()` / `get_djia_symbols()` —
  Wikipedia-sourced. Defensive parsing: each fetcher iterates through all
  tables on the page and finds the right one by column name (looks for
  "Symbol" / "Ticker" / "Ticker symbol" / "Code") AND a row-count sanity
  bound (S&P 500 expects 400-600, NASDAQ-100 expects 90-110, DJIA expects
  exactly 30). On any parse failure returns an empty set, logs loudly,
  and the orchestrator aborts the refresh — preserving the previous good
  watchlist file.

- `_normalize_symbol(symbol)` — converts hyphenated forms (e.g. "BRK-B")
  to dot form ("BRK.B") to match Polygon and Alpaca conventions.

- `fetch_30day_adv(symbols, polygon_key, ...)` — async. Uses Polygon's
  grouped daily aggregates endpoint (one HTTP call returns all US stocks
  for one trading day) for the last 45 calendar days, then keeps the
  first 30 non-empty days (= trading days) and computes per-symbol
  average dollar volume. ~30 API calls per refresh, well under Polygon
  Stocks Starter's effective limit.

- `build_dynamic_watchlist(polygon_key, top_n=500)` — async. Orchestrates
  the three Wikipedia fetches plus the ADV computation, sorts the union
  by ADV descending, returns the top N plus metadata.

- `write_watchlist_file(watchlist, metadata, output_path)` — atomic
  write-to-temp-then-rename so a kill mid-write can't leave a half-written
  file.

- `read_watchlist_file(path, max_age_days=7)` — used at boot. Returns
  None if the file is missing, malformed, or older than max_age_days.

- `refresh_dynamic_watchlist(polygon_key, output_path, top_n=500)` —
  async wrapper. Builds + writes; returns False (and leaves the previous
  file intact) on any source-side failure.

### New file: `scripts/manual_build_watchlist.py`

One-shot trigger to validate the live flow without waiting for 08:30 ET
or to seed the dynamic file before a service restart.

### Modified files

`main.py` — three small edits:

1. Import: `from data.watchlist_builder import read_watchlist_file, refresh_dynamic_watchlist`.

2. `TradingPlatform.__init__` reads `config/watchlist_dynamic.json` if
   present and recent (<7 days). On hit, uses it; on miss, falls back to
   `settings.yaml.watchlist`. Logs which path was taken so the boot trace
   makes the choice explicit.

3. `_daily_routine_loop` adds a new ungated block that fires every day at
   08:30 ET (alongside the earnings refresh) calling
   `_run_dynamic_watchlist_refresh`. The new method writes the JSON file;
   `self.watchlist` is NOT updated mid-run — SIP subscriptions are set at
   boot from `self.watchlist` and Phase B does not implement hot
   re-subscription. The new file becomes effective on next service
   restart.

`requirements.txt` — adds `lxml>=5.0.0` (required by `pandas.read_html`).

### New SQLite table — none

Phase B uses a JSON file, not a SQL table. The watchlist isn't a series
of events (which is what the catalysts table holds); it's a single
snapshot that gets fully replaced each day. JSON file with atomic rename
matches that shape better than a versioned table would.

### Configuration — none

`config/settings.yaml` unchanged. The static `watchlist:` list there now
serves only as the fallback when no dynamic file exists.

## Sandbox tests

10/10 passed (ephemeral test in `/tmp/finnhub_wave1a/`):

1. `_normalize_symbol` handles lowercase, hyphens, surrounding whitespace.
2. `_find_symbol_column` matches "Symbol" / "Ticker symbol", returns None
   when no column matches.
3. `get_sp500_symbols` with mocked HTML returns 503 normalized symbols.
4. `get_djia_symbols` with mocked HTML returns exactly 30.
5. `get_djia_symbols` rejects when row count != 30 (negative test).
6. `build_dynamic_watchlist` with all mocks returns top 200 sorted by
   mocked ADV, with correct metadata.
7. `build_dynamic_watchlist` aborts (returns `[], {}`) when the S&P 500
   fetch returns suspiciously few symbols.
8. `write_watchlist_file` + `read_watchlist_file` roundtrip preserves
   the watchlist exactly.
9. `read_watchlist_file` rejects a file older than `max_age_days`.
10. `read_watchlist_file` returns None when the file doesn't exist.

## Deploy procedure

This is a multi-file deploy (data/watchlist_builder.py, main.py,
requirements.txt, scripts/manual_build_watchlist.py). Standard paste.rs
+ curl pattern as with prior deploys.

Order of operations:
1. Upload all four files via paste.rs from PowerShell.
2. On the VPS, `pip install lxml` into the venv (one-time dep add).
3. curl the four files into place.
4. py_compile the two .py modules + main.py.
5. Run the manual trigger to validate the live flow before restart.
6. chown trader:trader on all four files.
7. Restart trader.service.
8. Confirm boot logs show `Watchlist: <N> symbols (dynamic, ...)`.

## Not yet tested (Rule 12 disclosure)

- **Live Wikipedia parsing.** Sandbox tests use mocked HTML. Real-world
  Wikipedia tables can include footnote markers, header rows, or layout
  changes that defensive parsing might miss. Manual trigger run will
  surface this.
- **Live Polygon grouped daily endpoint shape.** Sandbox tests assume
  the documented response shape `{results: [{T, c, v, ...}, ...]}`.
  Manual trigger run validates.
- **End-to-end flow at boot.** Untested in sandbox because the
  Trading.Base.1 main.py is too large to py_compile through OneDrive's
  FUSE staleness. Will validate via py_compile on the VPS before
  restart.

## Future work tracked separately

- **Hot SIP re-subscription** when watchlist changes mid-run. Currently
  out of scope; the daily refresh at 08:30 ET doesn't take effect until
  the next service restart. If that becomes operational pain, file a
  Phase B.5 task to add unsubscribe/subscribe diff logic to the
  AlpacaBarStream.

- **Polygon Indices add-on** ($199/mo) as a first-party constituent
  source if Wikipedia parsing proves fragile.

- **Russell 2000 / NASDAQ Composite expansion** — would broaden coverage
  to small-caps but increase Alpaca SIP load and noise in the signal
  engine. Defer until at least 2-4 weeks of clean paper data on the
  current watchlist.
