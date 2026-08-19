# Handoff — open this in a new session

Paste this whole file as your first message.
Written 2026-08-19 12:46 MDT, market still open.
**Supersedes `SESSION_HANDOFF_2026-08-18.md`.**

---

## Read these first, in this order, before doing anything

1. **`CURRENT_SCOPE.md`** (now 425 lines) — what this project IS. It gained an
   **Authorised exception** section on 2026-08-19. Read that section carefully;
   a process now runs unattended and it is not drift.
2. **This file.**
3. **`CLAUDE_PREFLIGHT.md`** (692 lines) — the rules, through Rule 30.
4. `SESSION_HANDOFF_2026-08-18.md` for the chain-store findings, which still
   hold. Ignore its "Schwab token expires 2026-08-21" line — see Corrections.

`C:\trading\LLM model` still contains **two projects sharing one directory.**
The dormant autonomous signal generator (`main.py`, `strategy/`, `execution/`,
everything in `docs/`) is **read for constraints, never for scope.**

---

## Verify the anchors first (Rule 25)

```
date && TZ=America/New_York date     # Godzilla is Mountain; quote MT only
working directory: C:\trading\LLM model
workstation: Godzilla
```

The Cowork container clock rolls to the next UTC day in the evening while
Godzilla has not. Measure Godzilla time in the same message you cite it.

---

## Already committed — do NOT redo this

The 2026-08-19 work is **committed and pushed**. Verified `working tree clean`
and `up to date with 'origin/main'`.

    f8a84f7   SNDK structure model: gamma exposure, persistent bars,
              OI change, dashboard, collector    12 files, 3,924 insertions
    b99dfc4   gitignore the persistent bar store

Test count went **1198 -> 1231**. Remote is `origin` ->
https://github.com/NZ1979/trading-model-llm.git.

Files now in the repo:

    analysis/gamma_exposure.py          gamma exposure + flip solver
    tests/test_gamma_exposure.py        33 tests
    data/price_store.py                 persistent bars, phase-split
    scripts/backfill_bars.py            Alpaca equity backfill
    scripts/oi_report.py                day-over-day OI change
    scripts/probe_alpaca_options.py     vendor capability probe
    scripts/sndk_dashboard.py           the dashboard
    scripts/collector.py                the unattended collector
    scripts/alert.py                    level + feed-health alerting
    scripts/selftest.py                 alert.py regression suite
    CURRENT_SCOPE.md                    modified, 380 -> 425 lines

`data/bars/`, `data/chains/` and `data/live/` are gitignored — generated
stores, not source. **`chains.db` is NOT regenerable**: open interest has no
history endpoint at any vendor, so back it up outside git. `bars.db` IS
regenerable via `scripts/backfill_bars.py`.

Start from "Next, in priority order" near the end of this file. Item 1 there
is now done; begin at item 2, the peer universe backfill.

---

## What is running right now

Two PowerShell windows on Godzilla. Both were restarted at ~12:40 MDT after an
incident (see Operational).

```powershell
# window 1
.\.venv\Scripts\python.exe -m scripts.watch SNDK

# window 2
.\.venv\Scripts\python.exe -m scripts.collector --symbols SNDK
```

`scripts.collector` is the **authorised exception** to CURRENT_SCOPE's
"nothing autonomous" rule, granted by the user 2026-08-19 in conversation and
recorded in that file. **Do not delete it as drift.** Its boundary:

    MAY   write to disk — bars, chains, the dashboard
    MAY NOT   write to the user — no alerting, deciding, or order entry

`scripts/alert.py` predates the exception and is run manually by the user. The
collector must never gain the ability to start it.

Cadence: bars every 5 min while any session is open; chains at **08:00 and
16:15 ET**; dashboard every 5 min.

**To tell "running and quiet" from "dead since 06:00"**, read
`data/live/collector_state.json` — heartbeat, last success per task, per-task
error counts. The console cannot distinguish those two states; that file can.

---

## State of the data as of 2026-08-19 12:46 MDT

```
chains.db   20260817 1,960 | 20260818 3,176 | 20260819 3,300 contracts
bars.db     20,452 bars, 23 sessions, 2026-07-20 -> 2026-08-19
            PRE 6,873 | REGULAR 8,699 | POST 4,880
watch       pid 2120, errors 0
SNDK        1,562.33  -3.90%   O 1682.405  H 1698.9999  L 1542.00
            volume 13,602,443 = 86.3% of 10-day ADV
```

