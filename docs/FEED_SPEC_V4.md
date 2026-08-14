# Intraday Feed → MCP Server — Spec v4

Build spec for `C:\trading\LLM model` (Godzilla). Supersedes v3.

**v3 was solving the wrong problem.** It engineered an elaborate pseudo-tape reconstruction to work around Schwab having no time-and-sales service. You already own a consolidated tape — Alpaca Algo Trader Plus, active, covering all three accounts, currently switched off in config. v4 deletes the workaround and splits the feed by what each vendor is actually good at.

Verified 2026-08-14 15:14 EDT. Working directory `C:\trading\LLM model`, workstation Godzilla, remote `origin` → `https://github.com/NZ1979/trading-model-llm.git`, working tree clean.

---

## 0. What changed from v3, and why

| v3 design | v4 | Reason |
|---|---|---|
| Schwab `LEVELONE_EQUITIES` as pseudo-tape source | **Alpaca SIP `trades` stream** | Alpaca delivers every print with price, size, exchange, and condition codes. Schwab level one is officially conflated and has no tape at all. |
| §2 Gate A (entitlement/NFL check) | **Deleted** | Alpaca SIP is consolidated by definition. There is no NFL-equivalent partial-market failure mode. |
| §2 Gate B (coverage experiment, 3 sessions) | **Deleted** | Nothing to measure. Coverage is 100%. |
| §6.3 volume accounting, `unattributed`, anomaly counters | **Deleted** | Conflation was the only reason these existed. |
| §6.7 `cvd_observed` vs `cvd_scaled` | **One CVD** | No extrapolation needed. |
| §6.6 `coverage_pct`, `DEGRADED`, `UNRELIABLE`, `INVALID_QUOTE_SOURCE` | **Reduced to staleness + sample-size guards** | The uncertainty those flags reported no longer exists. |
| "No trade conditions" limitation | **Resolved** | Alpaca trades carry condition codes, so odd lots and late reports are filterable. |
| Schwab as primary feed | **Schwab reduced to `NASDAQ_BOOK`** | Alpaca has no Level 2. Schwab's book contract is fully specified with MPID, per-MM size, and per-MM quote time. That is the one thing only Schwab provides. |

Roughly 40% of v3 was machinery for managing an error that no longer occurs. It is gone, not softened.

**What carries forward unchanged from v3:** the Schwab Streamer field tables and book contract (§3, §7 below), the read/compute thread split, the one-connection-per-user constraint, session-boundary handling, and all of §9 output discipline. Those were correct.

---

## 1. Immediate action, independent of this build

`config/settings.yaml` line 12 currently reads:

```yaml
alpaca_data_feed: iex   # Flipped to iex 2026-05-14: PA3QAZ941NFN doesn't have Algo Trader Plus.
```

The stated reason is false as of 2026-08-14 — Algo Trader Plus is Active and applies to all three Alpaca accounts. Every bar the platform has consumed since 2026-05-14 came from IEX alone, which `data/alpaca_market_data.py` itself documents as understating pre-market volume to 5–15% of true.

Change to:

```yaml
alpaca_data_feed: sip   # Full SIP. Algo Trader Plus verified active on all accounts 2026-08-14.
```

This affects RVOL, the pre-market baselines in `pm_rvol_thresholds.py`, and the gap-and-go RVOL≥5x gate — all of which have been computing against a fraction of real volume. Do this in its own commit, before any feed-daemon work, so the change is attributable if downstream numbers shift.

**Stale docs to fix in the same commit:**

- `PROJECT_BLUEPRINT.md` §2 stack table — still lists Databento as the canceled line and does not record the IEX downgrade or its reversal.
- `CLAUDE_PREFLIGHT.md` Rule 26 — the gap-and-go fork was shut down. The partition's operational premise has changed. Rewrite it to state current reality rather than leaving a rule that reads as binding but no longer describes the world. A quietly obsolete hard rule is worse than none, because the next session cannot tell which clauses still hold.
- `PROJECT_BLUEPRINT.md` §3 — deployment state is dated 2026-04-28 and the repo has been dormant since mid-May. Mark it stale or refresh it.

