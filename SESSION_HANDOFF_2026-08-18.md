# Handoff — open this in a new session

Paste this whole file as your first message.
Written 2026-08-17 evening MDT, after the close.
**Supersedes the 12:30 MDT version of this file.**

---

## Read these first, in this order, before doing anything

1. **`CURRENT_SCOPE.md`** (380 lines) — what this project IS. Read all of it,
   including "What this project is NOT".
2. **`SESSION_RESUME_2026-08-17.md`** (311 lines) — TWO sessions in one day.
   Read the evening addendum at the bottom as well as the midday body; the
   addendum corrects four things the midday body states.
3. **`CLAUDE_PREFLIGHT.md`** (692 lines) — the rules, through Rule 30.

**`C:\trading\LLM model` contains TWO projects sharing one directory.** The
other is a dormant autonomous trading signal generator last touched
2026-05-16 (`main.py`, `strategy/`, `execution/`, everything in `docs/`).
**Read those for constraints, never for scope.** Three consecutive sessions
drifted by reading them for direction.

## What this project is

Ask about a stock, get live data, discuss it. That is the whole goal.
Nothing autonomous, nothing watching, nothing alerting, no order entry.
Scheduled self-wakeups were offered on 2026-08-17 and **explicitly declined**.
Do not re-propose them.

## It already works. Do not rebuild it.

```powershell
# leave running in its own window - the zero-round-trip path
.\.venv\Scripts\python.exe -m scripts.watch SNDK

# one-shot equity snapshot
.\.venv\Scripts\python.exe -m scripts.market SNDK

# options with OPEN INTEREST (the only path that has it), stores to SQLite
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK --strikes 200 --dte 60

# multi-day context and level history
.\.venv\Scripts\python.exe -m scripts.daily SNDK --days 60 --level 2100
```

`watch.py` writes `data/live/<SYM>.json` every 5s and `<SYM>_chain.json` every
60s. Read those directly with `device_bash`. Check `updated_at_epoch` to
confirm the poller is alive before trusting anything in the file — it was left
stopped on 2026-08-17 at 13:19 MDT.

---

## THE FIRST TASK — now actually possible

2026-08-17 stored session `20260817` only, so no day-over-day diff existed
that day. On 08-18 the diff is real for the first time.

**Use `--strikes 200`. Not 40, not 100.** A contract only appears in
`oi_change()` if it exists in BOTH sessions, so anything the morning fetch
misses drops out silently. Schwab applies `strikeCount` PER EXPIRATION around
each expiration's own ATM, so `--strikes 100` produced only 1430..2280 on the
08-21 expiry while stored contracts run to **2590**. Measured union of what is
already stored, per expiration:

    2026-08-21   160 strikes   1255..2590   <- the binding constraint
    2026-08-28   160 strikes   1385..2270
    2026-09-04   160 strikes   1385..2270
    2026-09-11   100 strikes   1530..2170
    2026-09-18   100 strikes   1290..2280
    2026-09-25   100 strikes   1530..2170
    2026-10-02   100 strikes   1530..2170
    2026-10-16   100 strikes    800..2570

Run both, pre-market or early:

```powershell
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK --strikes 200 --dte 60
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SPCX --strikes 200 --dte 60
```

Then `ChainStore.oi_change()` / `.new_contracts()`.

### The specific question worth answering

SNDK 08-21 (4-day) calls at/above strike 2000. Measured 2026-08-17 evening
across both stored vintages:

    60 contracts    OI 20,749    volume >= 38,512

The OI figure is solid; it reproduced exactly. The volume is an UNDERCOUNT —
31 of those 60 contracts are frozen at a 12:24 MDT mid-day reading and were
never re-captured post-close. The widely quoted "34,249 against 20,749" from
the midday session was itself a mixed-vintage aggregate.

**If OI JUMPED**, that was opening flow and someone is positioned for the
user's upside thesis. **If OI is flat or fell**, it was churn and closing
trades. Volume alone cannot tell you — it carries no direction.

`oi_change` semantics: OI in a chain fetched on day D is the close of D-1.
`prior_session()` returns the previous STORED session, not the previous
calendar day.

---

## Findings from the 08-17 evening session — these change how the store reads

**1. `chain_snapshots` holds ONE row per contract per session.** Schema is
`UNIQUE(session_date, symbol)` with `ON CONFLICT DO UPDATE SET volume=...,
open_interest=...`. Verified: 1,960 SNDK rows, 1,960 distinct contracts, max
rows per contract = 1. "Keep stale rows working as designed" means only that
contracts ABSENT from a newer fetch survive; contracts present in it are
overwritten. `fetched_at_ms` is a per-row last-written stamp, NOT a snapshot
marker. A narrow fetch cannibalises rows from a wider earlier one — the 12:13
vintage was absorbed entirely and no longer exists.