---

## What this session built, and why each piece exists

### `data/price_store.py` + `scripts/backfill_bars.py`

**Nothing in the repo persisted price.** `scripts/watch.py` holds an in-memory
deque only; `data/ticks/` had one file from 2026-08-14. Worse, the ring buffer
silently decays the session's own extremes — at 09:48 MDT `derived.watch_high`
read **1640** against a true session high of **1698.9999**. A $59 error in a
field named "high".

The store keeps phase as a written column, not a query, because pre-market
volume runs one to two orders of magnitude below regular hours and any RVOL or
pace figure computed across the boundary is meaningless. `session_extremes()`
returns them split, so the pre-market low (1597.70) can never be quoted as the
regular-hours low (1542.00).

### `analysis/gamma_exposure.py` + 33 tests

`option_walls.py` had walls, skew and coverage but no GEX. This adds exposure
by strike, a flip solver, and dual-basis reporting.

**It rests on an assumption that cannot be verified from public data:** that
dealers are long calls and short puts. Treat the flip level as the output and
the sign convention as the thing to falsify.

### `scripts/oi_report.py`

Day-over-day OI change. The FLOW table in `fetch_option_chain` ranks on
volume/OI, which measures turnover and carries **no direction** — a contract
can print huge volume as churn or as closing trades. OI change is the only
field that says whether positions were opened or closed.

### `scripts/sndk_dashboard.py`

Six panels: stat tiles, price-against-levels ladder, GEX by strike, OI walls,
OI change, pre-market setup. Reads only from disk — no network.

Renders `data/live/SNDK_dashboard.html`, self-contained, dark and light. Open
with `Invoke-Item`. **There is no hosted URL and no auto-refresh** beyond the
collector regenerating it every 5 minutes.

The **gamma flip is drawn as a band, not a line**, because the two gamma bases
can disagree and a line asserts precision they do not share.

### `scripts/collector.py`

See "What is running right now".

### `scripts/probe_alpaca_options.py`

Settled what the vendors actually provide. See Vendor capability.

---

## Findings that change how you read things

### 1. Trap #5 poisoned every intraday number I quoted

`watch.py` auto-fetches the chain at `strike_window: 40`. Every gamma and wall
figure computed from it was a windowing artifact:

| | ±40 | full ±200 |
|---|---|---|
| gamma flip | ~1570 | **1590.32** |
| total GEX | +31.0M | **+12.9M** |
| short-gamma pocket | −18.1M over 1500–1570 | **−89.1M over 1232–1589** |
| largest call wall | 1600 (6,931 OI) | **2000 (12,364 OI)** |

Strike 2000 is structurally invisible at ±40 and is the **fastest-growing wall
on the board**: +3,160 net calls, zero puts, during the 08-18 session.

**Always compute walls and GEX from a `--strikes 200` fetch.** And never
aggregate chain-wide from the DATABASE — `chain_snapshots` is
`UNIQUE(session_date, symbol)`, so a narrow fetch overwrites a wider one and
any rollup sums across vintages. Per-contract joins (`oi_change()`) are immune.

### 2. Vendor gamma goes stale within minutes, and staleness looks like disagreement

Vendor gamma is published against the underlying **in that same chain fetch**.
A chain fetched 04:45 carried gamma against 1594.60. Read at spot 1561.94 —
2.1% away — the vendor basis said **+3.5M (dampening)** while Black-Scholes
said **−30.1M (amplifying)**. Opposite regimes, identical open interest.

Re-fetching at the live spot collapsed the disagreement; both read negative.
**Most of the apparent model divergence was my own stale input.**

`gamma_profile` now rejects vendor gamma past 0.5% drift and exposes
`vendor_gamma_rows`, `spot_drift_pct`, `vendor_gamma_stale`.

**Open problem:** 0.5% is ~8 points on SNDK, cleared within minutes, so the
vendor basis is effectively unavailable intraday and the dual-basis
cross-check is mostly dark. Fix is a third midday chain fetch, or scaling the
threshold to realized volatility. Prefer the latter — a flat percentage across
every symbol is the same class of error as a flat pace threshold across every
regime.

### 3. OI and volume have opposite optimal fetch times

OI is T+1 and final by pre-market. **Volume only completes after the close — a
pre-market fetch records 0 for every contract.** The 08-18 chain was fetched
07:30 ET, so its volume column was uniformly zero and the day-over-day OI
change could not be attributed to flow at all. That cost three iterations of
`oi_report` to find.

