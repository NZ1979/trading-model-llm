# Handoff — open this in a new session

Paste this whole file as your first message. Written 2026-08-17 12:30 MDT.

---

## Read these first, in this order, before doing anything

1. **`CURRENT_SCOPE.md`** (361 lines) — what this project IS. Read all of it.
   Section "What this project is NOT" matters as much as the rest.
2. **`SESSION_RESUME_2026-08-17.md`** (250 lines) — what happened yesterday,
   including six errors and three retractions worth not repeating.
3. **`CLAUDE_PREFLIGHT.md`** (690 lines) — the rules, through Rule 30.

**`C:\trading\LLM model` contains TWO projects sharing one directory.** The
other is a dormant autonomous trading signal generator last touched 2026-05-16
(`main.py`, `strategy/`, `execution/`, everything in `docs/`). Those documents
describe goals that are NOT this project's. **Read them for constraints, never
for scope.** Three consecutive sessions drifted by reading them for direction.

---

## What this project is

Ask about a stock, get live data, discuss it. That is the whole goal.

**Nothing autonomous. Nothing watching. Nothing alerting. No order entry.**
The user was offered scheduled self-wakeups on 2026-08-17 and **explicitly
declined**. Do not re-propose it.

---

## It already works. Do not rebuild it.

```powershell
# leave running in its own window - this is the zero-round-trip path
.\.venv\Scripts\python.exe -m scripts.watch SNDK

# one-shot equity snapshot
.\.venv\Scripts\python.exe -m scripts.market SNDK

# options with OPEN INTEREST (the only path that has it), stores to SQLite
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK --strikes 40 --dte 60

# multi-day context and level history
.\.venv\Scripts\python.exe -m scripts.daily SNDK --days 60 --level 2100
```

`watch.py` writes `data/live/<SYM>.json` every 5s and `<SYM>_chain.json` every
60s. **Read those directly with `device_bash`** — no command from the user, no
copy/paste. Check `updated_at_epoch` to confirm the poller is alive before
trusting anything in the file.

---

## THE FIRST TASK, and it is time-sensitive

`data/chains/chains.db` holds session `20260817` only: **SNDK 1,860 contracts,
SPCX 652**, all 8 expirations each. Today is the first day a **day-over-day
open-interest change** is possible. That measurement is the only thing that
separates opening flow from closing flow — volume cannot, because volume
carries no direction.

Run both, pre-market or early:

```powershell
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK --strikes 40 --dte 60
.\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SPCX --strikes 40 --dte 60
```

Then use `ChainStore.oi_change()` / `.new_contracts()`. **The specific question
worth answering:** SNDK 4-day calls at/above 2,000 traded **34,249 contracts
against 20,749 OI** on 08-17, and the 1800 call did 11,824 on 2,259. If OI
JUMPED, that was opening flow and someone is positioned for the user's upside
thesis. If OI is flat or fell, it was churn and closing trades.

Note `oi_change` semantics: OI in a chain fetched on day D is the close of
D-1, which is why `OIChange` carries `as_of_close` and `prior_close` separately
from fetch dates. `prior_session()` returns the previous STORED session, not
the previous calendar day.

---

## Open items, none urgent

1. **MCP server buildout.** `mcp_server/server.py` has exactly ONE tool,
   `get_health`. Registering it as-is buys nothing. New MCP tools only appear
   in a NEW session. The user chose "Both" (poller + MCP) — the poller shipped,
   this is the remainder. Check Settings -> Connectors, not config files.
2. **Trap #5 guard is blunt.** `fetch_option_chain` exiles strike 1750 at 7/8
   coverage with 3,256 contracts while ranking 1740 at 8/8 with 478. A single
   table with coverage as a COLUMN would be better than the current split.
3. **`_r30_clause1_new.txt`** — untracked in the repo root for two sessions.
   The Rule 30 clause 1 line-splice was never run. Either splice it (expected
   690 -> 692 lines) or delete the file.
4. **`requirements.txt` declares neither `mcp` nor `httpx`.** Rule 22 logging
   audit for `httpx` still owed.
5. **`market.py --chain` calls ALPACA**, which has no open interest, so that
   path structurally cannot show walls. `from_schwab()` remains unwired.
