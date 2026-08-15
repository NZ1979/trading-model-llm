# LLM Model — Session Resume Snapshot (2026-08-14)

## How to use this file

Read this before `SESSION_RESUME_2026-05-12.md`. That file is three months
stale and describes a world that has changed: the gap-and-go fork is shut
down, the data feed was silently degraded for three months, and two new
subsystems exist that it knows nothing about. Where the two disagree, this
file wins.

Then read `docs/FEED_SPEC_V4.md` — the build spec, with the verification log.

---

## What happened, in one paragraph

The session started as "build a Schwab streaming feed into an MCP server" and
ended somewhere else twice. First: Schwab has no time-and-sales service, so
the original design was reconstructing a tape from conflated level-one quotes
— unnecessary, because a real consolidated tape was already paid for and
switched off (`alpaca_data_feed: iex` since 2026-05-14, on the false premise
that the account lacked Algo Trader Plus). Second: Schwab's `/chains` endpoint
turned out to deliver everything the deferred Phase 7 options-walls work
needed, at no cost, unblocking a phase that had been gated on a $29/mo Polygon
subscription. The equity feed reached phase 2 (tape persisting to a corpus)
and the options layer reached a working live chain fetch.

---

## Production state

### Data feed — CHANGED, verify before assuming

- `config/settings.yaml` `alpaca_data_feed: sip` (was `iex` 2026-05-14 →
  2026-08-14). **Every RVOL-gated signal in that window ran against IEX only**,
  which `data/alpaca_market_data.py` documents as understating pre-market
  volume to 5-15% of true. Gap-and-go requires RVOL ≥ 5x. Treat any signal
  analysis from that period as compromised — this is a data-integrity problem
  that has been stopped, not fixed retroactively.
- Algo Trader Plus is Active across all three Alpaca accounts.
- Live-verified: 7 distinct trade exchanges, zero IEX prints in any sample.
  Alpaca quotes on SIP are consolidated NBBO with bid and ask from different
  venues.

### Workstation (Godzilla)

- **Clock was 2.41 seconds slow.** Found by comparing SIP timestamps to local
  time, confirmed with `w32tm /stripchart`, fixed with `w32tm /resync` to
  ~1.6ms. Would have silently invalidated the 500ms quote-age tag and all
  cross-vendor timestamp correlation. No test could catch it. **Re-check at
  the start of any session doing timestamp work.** The daemon now monitors
  continuously and alerts above 500ms.
- Python 3.14. Note: pip's vendored `truststore` globally patches
  `ssl.SSLContext.wrap_socket`, which breaks any local HTTPS *server* —
  including schwab-py's OAuth callback listener. See "Schwab auth" below.

### Schwab

- App `trading-feed-daemon`, Production, **Ready For Use with no approval
  wait**. Market Data Production + Accounts and Trading Production.
  **Order Limit 0.**
- Accounts and Trading is attached only because `GET /userPreference` lives
  there and the streamer cannot authenticate without it. Not for order entry.
- Credentials in `%LOCALAPPDATA%\trading\schwab.env`, outside the repo, ACL
  restricted to `GODZILLA\kings` with inheritance stripped.
- **Authenticated 2026-08-14. Token valid 7 days from then — expires
  2026-08-21.** Re-auth with `python -m scripts.schwab_login`.
- **The automatic OAuth flow does not work on this machine.** `--manual` is
  the default for that reason (paste-the-URL flow, no local server).
- **Options data is DELAYED on this entitlement** (`isDelayed: true`). Open
  interest is a T+1 figure so walls remain valid; underlying price, volume,
  greeks and IV are not current. Unresolved whether a different entitlement
  fixes this.

### Gap-and-go fork

Reported shut down ~2026-08-12. **Rule 26 amended, not retired** — see the
dated block in `CLAUDE_PREFLIGHT.md`. All prohibitions remain in force until
the VPS at `5.161.199.155` is confirmed decommissioned in writing.

---

## What shipped

| SHA | Deliverable |
|---|---|
| `8909f99` | SIP restored; tick gitignore; Rule 26 amended; stale docs flagged |
| `c868ceb` | `AlpacaMarketStream` — bars, trades, quotes, status on ONE connection |
| `2300062` | `scripts/verify_alpaca_sip.py` — live feed verification |
| `58eb7da` | `docs/FEED_SPEC_V4.md`; `tick_types.py` size evidence |
| `4d6a61f` | `data/tick_store.py` — WAL corpus writer, loud drop accounting |
| `06c0885` | `data/feed_daemon.py` + runner + clock-skew monitor |
| `2c83ae1` | This session-resume file |
| `fa377bf` | Rule 22 audit for schwab-py (werkzeug/flask/authlib/schwab) |
| `533d881` | `data/schwab_auth.py` — 7-day token state machine, manual OAuth |
| *pending* | `data/schwab_chains.py`, `scripts/fetch_option_chain.py`, tests |

Test suite 957 → 1008.

---

## New modules

**Equity feed (phases 0-2 complete):**

- `data/tick_types.py` — `Trade`, `Quote`, `TradingStatus`. Nanosecond integer
  timestamps, NOT `datetime`: Python truncates to microseconds and collapses
  prints nanoseconds apart, corrupting the ordering Lee-Ready depends on.
  Sizes are SHARES throughout.
- `data/alpaca_market_data.py` — extended in place. `AlpacaBarStream` is an
  alias for `AlpacaMarketStream`; `main.py`'s call site is untouched.
- `data/tick_store.py` — queue-fed batching SQLite writer, WAL, one DB per ET
  session date under `data/ticks/` (gitignored).
- `data/feed_daemon.py` — wires stream to store, plus `ClockSkewMonitor`.
- `scripts/run_feed_daemon.py`, `scripts/verify_alpaca_sip.py`.

