# LLM Model — Session Resume Snapshot (2026-08-16)

## How to use this file

**Read this before `SESSION_RESUME_2026-08-15.md` and
`SESSION_RESUME_2026-08-14.md`. Both contain claims about Schwab that are
FALSE, and this file says which and why.** Those two have not been edited —
a session resume records what was believed at the time, and rewriting one
destroys the ability to tell when a wrong belief entered the project. They
carry pointers to this file instead.

`docs/FEED_SPEC_V4.md` remains the reference for what each vendor is good at.
Its §7 names `mcp/server.py`; that path is wrong and would break the build —
see "Corrections" below. Its §9 build order is still superseded.

---

## What happened, in one paragraph

The 08-15 session ended with a decision to abandon the daemon-first build
order and construct the small thing that was actually asked for: on-demand
stock and option data, read when the user asks. That shipped today — an
Alpaca REST client, a CLI, option analytics and an MCP server skeleton. Along
the way, live data exposed a third metric trap in the same family as the two
already documented, and the routine Schwab re-auth failed three times, which
turned out to be a two-day silent outage nobody had noticed because the
health check never opened the token file.

---

## CORRECTIONS to the prior record

### 1. Schwab access was dead from 2026-08-14 21:44, not valid until 08-21

`SESSION_RESUME_2026-08-14.md` records: *"Authenticated 2026-08-14. Token
valid 7 days from then — expires 2026-08-21."* It was valid for **one hour**.

The 08-14 OAuth flow produced a **partial token**. Observed key set on disk:

    access_token, expires_at, expires_in, scope, token_type

No `refresh_token`. No `id_token`. The access token expired on its normal
one-hour schedule at 21:44 on 08-14 and there was nothing to renew it with.
Every Schwab API call from that moment on would have failed.

A complete token, for comparison, written 2026-08-16:

    access_token, expires_at, expires_in, id_token, refresh_token, scope,
    token_type

The broken file is preserved at
`%LOCALAPPDATA%\trading\schwab_tokens.broken-20260814.json`.

### 2. Schwab options data is NOT delayed

`SESSION_RESUME_2026-08-14.md` records `isDelayed: true` and lists "resolve
whether the delayed-data entitlement can be changed" as open work. The 08-15
session then spent a four-track research pass on OPRA licensing, `entitlement`
parameters and `PP-PayingPro`/`NP-NonPro` client classifications, and recorded
conclusions about account-level entitlement.

With a complete token, `/chains` returns **`delayed=False`** — real-time, with
open interest, volume, implied volatility and delta together.

`HYPOTHESIS:` the partial grant returned degraded data. One observation
before, one after. But the entitlement research was probably explaining an
artifact of a broken token, and **should not be treated as a finding**.

Practical consequence: the split designed on 08-15 — Alpaca for IV and greeks,
Schwab for open interest — was premised on Schwab being delayed. Schwab alone
covers both. Alpaca remains worth having for a real-time SIP underlying and as
a second vendor to cross-check against, which proved its value the same day
(see the spot guard below), but it is no longer load-bearing.

### 3. `FEED_SPEC_V4` §7's `mcp/server.py` path would break the build

A local package named `mcp` shadows the installed MCP SDK completely.
Verified 2026-08-16: with `mcp/__init__.py` present, `import mcp` resolves to
the local directory and `from mcp.server import MCPServer` fails from inside
the server itself. Every other package here has an `__init__.py`, so
convention would add one. The package is `mcp_server/`, and
`tests/test_mcp_server.py` fails loudly if anyone renames it back.

### 4. `mcp` 2.0.0 removed `FastMCP`

Any example using `from mcp.server import FastMCP` raises ImportError. The
class is `MCPServer`, same decorator and run signatures.

---

## The third metric trap — spot inconsistency

Joins the 0DTE volume/OI artifact and the IV-explosion-at-expiry trap, and is
the most expensive of the three.

**Vendors compute implied volatility and greeks against the underlying's
current last price, not against the underlying that prevailed when the option
was quoted.** Those agree during regular hours and diverge outside them: US
options stop quoting at 16:00 ET while equities print until 20:00.

Measured on SNDK, 2026-08-16:

    equity last (Alpaca)      1657.00   age 46.9h
    parity-implied spot       1642.61   522 pairs, stdev 2.51
    Schwab underlying         1641.11   independent confirmation
    divergence                  14.39   0.88%