**Consequence: any chain-wide aggregate computed FROM THE DATABASE is trap #5
rebuilt inside the store**, because it sums across vintages taken at different
strike windows. Strike 1600 read 7,423 calls over 8 expirations in the
database and 6,743 over 3 in the live fetch that wrote it. Aggregate from a
SINGLE fetch only. Per-contract joins, which is what `oi_change()` does, are
unaffected.

**2. Schwab revises the chain after the close.** Between a 16:05 MDT fetch and
an 18:15 MDT fetch, with no trading in between: CALL 1700 OI 6,231 -> 6,229;
PUT 1850 OI 556 -> 511; CALL 1800 08-21 volume 13,902 -> **13,903**; CALL 1950
08-21 volume 2,117 -> **2,112**. Volume FELL on one contract, which trading
cannot do, so these are post-close corrections and cancellations. Magnitudes
are small (0.03% to 8% on a thin line) but "OI is static intraday" does NOT
mean "immutable once published". Do not state the stronger claim.

**3. The strike window changed the top of the book, not just the ordering.**
At `--strikes 40` the largest call wall was 1600. At `--strikes 100` it is
**strike 2000 with 7,670 OI at 8/8 coverage** — a strike entirely ABSENT from
the ±40 fetch. Also: 1900 read 2,915 at ±40 and 3,318 at ±100 **with spot
identical at 1786.85 in both**. `CURRENT_SCOPE` attributes that same number
pair to spot drift over 75 minutes; the real variable is COVERAGE, and spot
drift is only one of two ways to move it.

**4. The FLOW table structurally hides the biggest positioning.** It ranks on
v/OI, which favours thin contracts. The ≥2000 08-21 block runs v/OI 1.86
against a top-8 cutoff of 5.59, so the largest absolute new positioning in the
chain never appears in the table built to show positioning. Worth an
absolute-volume companion table.

---

## What shipped 2026-08-17 evening — 2 commits, both pushed

| Commit | What |
|---|---|
| `4a33304` | Rule 22: `log_hygiene.py`, one HTTP-logger suppression list |
| `f07a5a8` | Trap #5: `StrikeCoverage` + `coverage_table`, coverage as a column |

Tests **1176 -> 1198**.

`4a33304` also spliced the Rule 30 clause 1 amendment into
`CLAUDE_PREFLIGHT.md` (690 -> 692) and declared `httpx` and `mcp` in
`requirements.txt`, both first-party imports with no declaration. The Rule 22
audit found the previous handoff's premise wrong: `fetch_option_chain.py`
already had the suppression, and `market.py` / `watch.py` / `daily.py` never
call `basicConfig` at all so their gap is LATENT. The one live gap was
`mcp_server/server.py` at INFO with no suppression while `data/alpaca_rest.py`
imports httpx directly. No credential was ever in those URLs — Alpaca and
Schwab both authenticate by header — so it was metadata exposure, not the
2026-05-04 Polygon class of leak.

`f07a5a8` replaced the split-table wall output. Every strike now appears with
a `COV` column and an `OI/EXP` column; nothing is exiled. The old design hid
the largest concentration on BOTH sides of the chain.

---

## Open items

1. **`CURRENT_SCOPE.md`'s chain-store line still says "the 'keep stale rows'
   decision working as designed".** The user deliberately deferred correcting
   it until the 08-18 fetch confirms the behaviour end to end. Confirm, then
   correct it. Finding #1 above is the correction. The trap #5 section of that
   file is already updated.
2. **MCP server buildout.** `mcp_server/server.py` has one tool, `get_health`.
   New MCP tools only appear in a NEW session. Check Settings -> Connectors,
   not config files.
3. **`market.py --chain` calls ALPACA**, which has no open interest, so that
   path structurally cannot show walls. `from_schwab()` remains unwired.
4. **Schwab token expires 2026-08-23.** Re-auth is weekly and MANUAL; refresh
   tokens hard-expire at 7 days with no programmatic renewal. `auth_state`
   reporting OK is NOT sufficient — check `has_refresh_token`, because a
   partial token once reported five days of runway on dead access for 46
   hours.
