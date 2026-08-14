# LLM Model — Session Resume Snapshot (2026-08-14)

## How to use this file

Read this before `SESSION_RESUME_2026-05-12.md`. That file is three months
stale and describes a world that has changed: the gap-and-go fork is shut
down, the data feed was silently degraded for three months, and a new
subsystem exists that it knows nothing about. Where the two disagree, this
file wins.

Then read `docs/FEED_SPEC_V4.md` — it is the build spec for the work in
progress and carries the verification log.

---

## What happened in this session, in one paragraph

The session started as "build a Schwab streaming feed into an MCP server" and
ended somewhere else. Schwab turns out to have no time-and-sales service at
all, so the original design was reconstructing a tape from conflated level-one
quotes. Reading this repo revealed that a real consolidated tape was already
paid for and switched off: `alpaca_data_feed` had been on `iex` since
2026-05-14 on the false premise that the account lacked Algo Trader Plus. It
does have it, on all three accounts. Flipping it back cost nothing and made
the whole Schwab pseudo-tape design unnecessary. Schwab's role shrank to
`NASDAQ_BOOK` depth, which is the one thing Alpaca cannot provide.

---

## Production state today

### Data feed — CHANGED, verify before assuming

- `config/settings.yaml` `alpaca_data_feed: sip` (was `iex` from 2026-05-14 to
  2026-08-14). **Every RVOL-gated signal computed in that window ran against
  IEX only**, which `data/alpaca_market_data.py` documents as understating
  pre-market volume to 5-15% of true. Gap-and-go requires RVOL >= 5x. Treat
  any signal analysis from that period as compromised.
- Algo Trader Plus is Active and covers all three Alpaca accounts (verified on
  the Alpaca dashboard 2026-08-14). There is no entitlement reason to cross
  accounts.
- Live-verified: 7 distinct trade exchanges, zero IEX prints in any sample.
  Alpaca quotes are consolidated NBBO with bid and ask from different venues.

### Workstation (Godzilla)

- **Clock was 2.41 seconds slow.** Found by comparing SIP timestamps against
  local time, confirmed with `w32tm /stripchart`, corrected with
  `w32tm /resync` to ~1.6ms. This would have silently invalidated the 500ms
  quote-age confidence tag and all cross-vendor timestamp correlation. No test
  could have caught it — it is an environment fault. **Re-check it at the
  start of any session doing timestamp work.** The daemon now monitors it
  continuously and alerts above 500ms.

### Schwab

- Developer app `trading-feed-daemon` created 2026-08-14, Production, **Ready
  For Use with no approval wait** (the "approval takes days" assumption was
  wrong for an Individual Developer with an approved line of business).
- Market Data Production + Accounts and Trading Production. **Order Limit 0** —
  Schwab throttles order requests to zero at their edge, so the
  trading/analysis separation is enforced outside this codebase.
- Accounts and Trading is attached only because `GET /userPreference` lives
  there and the streamer cannot authenticate without it. Not for order entry.
- Credentials in `%LOCALAPPDATA%\trading\schwab.env`, outside the repo, ACL
  restricted to `GODZILLA\kings` with inheritance stripped. Not yet used —
  no Schwab code has been written.

### Gap-and-go fork

- Reported shut down on or about 2026-08-12. **Rule 26 has been amended, not
  retired** — see the dated block in `CLAUDE_PREFLIGHT.md`. All prohibitions
  remain in force until the VPS at `5.161.199.155` is confirmed decommissioned
  in writing. "Shut down" is not "verified gone."

---

## What shipped (all pushed to origin/main)

| SHA | Deliverable |
|---|---|
| `8909f99` | SIP restored; tick-corpus gitignore rules; Rule 26 amended; stale blueprint sections flagged |
| `c868ceb` | `AlpacaMarketStream` — bars, trades, quotes, status on ONE connection |
| `2300062` | `scripts/verify_alpaca_sip.py` — live feed verification |
| `58eb7da` | `docs/FEED_SPEC_V4.md`; `tick_types.py` annotated with size evidence |
| `4d6a61f` | `data/tick_store.py` — WAL corpus writer, batched, loud drop accounting |
| `06c0885` | `data/feed_daemon.py` + `scripts/run_feed_daemon.py` + clock-skew monitor |

Test suite: 957 -> 982. Full suite green at every commit.

---

## New modules and what they are for