---

## 1a. Live verification results — 2026-08-14 post-market

Phase 0 and phase 1 shipped and were exercised against the live SIP tape via
`scripts/verify_alpaca_sip.py`. Samples: SNDK/NVDA/MU, 20–30s windows,
17:59–18:14 ET. Six findings, three of which changed the design.

**Confirmed as designed:**

| Claim | Observed |
|---|---|
| SIP is consolidated, not single-venue | 7 distinct trade exchanges (D, K, P, Q, U, Z, Y, H across runs). **Zero IEX (`V`) prints in any sample** — on the pre-phase-0 config these windows would have shown nothing. |
| Alpaca quotes are NBBO | Bid and ask routinely from different venues (bid P / ask K), consistent with a consolidated best. Confirmed against Alpaca's forum: `feed=iex` gives IEX's own best quote, "not necessarily the best full market (NBBO) quote". |
| Tick types parse real payloads | Trades, quotes and status all parsed clean; zero parse failures across ~340 trades. |

**Resolved — was `UNVERIFIED` in the original v4:**

Trade condition codes actually observed: `@` regular sale, `T` extended hours
(Form T), `I` odd lot, `F` intermarket sweep (ISO). The §5.2 filter is
therefore:

```python
NOT_LAST_ELIGIBLE = {"I"}   # odd lots never update last on the consolidated tape
```

`T` appears on 100% of post-market prints and will be absent in RTH — the
filter must not treat its presence or absence as an error. Any code outside
the known set must be surfaced loudly, not silently treated as eligible.

**Design changes forced by live data:**

1. **Clock skew is a real, measured hazard — not a theoretical one.** Godzilla's
   clock was **2.41 seconds slow**, measured two independent ways: negative
   message ages against SIP timestamps, and `w32tm /stripchart` against
   time.windows.com. Resynced to ~1.6ms. This would have silently destroyed
   the 500ms quote-age confidence tag (§5.1), by a factor of four, and broken
   the Schwab-book-to-Alpaca-tape correlation in §7 outright. No unit test
   could have caught it — it is an environment fault. **The daemon must
   measure skew continuously and expose it in `get_health`, not measure once
   at connect.** Drift returns.

2. **Every price field must carry its age.** In a 20s post-market window, only
   **1 of 38 prints** was last-sale eligible (97.4% odd lots). The last
   eligible price was 11.3 seconds old while raw prints arrived 0.3 seconds
   ago. A `last` field without an age reads as current when it is thirty-seven
   prints behind the market. This is stronger than the §8.1 stale-flag
   threshold: the flag says "something is wrong", the age says "here is how
   wrong". Both are needed.

3. **Off-exchange share bounds the pull/fill design.** Code `D` (FINRA ADF/TRF)
   was 23–53% of prints across samples. Those prints have no lit-book
   counterpart, so §7 pull/fill resolution can only ever speak to the lit
   portion of the tape. `UNRESOLVED` remains a correct and common answer;
   quantify it rather than hiding it.

**Unit question settled.** Alpaca's field reference describes quote sizes as
"in round lots". Observation contradicts it: a bid of `1665.00 x 1280` against
a tape doing 274 shares per 20s would be 128,000 shares resting on one level.
Sizes are **shares**, matching trade sizes. Note this differs from Schwab
level one, where bid/ask sizes genuinely are lots — the conversion belongs in
the Schwab adapter (§0 finding 5), and `tick_types.py` stays share-denominated
throughout.

---

## 2. Architecture

