> **CORRECTED 2026-08-16 — the options-entitlement research below is not a
> finding.** Read `SESSION_RESUME_2026-08-16.md` first. This file is
> deliberately left unedited as a record of what was believed on 08-15.
>
> The section "Options data — the investigation that overturned yesterday's
> conclusion" explains `isDelayed: true` through OPRA licensing, the
> `entitlement` parameter and `PP-PayingPro`/`NP-NonPro` client
> classifications. The likely cause was far simpler: the 08-14 token was
> **partial**, carrying no `refresh_token`, and a degraded grant appears to
> have returned degraded data. A complete token returns `delayed=False`.
> **Treat that section as explaining an artifact, not as research.**
>
> What in it DOES stand, independently confirmed against live data on 08-16:
>
> - Algo Trader Plus already includes real-time OPRA options data — quotes,
>   trades, greeks, implied vol. Confirmed by a live call.
> - Alpaca's market data API has NO open-interest field. Its OI lives on the
>   trading API at T+2 with no history.
> - Open interest is a once-daily T+1 figure for everyone, at every price
>   point. That reframing is correct and load-bearing.
>
> Also superseded: the architecture note "OI walls from Schwab / IV and greeks
> from Alpaca" was premised on Schwab being delayed. Schwab covers both.
> Alpaca is still worth having for a real-time SIP underlying and as a
> cross-check — which proved its worth the same day, via the spot-consistency
> guard — but it is no longer load-bearing.


# LLM Model — Session Resume Snapshot (2026-08-15)

## How to use this file

Read this before `SESSION_RESUME_2026-08-14.md`. That file remains accurate on
everything it describes, but this session **changed the build plan**, so where
the two disagree about what to build next, this file wins.

`docs/FEED_SPEC_V4.md` is still the reference for what the feed layer is and
what each vendor is good at. Its §9 build order is **superseded** — see
"The scope correction" below.

`SESSION_RESUME_2026-05-12.md` is three months stale. Ignore it.

---

## What happened, in one paragraph

The session opened on yesterday's handoff prompt and got two commands in
before the clock check found Godzilla 2.23 seconds slow again — one day after
being resynced to 1.6ms. Root cause was not drift: the Windows Time service
was `Stopped` / `Manual`, so `w32tm /resync` had been a one-shot correction
with nothing maintaining it, and the clock free-ran at ~41 ppm. That is now
fixed properly. In parallel, a demanded deeper investigation into Schwab's
delayed options data overturned the prior session's conclusion and surfaced
that **real-time OPRA options data is already paid for and unused** on the
existing Alpaca subscription. Two process rules were written and shipped after
a run of incorrect inferences. The session ended with the user identifying
that the build had drifted well beyond the original request, and choosing to
pivot to a much smaller one.

---

## The scope correction — read this before planning any work

**The original request was: "let me ask you to look at live stock and options
data on demand, when I ask, to help me analyze a stock."**

`docs/FEED_SPEC_V4.md` §7 describes exactly that — an MCP server exposing
`get_snapshot`, `get_flow`, `get_levels`, `get_bars`, `get_pace` to Claude
Desktop — and §11 states plainly that the model is turn-based and cannot
watch or alert. The goal was never lost. **The build order was wrong.**

Phases 0-2 built a websocket daemon and a tick corpus. Phase 3 is aggressor
classification. The MCP server — the only component the user interacts with —
sits at phase 4 and beyond. After two full sessions, the user still could not
ask about a stock.

The work sorts cleanly by what it actually requires:

- **Needs no daemon; plain REST at request time.** Price, bid/ask, spread,
  session OHLC, volume, VWAP, bars at any lookback, gap %, RVOL against a
  Polygon baseline, full option chain, OI levels, greeks, implied vol,
  25-delta skew.
- **Needs persistence, but only a once-daily snapshot.** Day-over-day open
  interest change. OI publishes once and is unrecoverable if unrecorded —
  which is why `data/chain_store.py` had to exist and why that work stands
  regardless.