- `data/tick_types.py` — `Trade`, `Quote`, `TradingStatus`. Nanosecond
  integer timestamps, NOT `datetime`: Python truncates to microseconds and
  would collapse prints that are nanoseconds apart, corrupting the ordering
  the Lee-Ready tick test depends on. Sizes are SHARES throughout.
- `data/alpaca_market_data.py` — extended in place. `AlpacaBarStream` is now
  an alias for `AlpacaMarketStream`; `main.py`'s call site is untouched and
  acquires no lock. Tick callbacks are opt-in.
- `data/tick_store.py` — queue-fed batching SQLite writer, WAL, one DB per
  ET session date under `data/ticks/` (gitignored).
- `data/feed_daemon.py` — wires stream to store, plus `ClockSkewMonitor`.
- `scripts/run_feed_daemon.py` — entry point.
- `scripts/verify_alpaca_sip.py` — read-only live feed check.

---

## Constraints that drove the design (do not re-litigate)

1. **One market-data connection per account, both vendors.** Alpaca documents
   it in `alpaca_market_data.py`; Schwab enforces it with response code 12.
   This is why trades, quotes and bars share one class rather than three, and
   why there is a lock file.
2. **Both vendors drop slow consumers.** Schwab does it explicitly with code
   30 STOP_STREAMING. Nothing expensive may run on a read loop. Callbacks
   enqueue and return; all SQLite work is on a separate task.
3. **Queue saturation drops rows rather than applying backpressure.** Blocking
   would push backpressure onto the read loop and get the connection dropped,
   turning partial loss into total loss. Drops are counted and surfaced.
4. **Odd lots are not last-sale eligible.** 87-97% of post-market prints
   carried condition `I`. A charted "last" price can be 11+ seconds stale
   while raw prints keep arriving. Every price field must carry its age.
5. **~50% of prints are off-exchange** (condition/exchange `D`, FINRA ADF).
   Those have no lit-book counterpart, which bounds what pull/fill resolution
   can ever claim.

---

## What's left

- **Phase 3 — aggressor classification.** Bisect quote history at each print's
  timestamp, apply Lee-Ready, tag prints whose quote is older than 500ms.
  First phase that produces a number rather than a row.
- **Phase 4 — `get_pace`, `get_snapshot`, `get_bars`.** Backfill the 20-session
  volume baseline from `data/polygon_feed.py`. Highest analytical value per
  unit of work, no coverage caveats. This is the metric that would have caught
  2026-08-14.
- **Phase 5 — `get_flow`, `get_levels`.**
- **Phase 6 — Schwab auth + `NASDAQ_BOOK`.** Rule 22 logging audit required
  first: `schwab-py` is a new HTTP dependency.
- **Phase 7 — pull/fill via book x tape cross-reference.** The genuinely novel
  capability; neither vendor can do it alone.
- **Phase 8 — thesis registry + `replay`.** Read
  `docs/M2_REPLAY_HARNESS_DESIGN.md` first; do NOT build a second replay path.

---

## Open UNVERIFIED items

- Whether `main.py`'s bar path tolerates trade+quote volume on the shared
  connection at watchlist scale. Live runs used 1-3 symbols in a thin
  post-market tape. **The open on a liquid name is a different order of load
  and the queue-drop path has never fired outside a test.**
- Schwab `NASDAQ_BOOK` throttle interval — unmeasured, bounds every book metric.
- Exchange-letter to venue mapping (`D` = FINRA ADF etc.) is from model
  training, not verified against Alpaca's reference. Verify before any code
  branches on it.
- Whether the non-last-eligible condition set extends beyond `I`. Only four
  codes have been observed, all post-market.

---

## First thing to check next session

1. Rule 23/25 anchors: `date && TZ=America/New_York date`, working directory
   `C:\trading\LLM model`, workstation Godzilla.
2. **Clock skew.** `w32tm /stripchart /computer:time.windows.com /samples:5
   /dataonly`. It was 2.41s off once; drift returns.
3. `git status` — expect clean at `06c0885` or later.
4. Monday's open is the first session the SIP flip touches the signal engine.
   Watch whether RVOL-gated signals behave differently now that the volume
   denominator is real.

---

## Orienting phrases to start a new session

- "Read `SESSION_RESUME_2026-08-14.md` and `docs/FEED_SPEC_V4.md`, then
  confirm anchors and clock skew."
- "Continue the feed build at phase 3 — aggressor classification."
- "Check whether the SIP flip changed RVOL behaviour at the open."
