# CURRENT SCOPE — read this before anything else in this repository

Last updated 2026-08-17.

## What this project is

In the user's own words, 2026-08-17:

> "I want you to be able to directly access my Schwab and Alpaca api to be
> able to provide me with live stock and options data. I want to be able to
> ask you about a stock, and have you directly access live stock data from my
> Schwab and Alpaca accounts."

That is the whole goal. Ask about a stock, get live data, discuss it.

**Nothing autonomous. Nothing watching. Nothing alerting. No order entry, ever.**
The model is turn-based and only executes when a message is sent. It cannot
monitor a condition, and any design that assumes it can is wrong.

## What this project is NOT

`C:\trading\LLM model` contains **two projects sharing one directory.**

The other one is an **autonomous LLM trading signal generator** — `main.py`,
`strategy/`, `execution/`, the gap-and-go replacement, tier-1/2/3 model
escalation, the pre-market RVOL context. It was last worked on **2026-05-16**
and has been dormant since. It is not running anywhere: `main.py` is not
running on Godzilla, and `trader.service` on the VPS at `5.161.199.155` is
`inactive` AND `disabled` (verified 2026-08-16).

**Every document in `docs/` describes that dormant project**, not this one:

    docs/LLM_MODEL_CHARTER.md        2026-05-11
    docs/LLM_MODEL_OVERVIEW.md       2026-05-13
    docs/LLM_SIGNAL_INTERFACE.md     2026-05-13
    docs/NARRATIVE_OVERVIEW.md       2026-05-12
    PROJECT_BLUEPRINT.md             mostly 2026-04/05
    docs/FEED_SPEC_V4.md             2026-08-14, build order superseded

**READ THOSE FOR CONSTRAINTS, NEVER FOR SCOPE.** They hold hard-won findings
that still bind — vendor limits, the metric traps, credential handling,
one-connection-per-account, condition codes. They also describe goals that are
not this project's, and reading them for direction is how three consecutive
sessions drifted:

- 2026-08-14/15 built a websocket daemon and tick corpus first, putting the
  only component the user interacts with four phases away.
- 2026-08-17 asserted "the metric your entire platform gates on" about RVOL,
  from a phrase in a May document, about a strategy that was shut down.

If a document tells you what to build, it is out of date. This file tells you
what to build.

## Which data source for what

Decided on evidence, 2026-08-16/17.

| Need | Source | Why |
|---|---|---|
| Equity price, quotes, bars, tape | **Alpaca SIP** | Schwab has no time-and-sales endpoint at all and its level one is officially conflated (`FEED_SPEC_V4` §0). Alpaca gives the full consolidated tape with per-print size, exchange and condition codes. |
| Option open interest | **Schwab `/chains`** | Alpaca has NO open-interest field anywhere in its market data API. Its OI lives on the trading API at T+2 with no history. |
| Option IV, greeks, quotes | **Schwab `/chains`**, Alpaca as cross-check | Schwab returns real-time with OI, IV and delta in one call. Alpaca's OPRA feed is also real-time with greeks and is a useful second opinion — it caught a stale-spot problem on 2026-08-16 that a single vendor would have hidden. |

**Use the Schwab DEVELOPER API, not thinkorswim.** thinkorswim's RTD COM
server is real and Schwab-documented, but it only works while the desktop app
is running and logged in, is Windows-COM-bound, and will not load a chain —
every contract must be enumerated and requested individually. It provides
nothing the developer API does not. Fallback only.

Both subscriptions are already paid for:

- **Alpaca Algo Trader Plus**, ~$99/mo — full SIP equities AND real-time OPRA
  options. The options half went uncalled from subscription start until
  2026-08-16.
- **Schwab Trader API**, free, app `trading-feed-daemon`, Order Limit 0.

`VERIFIED 2026-08-16:` Schwab `/chains` returns `delayed=False`.
`UNVERIFIED:` Schwab equity quotes (`/quotes` never called), and whether
Schwab options stay real-time during regular hours — the only sample is a
Sunday evening.

## How it works today

    .\.venv\Scripts\python.exe -m scripts.market SNDK
    .\.venv\Scripts\python.exe -m scripts.market SNDK --chain --dte 45
    .\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK

`scripts/market.py` prints a readable snapshot and writes
`data/snapshots/<SYMBOL>.json`, which Claude reads directly off disk through
the Cowork device bridge. **One command, then ask the question.** No MCP
server, no registration, no copy/paste.

`mcp_server/server.py` is a skeleton of the smoother version — it would let
Claude make the call itself. It is not registered and is **convenience only**.
Do not treat registering it as a prerequisite for anything.

## The modules that belong to THIS project

    data/alpaca_rest.py        equity snapshots, bars, OPRA option chains
    data/schwab_auth.py        7-day token state machine
    data/schwab_chains.py      /chains fetch and parse
    data/chain_store.py        rolling SQLite for day-over-day OI change
    analysis/option_walls.py   walls, flow, skew + the spot-consistency guard
    scripts/market.py          the CLI above
    scripts/schwab_login.py    weekly re-auth
    mcp_server/                MCP skeleton, unregistered

Dormant, do not extend: `data/feed_daemon.py`, `data/tick_store.py`,
`main.py`, `strategy/`, `execution/`, and phases 3 and 5-8 of `FEED_SPEC_V4`.

## Operational facts that bite

- **Schwab re-auth is weekly and manual.** Refresh tokens hard-expire at 7
  days with no programmatic renewal. Re-authed 2026-08-16, expires
  **2026-08-23**. `auth_state` reporting `OK` is not sufficient on its own —
  check `has_refresh_token`, because a partial token once reported five days
  of runway on dead access for 46 hours.
- **Godzilla is on Mountain time.** Market hours are ET. Measure, never
  extrapolate.
- **The metric traps are real and there are three.** The 0DTE volume/OI
  artifact, IV explosion at expiry, and vendor IV computed against a stale
  underlying. All three produced confident, plausible, wrong output before
  being caught. `analysis/option_walls.py` guards all three.

## Where the truth lives

1. **This file** — what the project is.
2. **`SESSION_RESUME_2026-08-16.md`** — the corrected record and open items.
3. **`CLAUDE_PREFLIGHT.md`** — the rules, through Rule 30.
4. Everything else — constraints only.