```
Alpaca SIP websocket  ← ONE connection, account-limited
  trades + quotes + bars
        │
        ▼
  data/alpaca_market_data.py     EXTENDED, not replaced
        │                        read loop only; enqueue and return
        ├──► asyncio.Queue ──► worker task
        │                          ├──► ring buffers (per symbol)
        │                          └──► SQLite WAL  ticks_YYYYMMDD.db
        │
Schwab websocket      ← ONE connection, account-limited
  NASDAQ_BOOK only
        │
        ▼
  data/schwab_book.py            NEW
        └──► book snapshots ──► same queue/worker
        │
        ▼
  analysis/microstructure.py     NEW — aggressor, CVD, VAP, pace, book metrics
        │
        ▼
  mcp/server.py                  NEW — stdio MCP server, read-only
        │
        ▼
  Claude Desktop
```

### The connection-limit constraint drives the module design

Both vendors cap you at one concurrent websocket per account.

`data/alpaca_market_data.py` line 16 already documents Alpaca's: *"The cap is per-account on concurrent connections (1 on all individual plans), so don't try to run two of these at once."*

Schwab's is response code 12 `CLOSE_CONNECTION`: *"A limit of 1 Streamer connection at any given time from a given user is available."*

Consequences, both non-negotiable:

1. **Do not write an `AlpacaTradeStream` class beside `AlpacaBarStream`.** They would fight over the single connection. Extend `AlpacaBarStream` into `AlpacaMarketStream` handling `T` in `{"b","t","q"}` on one socket. `main.py`'s existing `on_bar` callback keeps working; add `on_trade` and `on_quote` alongside it.
2. **Lock files on both daemons.** Fail loudly on double-start (Rule 18) rather than producing a reconnect loop that reads as a network fault. This is the failure mode that will cost you an afternoon otherwise.

You have three Alpaca accounts, all entitled. If you later need a second concurrent Alpaca connection, use a second account's keys rather than trying to multiplex — but design for one.

---

## 3. Alpaca SIP streams

Endpoint: `wss://stream.data.alpaca.markets/v2/sip` — already defined as `ALPACA_BARS_WS_SIP` in `data/alpaca_market_data.py`.

Subscribe in one message:

```python
await ws.send(json.dumps({
    "action": "subscribe",
    "trades": sorted(symbols),
    "quotes": sorted(symbols),
    "bars":   sorted(symbols),
}))
```

Message types to handle in `_process_message`:

| `T` | Meaning | Key fields |
|---|---|---|
| `t` | Trade | `S` symbol, `p` price, `s` size, `x` exchange, `c` condition codes, `t` RFC-3339 nanosecond timestamp, `i` trade id, `z` tape |
| `q` | Quote | `S`, `bp`/`bs` bid px/size, `ap`/`as` ask px/size, `bx`/`ax` exchanges, `t` timestamp |
| `b` | Bar | existing handling, unchanged |

`UNVERIFIED:` exact condition-code semantics for odd-lot and late-report exclusion. Pull Alpaca's condition-code reference before implementing the filter in §5.2; do not guess which codes are last-eligible.

**Symbol scope.** Trades and quotes on 503 S&P names is a far higher message rate than bars. Start the daemon on the handful of names you actually trade — the spec's original examples were SNDK, SNXX, MU, NVDL — not the full watchlist. Measure message rate before scaling. `main.py`'s bar path can stay subscribed to the full list on the same connection.

---

## 4. Schwab: book only

Everything about Schwab auth from v3 §4 stands, including the 7-day refresh-token expiry, `max_token_age` at 6 days, and the token file at `%LOCALAPPDATA%\trading\schwab_tokens.json`.

**App status, verified 2026-08-14:** `trading-feed-daemon`, Production, **Ready For Use** with no approval wait. Market Data Production + Accounts and Trading Production. Order Limit **0**. Callback `https://127.0.0.1:8182`. Credentials in `%LOCALAPPDATA%\trading\schwab.env`, outside the repo, folder ACL restricted to `GODZILLA\kings` with inheritance stripped.