Resulting call-vs-put IV gap at the SAME strike and expiration:

    5 DTE   20-22 vol points      26 DTE   7-8
    12 DTE  12-13                 40 DTE     6
    19 DTE   9-10

The decay is the fingerprint: a fixed dollar error divided by vega. Short
expiries have little vega so the error becomes enormous; long expiries absorb
it. Genuine put demand does not decay as 1/sqrt(T).

A 25-delta risk reversal computed from that data reads as extreme put skew —
panic pricing — when the true number is near zero. It fires every day between
16:00 and 20:00 ET and all weekend.

**The guard** recovers the spot the options were actually quoted against, via
put-call parity on deep-ITM pairs (nearly vol-independent, so a wrong vol
assumption cannot bias it):

    C - P = S - K*exp(-rT)   =>   S ~= C - P + K

`analysis/option_walls.check_spot_consistency()`. When it fails,
`risk_reversal()` and `iv_skew_curve()` return **nothing** rather than a
flagged number — a warning beside a plausible figure gets read past.

Two things learned building it, both worth keeping:

- **The guard validates, it does not repair.** Recovering the correct spot
  does not correct the vendor's IV, which was already computed against the
  wrong one. Feeding the recovered spot back in produced a confident
  +10.6 vol-point reading built entirely on rejected IVs. The functions take
  a `SpotCheck` rather than a bare float so this cannot happen by accident.
- **Parity needs strikes below 70% of spot.** `scripts/fetch_option_chain.py`
  defaults to `strike_count=25`, a narrow window around ATM containing none,
  so the guard returns UNKNOWN on every Schwab chain fetched that way. The
  message names it as a fetch problem.

---

## The `auth_state` bug — how a one-hour outage became a two-day one

`auth_state` computed token age from the token file's **mtime** and never
opened the file. The docstring stated the rationale: *"mtime advances on every
refresh write, which is exactly the quantity the 7-day rule is measured
against."* That is backwards twice over.

- schwab-py rewrites the token file on every **access**-token refresh, every
  ~30 minutes of normal use. So mtime tracks the last refresh write, not the
  refresh token's issue date. A token in active use looks perpetually young
  while its 7-day clock runs out underneath.
- It never checked whether a `refresh_token` existed at all.

Result: `OK — 5.11 days remaining` reported for 46 hours after every API call
had begun failing, for a file containing no refresh token.

Fixed in `c446793`:

- `read_token_file()` opens the file. Returns structure and timestamps, never
  token values.
- Age from `creation_timestamp`, falling back to mtime only when absent and
  labelling which was used in `token_age_source`.
- New states `TOKEN_INCOMPLETE` (no `refresh_token` — checked **before** age,
  since such a token is unusable at any age) and `TOKEN_UNREADABLE`.
- `health()` exposes `has_refresh_token`, `token_age_source`,
  `access_token_expired`, and `checked_live: false` — stating outright that it
  inspected a file and proved nothing about access.
- `verify_live()` makes one real call. Opt-in via `health(live=True)`.

Ten existing tests failed against the fix. Every one wrote `"{}"` as the token
file and asserted it was healthy — they encoded the fiction. Fixtures now
write realistic tokens; eleven regression tests added, including the exact
08-14 key set.

**All three callers gate on `("OK", "WARN_EXPIRING")`, so the new states fail
closed with no changes needed.**

---

## What shipped

| SHA | Deliverable |
|---|---|
| `5535d83` | `mcp_server/` skeleton — `get_health` over stdio, 12 tests |
| `3ed87b0` | `data/alpaca_rest.py` — equity + real-time OPRA options, 32 tests |
| `695788e` | `scripts/market.py` — on-demand CLI |
| `b08303f` | `analysis/option_walls.py` — walls, flow, skew + spot guard, 32 tests |
| `c446793` | `auth_state` fix, 11 regression tests |
| `1711b3b` | Ignore `data/snapshots/` |

Test suite **1056 → 1143**, green on Python 3.14.

### The loop that now works

    .\.venv\Scripts\python.exe -m scripts.market SNDK --chain --dte 45

Prints a readable snapshot and writes `data/snapshots/<SYMBOL>.json`, which
Claude reads directly off disk through the Cowork device bridge. **No MCP
server, no registration, no copy/paste.** This is the delivery mechanism that
works today; the MCP server is the smoother version of the same thing.

### Alpaca — already paid for, never called