The 08-18 handoff's "run pre-market or early" is right for OI and guarantees
useless volume. Hence two fetches, both `--strikes 200`.

### 4. Twenty-three sessions of history corrected three live claims

| Claim from the tape | What history says |
|---|---|
| today's 10.18% range was extraordinary | median **8.84%**, max 19.72% — a 57th-percentile day |
| front-week IV ~100 was enormous | realized c2c **150**, Parkinson **102**; IV/RV 0.68 — **underpriced** |
| the gap fill was decisive | gaps ≥3%: 15 occurrences, only **5 filled** |

The IV error is repo **trap #7** near-verbatim. Always name the baseline and
window before calling anything high, low, or extreme.

**Close-to-close 150 against Parkinson 102 means most of this stock's movement
happens between sessions, not during them.** An intraday-only model misses the
majority of the risk.

### 5. The pre-market setup model — the user's actual ask

User's framing, worth keeping verbatim:

> "There was a large, strong move up in the premarket to clear a resistance
> level of $1700. I would be looking for information to support or deny an
> expected reversal back down to existing OI levels near $1600 at the open.
> Which is exactly what happened today."

Measured on 16 pre-market advances ≥2% across 23 sessions. **The
discriminating variable is not the size of the advance but how much has been
surrendered before the bell.**

| Setup at the open | n | round-tripped to prior close | median close-vs-open |
|---|---|---|---|
| opened NEAR the pre-market high (giveback <1%) | 7 | **2/7 — 29%** | −0.30% |
| already FADING into the open (giveback ≥1%) | 9 | **7/9 — 78%** | +2.76% |

2026-08-19 gave back 1.76% → fading bucket → 78% base rate → **it
round-tripped**, low 1542.00 through a 1625.78 prior close.

The number worth acting on: **median MAE from the open is −3.53%** across all
16. Even sessions finishing green draw down that far first.

Also 69% (11/16) made a new high above the pre-market high — 08-19 was one of
the 31% that did not, a second confirming tell. And only **1 of 22** sessions
stayed inside its pre-market range: **the pre-market range is a launchpad, not
a container.**

**n=7 and n=9.** A promising split in this sample and nothing more. The
confidence interval on that 29% runs roughly 4% to 71%. Validation needs the
peer universe.

---

## Vendor capability — settled, do not re-litigate

| | Alpaca | Schwab |
|---|---|---|
| IV, greeks, quotes, option bars | ✅ real-time + historical to Feb 2024 | ✅ current only |
| **Open interest** | ❌ absent from market data entirely | ✅ T+1, **no history** |

Greeks ARE returned by Alpaca. The first probe missed them because
alphabetical OCC ordering sampled strikes 280/780/1190 — deep ITM, barely
quoted. Near the money: **100/100 populated**. Sample bias, not a missing
feature. Use `--spot` to band the probe around the money.

**Historical greeks are derivable** — option closes (Alpaca bars) + underlying
bars (`bars.db`) + strike/expiry from the OCC symbol → back out IV → compute
gamma. **Historical open interest is not derivable from anything.**

So the only thing money buys is historical OI. Massive (formerly polygon.io;
the pricing page 302s to massive.com — **UNVERIFIED** whether API hostnames
moved, which would break `data/polygon_feed.py`) Options Starter is $29/mo
with 2 years, covering SNDK's entire life since its **2025-02-24** listing.
Developer at $79 buys 4 years, reaching the 2022 memory downturn — the only
regime where these mechanics can be tested against a falling tape. **SNDK's own
18 months are a single uptrend; a model fit on it learns "it goes up."**

Decision still open.

---

## Corrections to earlier documents

- **Schwab token expires 2026-08-23**, not 08-21. `SESSION_RESUME_2026-08-14.md`
  is stale. Also check `has_refresh_token`, not just `auth_state` — a partial
  token once reported five days of runway on dead access for 46 hours.
  **Friday 08-21 is the expiry holding 53% of OI within 60 days.** Do not let
  auth lapse into it.
- Test count was **1198** before this session, not the 1008 in the 08-14 doc.
- `derived.watch_high` / `watch_low` are **windowed by the 1440-sample ring
  buffer, not session extremes.** Always read `schwab.high_price` /
  `low_price`.

---

## Operational — an incident worth not repeating