Accounts and Trading Production is attached because `GET /userPreference` lives there and the streamer `LOGIN` cannot be built without `SchwabClientCustomerId`, `SchwabClientCorrelId`, `SchwabClientChannel`, `SchwabClientFunctionId`. Order Limit 0 throttles order requests to zero at Schwab's edge — enforcement outside this codebase, where a bug in it cannot reach.

Subscribe `NASDAQ_BOOK` only. Do not subscribe `LEVELONE_EQUITIES` — Alpaca covers it, and every field it would provide is either duplicated or worse.

Login sequence, per the Streamer contract:

1. `LOGIN` to `ADMIN`, **wait for the success response** — subscribing before it returns is a documented cause of code 20.
2. Parse `msg` for `status=` (`PN`/`NP`/`PP`). Log it; it no longer gates anything since Schwab isn't carrying quotes, but it belongs in `get_health`.
3. `ADD` for `NASDAQ_BOOK`. Never `SUBS` — `ADD` works as a first subscription and avoids wipe-out semantics.

### Book contract (authoritative, from the portal)

```
0 Symbol
1 Market Snapshot Time (ms)
2 Bid Side Levels []
3 Ask Side Levels []

  Price Level: 0 Price | 1 Aggregate Size | 2 Market Maker Count | 3 Market Makers []

    Market Maker: 0 Market Maker ID | 1 Size | 2 Quote Time (ms)
```

Delivery type is `Whole` and **throttled** — snapshots at an unpublished interval, not deltas. Anything shorter-lived than the throttle never existed as far as this system is concerned. Measure the interval empirically in week one and record it; it bounds every book metric's resolution.

**Rule 22 obligation.** `schwab-py` introduces a new HTTP client dependency. Per Rule 22, any new HTTP-client dependency triggers a fresh audit of its default logging behavior *before* the change ships. Audit `httpx`/`authlib` log levels and confirm the `setup_logging` suppression block covers them. Schwab passes tokens in headers rather than URL params, which is safer than the Polygon trap, but verify rather than assume.

---

## 5. Microstructure computation — `analysis/microstructure.py`

### 5.1 Aggressor classification

Now straightforward, because both inputs are real.

Maintain time-indexed bid and ask histories from the `q` stream. For each `t` print, bisect on the trade's timestamp to get the NBBO in effect at that moment — not the current NBBO.

- `price >= ask_at(t)` → buy-initiated
- `price <= bid_at(t)` → sell-initiated
- inside the spread → Lee-Ready tick test against the prior print

Unlike v3, the "prior print" here is genuinely the prior print, so the tick test behaves as the literature describes.

Keep one confidence tag: prints where the applicable quote is older than 500ms are low-confidence. Expose that share. Everything else v3 tracked is gone.

### 5.2 Condition-code filtering

Exclude non-last-eligible prints, odd lots, and late reports from volume-at-price and large-print detection using the `c` field. Resolve the code list from Alpaca's reference first (`UNVERIFIED` above). Fail loud on an unrecognized code rather than silently including it (Rule 18).

### 5.3 Metrics

**Flow** — now unqualified.

- CVD, session and rolling 1/5/15min. One number.
- Buy vs sell volume at price → absorption levels
- Large prints: ≥ N× median size, bucketed by price and side. Real now — with a full tape you see the largest print, not the last one in a conflation window.
- Trade count vs share count

**Location**

- Session VWAP + 1σ/2σ bands. Note `analysis/indicators.py` already computes VWAP for the 5-min path; reuse or explicitly diverge, don't silently duplicate.
- Session O/H/L, opening range (5m, 30m)
- Volume-at-price histogram, POC, value area — exact, from real prints
- Distance from prior close, prior day H/L

**Pace** — the metric that would have caught 2026-08-14.

- Rolling volume vs same-time-of-day baseline over trailing 20 sessions
- Backfill the baseline from Polygon (`data/polygon_feed.py`, already built, 15-min delayed is irrelevant for historical). No 20-session wait.
- 5.3M shares in 12 minutes against a 14.2M ADV is the signal. Build this first.