- **Genuinely needs a live streaming daemon.** CVD, aggressor classification,
  exact volume-at-price, large-print detection, book pull/fill. Real
  capabilities, but microstructure tooling rather than "help me analyze this
  stock."

**Decision taken: build the small version first, use it, then decide whether
the microstructure layer is ever wanted.** The tick daemon and corpus stay on
disk, dormant. Nothing is deleted and nothing is wasted.

---

## Production state

### Workstation clock — root-caused and fixed

The 2026-08-14 finding ("clock was 2.41s slow, fixed with `/resync`") was a
Rule 14 violation in miniature: the 1.6ms reading was taken at the instant of
correction and never re-checked. It did not hold.

- **Root cause: `W32Time` service was `Stopped`, StartType `Manual`.**
  `w32tm /resync` trigger-started it, corrected the clock, and the service
  stopped again. Nothing maintained it. `w32tm /query /status` reported
  `Source: Local CMOS Clock`, Stratum 0, Leap Indicator 3.
- **Measured hardware drift: ~41 ppm.** That is 3.5 s/day, 25 s/week, and it
  exhausts the 500 ms quote-age budget in 3.4 hours. A normal crystal is
  5-20 ppm.
- **Fixes applied (all from elevated PowerShell):**
  - `Set-Service W32Time -StartupType Automatic` + `Start-Service`
  - `w32tm /config /manualpeerlist:"time.cloudflare.com,0x9 time.nist.gov,0x9 pool.ntp.org,0x9" /syncfromflags:manual /update`
    — replacing `time.windows.com` alone, which is high-jitter
  - `HKLM\...\W32Time\Config\UpdateInterval` **360000 → 100** (the main lever;
    360000 is the standalone-workstation default and applies clock discipline
    far too slowly for sub-100ms work — domain members default to 100)
  - `HKLM\...\W32Time\Config\FrequencyCorrectRate` **4 → 2**
  - `HKLM\...\NtpClient\SpecialPollInterval` **1024 → 256**
- **Result:** offset fell 82.9 ms → 18.4 ms in 32 minutes, the first decrease
  of the session. Every prior reading climbed. Net −34 ppm against a +41 ppm
  hardware bias means roughly −75 ppm of correction is being applied.
- **Offsets are slewed, not stepped.** `MaxAllowedPhaseOffset` is 1 s, so
  anything under a second is corrected gradually. No timestamp discontinuities.

`UNVERIFIED:` that the fix **holds**. 18.4 ms is one reading on a falling
curve and could be mid-swing; a phase-locked loop can overshoot. Two re-runs
are owed — see "First thing to check next session".

`UNVERIFIED:` that `SpecialPollInterval` is still 256. It was set, then the
service was restarted, but `w32tm /query /configuration` was never re-run
afterward. An earlier `w32tm /config /update` silently reset this same key
from 900 to 1024, so it has a demonstrated habit of being clobbered.

`UNVERIFIED:` whether `ClockSkewMonitor` in `data/feed_daemon.py` would have
caught any of this. It is supposed to alert above 500 ms. It has never fired
outside a test.

### Schwab

- Token `OK`, **expires 2026-08-21** (checked 2026-08-15, 6.38 days remaining).
  Re-auth Sunday as routine rather than hitting it mid-week.
- `isDelayed: true` — **the 2026-08-14 conclusion was wrong.** See the options
  data section below. It appears to be an account-level entitlement, not an
  Individual Developer tier ceiling.

### Alpaca

- **Algo Trader Plus already includes real-time OPRA options data.** This has
  been paid for since the subscription started and has never been called. The
  pricing page comparison row reads "US Options (Opra) — Yes, indicative /
  Yes, real-time."
- `data/alpaca_market_data.py` is **websocket-only**. There is no Alpaca REST
  client anywhere in this codebase. Every equity number the platform has ever
  consumed arrived by streaming subscription. This is the single largest gap
  for on-demand queries.

### RVOL gates — unresolved data-integrity question

The two sides of the RVOL ratio come from different vendors:

- **Threshold:** `scripts/build_pm_rvol_thresholds.py` pulls 180 days of
  **Polygon** 1-minute bars, P85 per ticker, refreshed daily at 08:30 ET.
  Polygon is full-market consolidated.
- **Runtime numerator:** live **Alpaca**, which was IEX-only from 2026-05-14
  to 2026-08-14.

That is not a symmetric degradation that partly cancels. A real numerator at
5-15% of true was measured against a full-market denominator, making the gate
roughly **7-20× stricter than the 5× it reads as** — a nominal 5× threshold
demanding 35-100× real volume. That gate almost certainly did not fire for
three months.

`HYPOTHESIS:` and explicitly **not** a retroactive explanation for the
2026-04-28 "zero qualifying setups across 503 stocks" mystery. That was
diagnosed before the 2026-05-14 IEX flip. The timing does not line up.
Different bug.

`UNVERIFIED:` whether `config/pm_rvol_thresholds.json` exists on disk. A read
attempt returned nothing. If it is absent, every lookup falls through to
`HARD_FALLBACK_THRESHOLD = 5.0` and the per-ticker Phase C work is not
reaching production at all.

---

## What shipped

| SHA | Deliverable |
|---|---|
| `95ee009` | Rule 28 — plausible mechanism is not evidence |
| `0c719ea` | Rule 29 — a message containing a command ends with that command |
| `5938752` | `data/chain_store.py` + `tests/test_chain_store.py`, 24 tests |

Test suite **1032 → 1056**, all green on Python 3.14 on Godzilla.

### `data/chain_store.py`

Rolling SQLite store for option-chain snapshots. Deliberately **not** shaped
like `TickStore`: synchronous, one transaction, no queue, no worker, no drop
accounting. Those exist in `TickStore` because it sits under a live tape that
disconnects slow consumers. A chain fetch is a few thousand rows once a day
off any hot path; porting the async machinery would add failure modes guarding
a load profile that cannot occur.

Design decisions worth not re-litigating:

- **One rolling DB, not one per date.** OI change is a cross-day self-join.
- **UPSERT idempotent on `(session_date, symbol)`.** A retry after partial
  failure must not create a second row — that would double every OI diff
  crossing that date. Explicitly tested.
- **The T+1 trap is encoded, not just documented.** OI in a chain fetched on
  D is the close of D−1, so diffing fetches on D and D−1 gives the change
  between the closes of D−2 and D−1. `OIChange` carries `as_of_close` and
  `prior_close` as fields distinct from the fetch dates, so a caller cannot
  label it "yesterday's change."
- **Prior session is the previous *stored* session, not the previous calendar
  day.** Friday → Monday resolves to Friday. A recorded failure never becomes
  a baseline, which would otherwise diff against an empty chain and report
  every contract as new accumulation.
- **New contracts are excluded from `oi_change()`**, surfaced separately by
  `new_contracts()`. Absent-treated-as-zero would report a newly listed
  strike's entire OI as one day's accumulation.