**Options layer (new):**

- `data/schwab_auth.py` — credential loading, 7-day token state machine
  (`OK` / `WARN_EXPIRING` / `AUTH_EXPIRED` / `NO_TOKEN` / `NO_CREDENTIALS`),
  `health()` for get_health. Nothing logs or reprs a credential.
- `scripts/schwab_login.py` — weekly re-auth. `--status` to check, `--manual`
  default.
- `data/schwab_chains.py` — `/chains` fetch and parse into `OptionContract`.
- `scripts/fetch_option_chain.py` — walls, flow, IV skew from one call.

---

## Constraints that drove the design (do not re-litigate)

1. **One market-data connection per account, both vendors.** Alpaca documents
   it; Schwab enforces with response code 12. Hence one stream class, not
   three, and a lock file.
2. **Both vendors drop slow consumers** (Schwab code 30). Nothing expensive on
   a read loop; callbacks enqueue and return.
3. **Queue saturation drops rows rather than applying backpressure.** Blocking
   would get the connection dropped, turning partial loss into total loss.
4. **Odd lots are not last-sale eligible.** 87-97% of post-market prints carry
   condition `I`. A charted "last" can be 11+ seconds stale while raw prints
   keep arriving. Every price field must carry its age.
5. **~50% of prints are off-exchange** (`D`, FINRA ADF), so pull/fill
   resolution can only speak to the lit portion.
6. **Open interest is T+1.** Volume/OI on a same-day expiry is a division
   artifact, not new positioning. See the metric traps below.

---

## Metric traps found by running against real data

Both of these produced confident, plausible, wrong output before being caught.
They are the reason to validate every new metric against a live sample before
trusting it.

- **The 0DTE volume/OI artifact.** OI is yesterday's close, so a contract
  expiring today has OI near zero and any volume yields a huge ratio. The
  first flow table ranked entirely on 0DTE churn showing `v/OI 1882` and
  read as massive put accumulation. Fixed with `--min-oi 250`, `--min-volume
  100`, and `DTE >= 1`; 0DTE is reported separately, ranked on raw volume.
- **IV explosion at expiry.** Implied vol diverges mechanically as time to
  expiry approaches zero. The first skew table used the expiring series and
  showed a delta +0.975 call at IV 245 and another at 375. Fixed by requiring
  `DTE >= 1` and restricting to OTM contracts, which is the standard skew
  convention. Now reports a 25-delta risk reversal.

---

## Live options reading, 2026-08-14 close (SNDK)

Recorded because it is the first real output and worth checking against what
actually happens next.

- Call walls: 1600 (OI 9,783), 1700 (7,420). Put walls: 1600 (4,570), 1700
  (2,117). 1600 is both — a magnet level.
- Put/call OI ratio 0.416 — call-heavy.
- Flow: all top rows calls. 8/21 1620 at v/OI 7.32, 1630 at 5.35 — genuine new
  call positioning.
- 25d risk reversal −3.2 = **call skew**. Equities normally carry put skew, so
  this is the market paying up for upside.
- 0DTE volume: 168,217 calls vs 67,309 puts.

Context: the 2026-08-14 session thesis was a fade whose 1,580 invalidation
fired at 1,565 and was rationalised past. The options market is positioned the
other way.

---

## What's left

**Equity feed:**

- Phase 3 — aggressor classification. Bisect quote history at each print's
  timestamp, Lee-Ready, tag prints whose quote is >500ms old.
- Phase 4 — `get_pace`, `get_snapshot`, `get_bars`. Backfill the 20-session
  volume baseline from `data/polygon_feed.py`. **The metric that would have
  caught 2026-08-14.**
- Phase 5 — `get_flow`, `get_levels`.
- Phase 6 — `NASDAQ_BOOK` streaming.
- Phase 7 — pull/fill via book × tape cross-reference. The novel capability.
- Phase 8 — thesis registry + `replay`. Read
  `docs/M2_REPLAY_HARNESS_DESIGN.md` first; do NOT build a second replay path.

**Options layer:**

- Persist chains to SQLite so OI changes day-over-day are measurable. Right
  now every fetch is a snapshot with no history, and OI *change* is the actual
  signal.
- Multi-symbol fetch with rate-limit handling. Untested beyond one symbol.
- `analysis/option_walls.py` — move wall/flow/skew computation out of the
  inspection script into a real module with tests.
- `LEVELONE_OPTIONS` streaming for intraday gamma. Last, and it hits the
  undocumented symbol cap.
- Resolve whether the delayed-data entitlement can be changed.

---

## Open UNVERIFIED items

- Whether `main.py`'s bar path tolerates trade+quote volume on the shared
  connection at watchlist scale. Live runs used 1-3 symbols in a thin
  post-market tape. **The open on a liquid name is a different order of load
  and the queue-drop path has never fired outside a test.**
- Schwab `/chains` rate limits — unpublished, untested beyond one call.
- Schwab `NASDAQ_BOOK` throttle interval — unmeasured.
- Exchange-letter → venue mapping (`D` = FINRA ADF etc.) is from model
  training, not verified against Alpaca's reference.
- Whether the non-last-eligible condition set extends beyond `I`.
- Whether options `isDelayed` can be resolved by entitlement change.

---

## First thing to check next session

1. Anchors (Rule 23/25): `date && TZ=America/New_York date`, working directory
   `C:\trading\LLM model`, workstation Godzilla.
2. **Clock skew:** `w32tm /stripchart /computer:time.windows.com /samples:5
   /dataonly`. It was 2.41s off once; drift returns.
3. **Schwab token age:** `python -m scripts.schwab_login --status`. Expires
   2026-08-21.
4. `git status` — expect clean.
5. Monday's open is the first session the SIP flip touches the signal engine.