**Book** — Schwab only.

- Displayed size within N ticks of touch, per side
- Imbalance ratio
- Level persistence: read per-MM `Quote Time` directly. No inference required.
- Pull/fill: when an MM's size disappears, cross-reference the Alpaca tape at that price and time. **This is the payoff of running both feeds** — v3 could only answer `UNRESOLVED` because the pseudo-tape often had no print there. With a full tape, pull-vs-fill is usually decidable.

**Out of scope:** any metric whose output is a narrative. No sentiment, no momentum score, no pattern labels. Numbers only.

---

## 6. Storage

Separate database from `trading.db`. Do not co-mingle tick data with the decisions/orders/sentiment tables — different write rates, different retention, different failure blast radius.

`data/ticks/ticks_YYYYMMDD.db`, SQLite WAL, append-only.

```sql
CREATE TABLE trades (
  symbol TEXT, ts_ns INTEGER, trade_id INTEGER,
  price REAL, size INTEGER, exchange TEXT, conditions TEXT, tape TEXT,
  aggressor TEXT,        -- 'B','S','U'
  method TEXT,           -- 'QUOTE','TICK'
  quote_age_ms INTEGER
);
CREATE INDEX ix_trades_sym_ts ON trades(symbol, ts_ns);

CREATE TABLE quotes (
  symbol TEXT, ts_ns INTEGER,
  bid REAL, bid_size INTEGER, bid_ex TEXT,
  ask REAL, ask_size INTEGER, ask_ex TEXT
);
CREATE INDEX ix_quotes_sym_ts ON quotes(symbol, ts_ns);

CREATE TABLE bars_1m (
  symbol TEXT, ts_ms INTEGER,
  o REAL, h REAL, l REAL, c REAL, v INTEGER, n INTEGER, vw REAL
);

CREATE TABLE book_levels (
  symbol TEXT, snapshot_ms INTEGER, side TEXT,
  price REAL, agg_size INTEGER, mm_count INTEGER
);
CREATE TABLE book_mms (
  symbol TEXT, snapshot_ms INTEGER, side TEXT, price REAL,
  mm_id TEXT, size INTEGER, mm_quote_ms INTEGER
);
```

Store raw trades and quotes even though classification is derived from them. When the classification logic changes — and it will — the corpus must be recomputable without re-recording the market.

**Retention.** Full tape on even a few active names is gigabytes per week. Decide a retention policy before the first session, not after the disk fills. Suggested: keep raw 30 days, keep derived per-minute aggregates indefinitely.

**`.gitignore` additions** — the existing file ignores `trading.db` and `*.db-journal`, which does not cover WAL mode or the new files:

```
*.db-wal
*.db-shm
data/ticks/
```

Add these before the first run. Git only ignores what it isn't already tracking; adding rules after a multi-gigabyte file is committed means rewriting history.

---

## 7. MCP tool surface — `mcp/server.py`

Read-only. No order entry, no account access.

```
get_health()
  → { alpaca_connected, schwab_connected, last_trade_age_ms, last_book_age_ms,
      schwab_auth_state, schwab_token_age_days, schwab_entitlement_tier,
      subscribed_symbols, reconnects, book_throttle_ms, clock_skew_ms }
  ALWAYS call first. If stale, say so instead of analyzing stale data.

get_snapshot(symbol)
  → last, bid/ask/sizes, session OHLC, volume, vs_ADV_pct, vwap,
    dist_from_vwap, prior_close, gap_pct

get_flow(symbol, window="5m")
  → cvd, buy_vol, sell_vol, delta_pct, large_prints[], trade_count,
    avg_trade_size, low_confidence_pct, quote_method_split{QUOTE,TICK}

get_levels(symbol)
  → session_extremes, opening_range, poc, value_area, volume_nodes[],
    prior_day_levels[]

get_book(symbol, depth=10)
  → levels[], imbalance_ratio, per_level_mms[], persistence_ms per MM,
    recent_pulls[], recent_fills[], book_throttle_ms

get_bars(symbol, interval="1m", lookback=60)
  → OHLCV array

get_pace(symbol)
  → volume vs time-of-day baseline, percentile rank vs trailing 20 sessions

replay(symbol, date, start, end)
  → same metrics recomputed from raw trades/quotes

register_thesis(symbol, direction, invalidation_level, invalidation_condition, ts)
check_theses(symbol) → active[], invalidated[]
```