- **`min_days_to_expiration=1` default**, carrying forward the 0DTE guard.
- **Stale rows on a narrower same-date re-fetch are KEPT, not purged.**
  User decision, 2026-08-15. Purging fails silently and unrecoverably (that
  day's chain cannot be re-fetched once the day passes); keeping fails
  visibly and stays correctable. Stale rows remain identifiable by a
  `fetched_at_ms` older than that date's `chain_fetches` entry.

### Rules 28 and 29

Written after four incorrect inferences in one session. Both are in
`CLAUDE_PREFLIGHT.md`, Rule 28 at line 551, Rule 29 at line 613.

- **Rule 28** — plausible mechanism is not evidence. Closes a gap in Rule 14,
  which governs *fix and diagnosis* claims but not *explanatory* ones.
  "Probably" is a tripwire, not a banned word: reaching for a hedge is the
  signal that the investigation is unfinished. Three permitted outcomes —
  conclusion with evidence, bare `UNVERIFIED:` naming the missing test, or a
  surviving hedge that carries its investigation with it.
- **Rule 29** — one command per message, command last, mechanical check.
  Written after the same instruction was given conversationally and violated
  three more times.

---

## Options data — the investigation that overturned yesterday's conclusion

Yesterday's session recorded `isDelayed` as probably inherent to a retail
Individual Developer app. A four-track research pass found the opposite.

**The reframe that collapses the problem: open interest is not real-time data
and never was.** OI rides the OPRA tape as a once-daily batch (Category `d`
"Open Interest" messages in the OPRA Pillar spec), published around
**06:30 ET the next morning**. It is T+1 by construction for everyone at every
price point. Nobody sells real-time OI because it does not exist.

That splits the requirement:

| Need | Latency required | Status |
|---|---|---|
| OI walls, day-over-day OI change | T+1 daily | **Delayed data is fine** |
| IV skew, greeks, underlying price | Real-time | Delayed data is useless |

**Findings:**

- **Schwab `isDelayed` is account-level, not app-tier.** `/chains` takes an
  `entitlement` parameter documented as "applicable only for retail token,
  entitlement of client PP-PayingPro, NP-NonPro, PN-NonPayingPro" — Schwab
  classifies the *client*, not the app. The quote schema carries per-symbol
  `realtime` and `quoteType` fields, `quoteType` documented as "NBBO -
  realtime, NFL - Non-fee liable quote." Schwab API Support has stated that
  API entitlement mirrors schwab.com entitlement. Lumibot's Schwab docs say
  "No extra entitlements required for individual developers." schwab.com has
  a `Profile > Streaming Quotes` toggle covering options chains.
- **Alpaca Algo Trader Plus already includes real-time OPRA** — quotes,
  trades, greeks, implied vol — via `/v1beta1/options/snapshots/{underlying}`
  and a websocket at `wss://stream.data.alpaca.markets/v1beta1/opra`.
  `alpaca-py` auto-selects the `opra` feed when entitled. Two gotchas: the
  options stream is **msgpack only**, and there is **no `*` wildcard
  subscription** for option quotes.
- **Alpaca's open interest is T+2 and unusable for this signal.** It lives on
  the trading API (`/v2/options/contracts`), not market data, with no
  historical endpoint. Alpaca staff confirmed on their forum: "The open
  interest data for day 1 will always lag and be available on day 3."
- **thinkorswim RTD is real and officially documented.** Schwab's own learning
  center documents a Real-Time Data COM server, 87 fields including `DELTA`,
  `GAMMA`, `IMPL_VOL`, `OPEN_INT`, `VOLUME`. `pyrtdc` is a pure-Python COM
  client, updated April 2026. **Treat as fallback only** — requires TOS
  running and logged in, is Windows-COM-bound, and does not load chains
  automatically. Coupling an unattended process to an open GUI application is
  a poor trade.
- **Do not build on `tos-wsjson-client`.** Schwab's Online Services Agreement
  prohibits software that automates logon or "the process of obtaining …
  Market Information" outside browsers and Schwab-approved applications. RTD
  falls inside that exception; the reverse-engineered websocket does not.
- **If a paid OI source is ever needed:** Polygon Options Starter at **$29/mo**
  carries `open_interest`, greeks and IV, and its 15-minute delay is
  irrelevant to an end-of-day figure. That is the same $29 subscription
  previously recorded as blocking Phase 7 — it would have worked; the
  reasoning that dismissed it assumed OI needed real-time. Alternatives:
  ThetaData Value $40 / Standard $80 (best OI freshness, 06:30 ET T+1),
  Tradier $0 with a funded brokerage account. Avoid IBKR (100 market-data-line
  cap makes chain scanning impossible) and Massive/Polygon Advanced at $199
  (buys real-time quotes already held via Alpaca).
- **OPRA non-professional subscriber fee is $1.25/mo** and every retail vendor
  absorbs it. Professional is $31.50/device or $2,000/mo non-display.
  Trading own money on own workstation qualifies as non-professional —
  **but incorporating an LLC for the trading flips this to professional**, and
  at Massive that is a $199 → $1,999 jump. Worth knowing before any entity
  restructuring.

**Recommended architecture, costing nothing additional:**

```
OI walls + OI change  ->  Schwab /chains   (already built; delay irrelevant)
IV skew + greeks + RT ->  Alpaca OPRA      (already paid for, never called)
```

Structurally the same move `FEED_SPEC_V4` already made for equities when it
gave the tape to Alpaca and the book to Schwab.

---

## Open UNVERIFIED items

- Whether the clock fix holds over 2h and 24h.
- Whether `SpecialPollInterval` survived at 256.
- Whether `ClockSkewMonitor` actually fires above 500 ms.
- Whether Schwab's OI is T+1 or T+2. Support's "delayed by a day" phrasing is
  ambiguous between "normal" and "a day behind normal." If T+2, a real OI
  source is needed (Polygon Starter $29).
- Whether the schwab.com `Profile > Streaming Quotes` toggle flips
  `isDelayed`.
- Whether equities return real-time while options return delayed on the same
  Schwab token. One `/quotes` call with an equity and an OCC option symbol
  settles it.
- Whether `config/pm_rvol_thresholds.json` exists.
- Whether Alpaca SIP pre-market cumulative volume reconciles with Polygon's
  for the same symbol and window. **This is the RVOL discriminator and it
  matters before Monday's open.**
- Whether `main.py`'s bar path tolerates trade+quote volume at watchlist
  scale (carried forward from 2026-08-14, untouched).
- Schwab `/chains` rate limits — still unmeasured, still untested beyond one
  symbol.
- Whether the `mcp` Python SDK installs cleanly on Python 3.14.

---

## The new build plan

Three pieces, smallest first, transport proven before tools are written.

1. **`mcp/server.py` skeleton with `get_health()` only.** Registered in Claude
   Desktop and verified callable — from the desktop app *and* from a Cowork
   session, since locally-registered MCP servers are proxied through the same
   device bridge. This de-risks two unknowns immediately: whether the `mcp`
   SDK installs on Python 3.14, and whether Claude Desktop picks the server
   up. **Do not build five tools against an unproven transport.**
2. **`data/alpaca_rest.py`** — equity snapshot, bars, latest quote/trade, plus
   the options chain snapshot with greeks and IV. This is the largest new
   piece because no Alpaca REST client exists.
3. **`analysis/option_walls.py`** — walls, flow, IV skew lifted out of
   `scripts/fetch_option_chain.py` into a tested module. Needed either way.
   Follow the shape of `analysis/futures_walls.py` (pure detection function,
   frozen dataclass, monitor wrapper) but **do not port its persistence /
   anti-spoofing layer** — that exists because displayed size can be spoofed,
   and open interest is settled and cleared and cannot be.

Target MCP tool surface: `get_health`, `get_snapshot`, `get_bars`,
`get_option_chain`, `get_oi_change`.

Deferred indefinitely, on disk, not deleted: `data/feed_daemon.py`,
`data/tick_store.py`, phases 3 and 5-8 of `FEED_SPEC_V4`.

---

## First thing to check next session

1. Anchors (Rule 23/25): `date && TZ=America/New_York date`, working directory
   `C:\trading\LLM model`, workstation Godzilla. **Godzilla is on Mountain
   time.** Do not extrapolate elapsed time between measurements (Rule 28).
2. **Clock hold check** — `w32tm /stripchart /computer:time.windows.com
   /samples:5 /dataonly`. Baseline: 18.4 ms at 13:31 on 2026-08-15. Want
   single-digit or small and stable. A negative offset means it overshot and
   is oscillating.
3. **Confirm the poll interval survived** — `w32tm /query /configuration`,
   expecting `SpecialPollInterval: 256`.
4. **Schwab token** — `python -m scripts.schwab_login --status`. Expires
   2026-08-21. Re-auth Sunday.
5. `git status` — expect clean at `5938752` or later.
6. **Monday's open is the first RTH session the SIP flip touches the signal
   engine's RVOL gates.** Run the Alpaca-vs-Polygon pre-market volume
   reconciliation *before* the open, not after.