6. **Schwab token expires 2026-08-23.** Re-auth is weekly and MANUAL; refresh
   tokens hard-expire at 7 days with no programmatic renewal. `auth_state`
   reporting OK is NOT sufficient — check `has_refresh_token`, because a
   partial token once reported five days of runway on dead access for 46 hours.

---

## Working style — the user has enforced these repeatedly

- **ONE command per message, and the message ENDS with it.** Nothing after it.
  No follow-up questions, no "and then run". Wait for the result. This was
  escalated to a permanent rule (Rule 29) after repeated violations.
- **State expected values BEFORE the command** so a mismatch is visible rather
  than rationalized after (Rule 30).
- **Quote GODZILLA (Mountain) times only.** Never ET, never the container
  clock — it was two hours wrong once and produced three bad claims. Measure it
  in the same message you cite it: `TZ=America/Denver date` via `device_bash`,
  which tracks Godzilla's clock.
- **Check the date/time before ANY claim about a day or time.**
- Direct and concise. No filler. No "great question". Fewer em dashes. Do not
  repeat the question back. Do not hedge with "it depends" — recommend.
- The user is **not** an options specialist. They asked what "bps" meant. Use
  analogies; explain mechanisms; do not assume jargon.

---

## Rules that produced actual failures — internalize, don't just read

- **Rule 14 / 28:** mark unverified claims UNVERIFIED or HYPOTHESIS, and treat
  "probably" as a trigger to investigate rather than a hedge to ship.
- **Rule 30:** an asserted fact must trace to something in the SAME message.
  Issuing a command against a file is an implicit claim the file exists.
- **Rule 27:** commit AND push each logical unit before starting the next.
- **Rule 24:** git operations and file verification run from PowerShell on
  Godzilla, never from the Cowork bash mount.
- **Never handle credentials.** They live at `%LOCALAPPDATA%\trading\schwab.env`,
  outside the repo, ACL-restricted. Report PRESENCE, never values or lengths.
  The user drives the OAuth browser step; the redirect URL contains a
  single-use code that goes in the terminal, never in chat.

---

## The seven metric traps, compressed

1. 0DTE volume/OI artifact — guarded in `fetch_option_chain`
2. IV explosion at expiry — guarded in `analysis/option_walls.py`
3. Vendor IV against a stale underlying — guarded by `check_spot_consistency`
4. Alpaca's daily bar not rolling pre-market — guarded by
   `day_bar_matches_last`
5. **Strike window changes every chain-wide aggregate.** Schwab applies
   `strikeCount` PER EXPIRATION. Only 11 of 79 SNDK strikes had full coverage
   at +/-40. Partially guarded
6. **A field's NAME is not its definition.** `htbQuantity` produced a
   four-times-repeated false squeeze narrative. Guarded, 3 tests
7. **A measurement compared against nothing is not evidence.** An 84-minute
   window used to answer a multi-day question; 98 IV called "extreme" without
   ever being compared to realized 104/152. Guarded by `scripts/daily.py`

**Two habits that would have caught 5, 6 and 7 for free:** ask what a number
MEASURES before interpreting it, and name the baseline and window before
calling anything high, low, unusual, or attracting.

**And: delta is N(d1), not probability.** P(ITM) = N(d2) = N(d1 - sigma*sqrt(T)).
At 91 vol over 60 days those differ by 0.37 sigma — delta 0.289 is an **18%**
probability, not 29%. Delta was quoted as probability three times on 08-17.

---

## Where the user's thinking was on 08-17

They hold a bullish SNDK thesis: breakout from the June 26 downtrend, reversal
2026-07-30, running toward the prior high of $2,354, with a conservative
variant of **$2,100 within 10-15 days**. They were right about the trajectory
and right that a post-earnings digestion window is normal. The model was wrong
to dismiss the earnings from price action alone.

Priced from the live chain, spot ~1,791, using N(d2):

```
2,100 @ 11d   finish 15%  touch 30%
2,100 @ 18d   finish 19%  touch 38%
2,354 @ 60d   finish 18%  touch 36%
```

The user is engaged, checks the work, and **caught a real methodological error
the model missed**. Treat their pushback as signal.