**Integrate `replay` with the existing M2 harness.** `data/replay/` already contains `driver.py`, `tick_loop.py`, `tick_context.py`, `persistence.py`, and `fill_simulator.py`, with fixtures and a design doc at `docs/M2_REPLAY_HARNESS_DESIGN.md`. Read that design before writing a second replay path. The MCP `replay` tool should almost certainly call into the existing harness rather than reimplement it — a fork in replay semantics between the two would be a slow, expensive bug.

Register in Claude Desktop's `claude_desktop_config.json` under `mcpServers`.

---

## 8. Output discipline

The feed fixes resolution. It does not fix the failure mode from 2026-08-14, where I anchored on a fade thesis and sorted incoming data by whether it agreed with me. Higher frequency gives me *more* material to build narratives from.

1. **`get_health` gates everything.** If `last_trade_age_ms > 2000`, other tools carry a `STALE` flag.
2. **Observables only.** No interpretation fields. If a response contains an adjective, it's a bug.
3. **Minimum-sample guard.** Thin names (SNXX) need a floor on trade count per window or the metrics are noise dressed as signal. `INSUFFICIENT_SAMPLE` is the only status flag surviving from v3.
4. **Thesis registry.** Any directional call gets an explicit kill condition. On 8/14 the 1,580 invalidation fired at 1,565 and I talked my way past it. `check_theses` returning `invalidated` is not advisory.
5. **Report state changes, not narratives.** "Update" returns what crossed a threshold since the last call. If nothing crossed, one line saying so.

---

## 9. Build order

| Phase | Deliverable | Notes |
|---|---|---|
| 0 | Flip `alpaca_data_feed` to `sip`; refresh stale docs; update Rule 26 | Own commit. Zero cost, immediate accuracy gain on the existing platform. |
| 1 | Extend `AlpacaBarStream` → `AlpacaMarketStream` (`b`/`t`/`q`), lock file, read/compute split | Existing tested module. Keep `on_bar` contract intact for `main.py`. |
| 2 | SQLite WAL persistence + `.gitignore` + retention policy | Corpus accumulation starts. No metrics yet. |
| 3 | Aggressor classification + condition-code filter | Resolve the `UNVERIFIED` condition-code question first. |
| 4 | Polygon baseline backfill + `get_pace` + `get_snapshot` + `get_bars` | Highest value per unit of work. The 8/14 metric. |
| 5 | `get_flow`, `get_levels` | Depends on 3. |
| 6 | Schwab auth + `data/schwab_book.py` + `get_book` | Rule 22 logging audit before this ships. |
| 7 | Pull/fill resolution (book × tape cross-reference) | The genuinely novel capability. Needs 3 and 6. |
| 8 | Thesis registry + `replay` via the M2 harness | Read `docs/M2_REPLAY_HARNESS_DESIGN.md` first. |

Phases 0 through 5 need Schwab not at all. If the book work stalls, everything of analytical value still ships.

---

## 10. Failure modes