5. An absolute-volume companion to the FLOW table (finding #4).

---

## Working style — enforced repeatedly

- **ONE command per message, and the message ENDS with it.** Nothing after it.
  No follow-up questions, no "and then run". Wait for the result. (Rule 29)
- **State expected values BEFORE the command**, and COMPUTE them rather than
  estimating. A test-count prediction was off by one on 2026-08-17 because it
  was counted by eye when `pytest --collect-only` was two seconds away.
- **Quote GODZILLA (Mountain) times only**, measured in the same message that
  cites them: `TZ=America/Denver date` via `device_bash`. The Cowork container
  clock rolls to the next UTC day in the evening while Godzilla has not — that
  happened on 2026-08-17 and is exactly the documented trap.
- **Check the date/time before ANY claim about a day or time.**
- Direct and concise. No filler. No "great question". Fewer em dashes. Do not
  repeat the question back. Do not hedge with "it depends" — recommend.
- The user is **not** an options specialist. They asked what "bps" meant. Use
  analogies, explain mechanisms, do not assume jargon.
- **Rule 24:** git operations and file verification run from PowerShell on
  Godzilla, never from the Cowork mount. Edits made through the mount are
  verified from PowerShell afterwards.
- **Never handle credentials.** They live at
  `%LOCALAPPDATA%\trading\schwab.env`, outside the repo, ACL-restricted.
  Report PRESENCE, never values or lengths. The user drives the OAuth browser
  step; the redirect URL contains a single-use code that goes in the terminal,
  never in chat.
- **A Word lock file (`~$NAME.md`) means the doc is open and the real file is
  NOT writable.** A write will fail with permission denied. Ask the user to
  close it.

---

## Rules that produced actual failures — internalize, don't just read

- **Rule 14 / 28:** mark unverified claims UNVERIFIED or HYPOTHESIS, and treat
  "probably" as a trigger to investigate rather than a hedge to ship.
- **Rule 30:** an asserted fact must trace to something in the SAME message.
  Issuing a command against a file is an implicit claim the file exists.
  Clause 3 — a proxy is not the thing — was violated twice on 2026-08-17: a
  resume sentence was treated as the chain-store schema, and "OI is static
  intraday" was stated as "OI cannot change".
- **Rule 27:** commit AND push each logical unit before starting the next.
- **Rule 24:** git and file verification from PowerShell, never the mount.

---

## The seven metric traps, compressed

1. 0DTE volume/OI artifact — guarded in `fetch_option_chain`
2. IV explosion at expiry — guarded in `analysis/option_walls.py`
3. Vendor IV against a stale underlying — guarded by `check_spot_consistency`
4. Alpaca's daily bar not rolling pre-market — guarded by
   `day_bar_matches_last`
5. **Strike window changes every chain-wide aggregate.** Guarded by
   `coverage_table`, which keeps every strike and carries the basis as a
   field. The trap ALSO lives inside `chains.db`, which the guard does not
   cover — see evening findings #1 and #3.
6. **A field's NAME is not its definition.** `htbQuantity` produced a
   four-times-repeated false squeeze narrative. Only `isHardToBorrow` carries
   signal; `htbRate` is a constant 0.0 sentinel. There is NO borrow analysis
   available from this API — not a wrong one, none.
7. **A measurement compared against nothing is not evidence.** An 84-minute
   window used to answer a multi-day question; 98 IV called "extreme" without
   ever being compared to realized 104/152.

**Two habits that would have caught 5, 6 and 7 for free:** ask what a number
MEASURES before interpreting it, and name the baseline and window before
calling anything high, low, unusual, or attracting.

**And: delta is N(d1), not probability.** P(ITM) = N(d2) = N(d1 - sigma*sqrt(T)).
At 91 vol over 60 days those differ by 0.37 sigma — delta 0.289 is an **18%**
probability, not 29%. Delta was quoted as probability three times on 08-17.

---

## Where the user's thinking was on 08-17

Bullish SNDK thesis: breakout from the June 26 downtrend, reversal 2026-07-30,
running toward the prior high of $2,354, with a conservative variant of
**$2,100 within 10-15 days**. They were right about the trajectory and right
that a post-earnings digestion window is normal. The model was wrong to
dismiss the earnings from price action alone.

SNDK closed the 08-17 observation window at 1,788.40, +8.98%, on 14.1M shares.
First touch of 1800 in 24 sessions. Four-session run +41%. Catalyst was the
**Investor Day on Thursday 2026-08-13**, not the Aug 5 earnings.

Priced from the live chain, spot ~1,791, using N(d2):

```
2,100 @ 11d   finish 15%  touch 30%
2,100 @ 18d   finish 19%  touch 38%
2,354 @ 60d   finish 18%  touch 36%
```

The user is engaged, checks the work, and **caught a real methodological error
the model missed**. Treat their pushback as signal.