Algo Trader Plus includes **real-time OPRA options data**: quotes, trades,
greeks, implied vol, via `/v1beta1/options/snapshots/{underlying}`. Confirmed
live. Two gotchas: the options websocket is **msgpack only**, and there is
**no `*` wildcard** subscription for option quotes.

`data/alpaca_market_data.py` remains websocket-only; `data/alpaca_rest.py` is
the new REST path. Every price on it carries an age and an `is_stale` flag —
a last price without an age is the most misleading field the module could
return, and matters more on-demand than on a live tape.

**Alpaca has no open interest** anywhere in its market data API. `OptionQuote`
omits the attribute entirely rather than carrying None, so reaching for it
raises AttributeError at the call site.

---

## Open UNVERIFIED items

- Whether the partial-token hypothesis explains `isDelayed`. One observation
  each side.
- Why the 08-14 flow produced a partial token at all.
- Whether the MCP server registers with this Claude Desktop build.
  `%APPDATA%\Claude` was created by us and is not the app's data directory;
  the app may use a Connectors UI rather than `claude_desktop_config.json`.
  **Convenience only — the CLI loop works without it.**
- `requirements.txt` declares neither `mcp` nor `httpx`. Both are imported by
  first-party code; `httpx` has been undeclared since `polygon_feed.py`.
- Rule 22 logging audit for `httpx2`, which arrived as an `mcp` dependency.
- Whether `config/pm_rvol_thresholds.json` exists. If absent, every lookup
  falls through to `HARD_FALLBACK_THRESHOLD = 5.0`.
- The RVOL reconciliation (Alpaca SIP vs Polygon pre-market volume). **Not
  urgent** — nothing is running anywhere, see below.
- Schwab `/chains` rate limits — still unmeasured.

---

## Production state

**Nothing is trading anywhere.** Verified 2026-08-16:

- `main.py` is not running on Godzilla.
- Hetzner VPS `5.161.199.155` is up (112 days) but `trader.service` is
  `inactive` **and** `disabled` — it will not restart on boot. Load 0.00.
- The only live process is `C:\trading\LLM_SWING_MODEL` running
  `research.daily_loop watch`, up since 2026-07-15. Separate codebase,
  separate venv, unaffected by this repo. **Rule 26's partition does not
  mention it.**

The VPS is **not decommissioned**, so Rule 26's prohibitions stand in full.
It also still costs $8/mo to idle.

`PROJECT_BLUEPRINT.md`'s SSH line is stale: `~\.ssh\hetzner_trader` does not
exist. SSH authenticates with a different key; the documented command works by
accident.

### Clock

Root-caused and fixed 08-15 (W32Time was `Stopped`/`Manual`, ~41 ppm
free-run). 24-hour hold check on 08-16: **86 ms, drifting at 0.76 ppm** — a
54x suppression, against 3.6 s if the service had stopped again. Confirmed
against two independent references.

**Do not spend more time on this.** The 500 ms budget came from
`FEED_SPEC_V4` §5.1's quote-age tagging, which belongs to the deferred
microstructure layer. Nothing in on-demand analysis breaks at 86 ms, or at
2 seconds. It was over-investigated on 08-16 and should be considered closed.

---

## What's next

1. **`requirements.txt`** — declare `mcp` and `httpx`, plus the Rule 22 audit
   for `httpx2`.
2. **Rule 30** — asserted facts, not hypotheses. Rule 28 fires on hedged
   language and cannot catch a confident statement about something never
   checked. Three wrong time claims, two "I wrote the file" claims for files
   that did not exist, and one "server-side at Schwab" from a file that had
   not been opened — all that class, all in two sessions.
3. **`analysis/option_walls` against Schwab chains** — now that the data is
   real-time and carries OI, wire `from_schwab()` into a real path and fetch
   wide enough for the parity guard to work.
4. **MCP registration**, if the convenience is wanted. Check Settings →
   Connectors before touching config files again.

---

## First thing to check next session

1. Anchors (Rule 23/25). **Godzilla is on Mountain time.** Measure elapsed
   time, never extrapolate it.
2. `python -m scripts.schwab_login --status` — should now report
   `has_refresh_token: True`. Re-authed 2026-08-16 18:36 MT, so it expires
   2026-08-23. **`OK` alone is no longer sufficient; check the new field.**
3. `git status` — expect clean at `1711b3b` or later.
4. Clock only if something timestamp-dependent is being built. Otherwise
   leave it alone.