- **Double-start** → lock file per vendor. One connection per account on both. Fail loud (Rule 18).
- **Slow consumer** → Schwab code 30 `STOP_STREAMING` terminates for client slowness. Never compute on either read loop. Alpaca will also drop a slow consumer.
- **Websocket drop** → the existing exponential backoff in `AlpacaBarStream.run()` is the pattern; mirror it for Schwab, re-`LOGIN`, wait for the response, then `ADD`.
- **Schwab code 20** → subscribed before the LOGIN response, or mutated `SchwabClientCorrelId`. Your bug, not theirs.
- **Refresh-token expiry** → 7 days, hard. `max_token_age` at 6, Windows toast, Sunday re-auth as routine.
- **Clock skew** → measure at connect, correct, expose. Classification is timestamp-driven across two vendors, so skew between Alpaca and Schwab timestamps corrupts pull/fill cross-referencing specifically. Measure both against a common reference, not against each other.
- **Session boundaries** → Alpaca timestamps are unambiguous UTC; partition accumulators by session anyway so the open doesn't read as a volume explosion.
- **Halt / LULD** → Alpaca emits trading status messages (`T:"s"`). Subscribe to them, mark the window, don't compute pace across it.
- **Thin tape (SNXX)** → minimum-sample guard, §8.3.
- **Daemon restart mid-session** → reload from the day's SQLite and recompute rather than starting cold.
- **Disk** → full tape fills disks quietly. Monitor free space and fail loud at a threshold rather than on write failure.

---

## 11. What this still won't do

**No Level 2 outside NASDAQ.** Schwab's `NASDAQ_BOOK` is NASDAQ's book, not consolidated depth. Displayed size elsewhere is invisible.

**Book is throttled snapshots.** Full MPID detail at an unpublished interval. Sub-interval activity never existed for you.

**Dark and off-exchange prints arrive without venue attribution beyond the tape/exchange code.** You will see the print, not the intent behind it.

**I'm turn-based.** Even with a perfect feed I only execute when you send a message. I cannot watch, alert, or trigger on a condition. Alerting belongs in the daemon writing to a Windows notification or a webhook, entirely independent of me.

The feed makes my answers better when you ask. It does not make me a monitor.

---

## Appendix — verification log

Verified 2026-08-14, this session:

- Schwab Streamer API contract, service list, field tables, book contract, response codes — developer.schwab.com, authenticated
- Schwab Market Data OpenAPI spec, 10 endpoints, `/pricehistory` minute floor — same
- Schwab app `trading-feed-daemon` created, Ready For Use — developer.schwab.com/dashboard
- Alpaca Algo Trader Plus $99/mo, SIP = all US exchanges, 100% volume, trades/quotes/bars streams — alpaca.markets/data and docs.alpaca.markets
- Algo Trader Plus Active across all three Alpaca accounts — user-supplied dashboard screenshot
- `config/settings.yaml` line 12 `alpaca_data_feed: iex` — read from disk
- `data/alpaca_market_data.py` one-connection-per-account note, line 16 — read from disk
- Working tree clean, remote `origin` → `NZ1979/trading-model-llm.git` — `git status`/`git remote -v` on device

Verified 2026-08-14 post-market, live tape (see §1a):

- SIP is consolidated — 7 trade exchanges, zero IEX prints
- Alpaca quotes are NBBO; bid and ask come from different venues
- Condition codes in play: `@`, `T`, `I`, `F`; odd lots are the only
  non-last-eligible code observed
- Quote and trade sizes are both in shares, contradicting Alpaca's
  "round lots" doc phrasing
- Godzilla clock skew of 2.41s found and corrected

Shipped: `8909f99` (SIP restore + doc corrections), `c868ceb`
(`AlpacaMarketStream`, 957 tests green), `2300062` (live verification script).

Outstanding `UNVERIFIED:` items —

- Schwab `NASDAQ_BOOK` throttle interval (unmeasured; bounds every book metric)
- Whether `main.py`'s bar path tolerates added trade/quote volume on the shared
  connection without backpressure. Untested at watchlist scale: live runs used
  1–3 symbols in post-market, which is nowhere near the load of 503 names in
  RTH.
- Exchange-letter to venue mapping (D = FINRA ADF etc.) is from training, not
  verified against Alpaca's reference. Verify before any code branches on it.
- Whether the full CTA/UTP non-last-eligible set extends beyond `I`. Only four
  codes have been observed, all in post-market.