**Two `scripts.watch` instances were running simultaneously**, both atomically
renaming onto the same `data/live/SNDK.json`. That is the likely cause of the
three `WinError 5` PermissionError failures — **not** the reader contention I
asserted three times during the session. One watcher runs now; **if errors stay
0 through a full session the diagnosis is confirmed.** If they recur on a
single writer, it is something else.

**Four `scripts.collector` instances** were also running, from repeated starts.

To see what is actually running:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, ParentProcessId,
    @{n='Script';e={($_.CommandLine -split '\s+-m\s+')[-1]}} |
  Format-Table -AutoSize -Wrap
```

Every PowerShell window spawns a `conhost.exe` child, so a naive "has children"
test to find idle windows is useless. Filter to `python.exe`.

**During cleanup, `scripts.watch` PID 23048 stopped at the same moment as a
`Stop-Process` batch that did not list it.** Mechanism unknown. Assume a
`Stop-Process -Force` batch can take more than it names, and restart the
watcher deliberately afterwards rather than assuming it survived.

**A dry run must not touch real state.** The first `--dry-run` marked bars,
chain_am and dashboard as done without doing them, so a real start immediately
after would have skipped the catch-up chain fetch. Now writes to
`collector_state.dryrun.json`.

---

## Next, in priority order

1. ~~Commit and push.~~ **DONE** — f8a84f7 and b99dfc4, working tree clean.
2. **Peer universe backfill** — `--symbol MU,WDC,STX,NVDA,AMD,SMH` plus ~25
   liquid non-semis. Moves the pre-market buckets off n=7/n=9 toward n≈500 and
   lets the gamma mechanics be validated across names and both directions
   rather than one stock in one uptrend.
3. **Scale the gamma drift threshold to realized volatility**, so the
   dual-basis cross-check is not permanently dark on fast names.
4. **`analysis/premarket_setup.py`** — the bucket model as a real module with
   tests, rather than constants embedded in the dashboard.
5. **Decide on the $29 OI history.**
6. **Re-auth Schwab before 2026-08-23.**
7. Retry-with-backoff around the `SNDK.tmp -> SNDK.json` rename in
   `scripts/watch.py`, now that multi-writer contention is understood.
8. Widen `--strikes` beyond 200 if any expiry's stored contracts reach the
   edge of the window; 08-21 already runs to 2590.

---

## Working style — enforced repeatedly, and it mattered

- **ONE command per message, and the message ENDS with it.** (Rule 29)
- **State expected values BEFORE the command, and COMPUTE them.** A falsifiable
  prediction beside a command is how three separate bugs surfaced today.
- **Label every command block with the machine and the directory.** A fresh
  PowerShell window opens in `C:\windows\system32`; include the
  `Set-Location`. Omitting it wasted a round trip on 08-19 (Rule 20).
- **Quote Godzilla (Mountain) times only**, measured in the same message.
- Direct and concise. No filler, no "great question", fewer em dashes. Do not
  repeat the question back. Do not hedge with "it depends" — recommend.
- The user is **not** an options specialist. Explain mechanisms, use analogies.
- **Never handle credentials.** They live at
  `%LOCALAPPDATA%\trading\schwab.env`, ACL-restricted. Report presence, never
  values or lengths.
- **Render visual output and look at it before shipping.** Three dashboard
  defects — an SVG rect emitted outside any `<svg>` element, colliding labels,
  and an axis driven by a far-OTM outlier — were invisible in code review and
  obvious in a screenshot.
- **The user catches real errors.** Treat pushback as signal. On 08-19 they
  caught a stale price quote, a missed break, and an unclear instruction, each
  of which led to a genuine fix.

---

## The metric traps, updated

The seven from the 08-18 handoff still hold. Three additions from 08-19:

8. **A field's timestamp is not the window you are measuring.** `OIChange.volume`
   is volume on the LATER fetch date, while the OI change describes the EARLIER
   session. Printing them side by side implied one window when they were two.
9. **Vendor greeks are only valid at the spot they were published against.**
   Reusing them after price moves produces a confident wrong answer rather than
   a missing one.
10. **A threshold calibrated in one regime is noise in another.** Pace ran
    1,300/min in the pre-market lull and 194,147/min in the opening minute — a
    150x span. Any fixed share-count gate is wrong somewhere. Recalibrating
    mid-session also invalidates any state machine seeded under the old
    threshold; re-seed at the session boundary.

**And the habit that would have caught most of them:** ask what a number
MEASURES before interpreting it, and name the baseline and window before
calling anything high, low, or unusual.
