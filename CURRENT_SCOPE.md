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

Split by QUESTION, not by vendor. Both equity sources are used, because each
carries what the other cannot.

| Need | Source | Why |
|---|---|---|
| Equity tape and bars | **Alpaca SIP** | Schwab has no time-and-sales endpoint and its level one is conflated (`FEED_SPEC_V4` §0). Only Alpaca gives per-print size, exchange and condition codes, and bars at any timeframe. |
| Equity snapshot — price, NBBO, TODAY's volume, change, baselines, fundamentals, borrow | **Schwab `/quotes`** | `VERIFIED 2026-08-17 06:51 ET`: `quoteType: NBBO`, `realtime: true`, timestamps 48s old. Carries `totalVolume` (today's, live), `avg10DaysVolume`, and `netPercentChange` against a real `closePrice`. Alpaca's `dailyBar` does not roll at the pre-market open, so its volume is the PREVIOUS session's and its change figures are suppressed until it does. |
| Option open interest | **Schwab `/chains`** | Alpaca has NO open-interest field anywhere in its market data API. Its OI lives on the trading API at T+2 with no history. |
| Option IV, greeks, quotes | **Schwab `/chains`**, Alpaca as cross-check | Schwab returns real-time with OI, IV and delta in one call. Alpaca's OPRA feed is also real-time with greeks and is a useful second opinion — it caught a stale-spot problem on 2026-08-16 that a single vendor would have hidden. |

**Neither vendor alone answers "what is this stock doing".** Schwab has no
tape; Alpaca has no live daily volume before its bar rolls. `scripts/market.py`
fetches both.

**`totalVolume / avg10DaysVolume` is NOT RVOL.** It compares a partial session
against a full-day average. Real RVOL needs a time-of-day baseline, which is
what `scripts/build_pm_rvol_thresholds.py` builds from Polygon and which
Schwab cannot supply. The field is named `volume_vs_avg_full_day` for that
reason.

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
`VERIFIED 2026-08-17:` Schwab `/quotes` returns real-time NBBO equity data.
`VERIFIED 2026-08-17 09:48 ET:` Schwab `/chains` still reports
`delayed=False` during REGULAR HOURS, on SPCX and SNDK. The Sunday-evening
sample was not a fluke. This question is closed.
`VERIFIED 2026-08-17 09:48 ET:` the spot-consistency guard has now been
observed PASSING (printed 145.34 vs parity-implied 145.06, +0.19%), not
only failing. Both arms of the guard are exercised against live data.
`VERIFIED 2026-08-17 09:48 ET:` Alpaca's daily bar DOES roll after the
open — `day_bar_matches_last` went True, and gap/change stopped being
suppressed. The suppression is a pre-market condition, not a permanent one.

## Sources evaluated and REJECTED

Recorded so they are not re-investigated. Both were chased on 2026-08-17 and
both are closed.

**OCC daily open interest** — `marketdata.theocc.com/daily-open-interest`.
The theory was that OCC is the clearing house, so its file is the ORIGIN of
every vendor's open interest rather than a redistribution, and would verify
Schwab's per-contract OI for free and permanently.

`VERIFIED 2026-08-17:` it responds unauthenticated and returns a CSV, but the
file is **market-wide aggregate only** — total open interest bucketed into
Equity / Index / Debt / Futures, one row per date. No symbol, strike or
expiration column anywhere. 1,449 bytes for a whole month. **It cannot verify
per-contract open interest.** Closed.

Not useless, just not for that. It is a free daily source for **market-wide
options breadth**, which nothing else in this stack covers — on 2026-08-14
total OI fell 14.7M contracts to 670,687,510, with equity calls down 8.8M.
Noted as available; nothing is built on it.

Consequence: Schwab's per-contract OI stays unverified against an independent
source, and that is accepted. Schwab's OI originates from the same clearing
house, so the risk was never a wrong number — it is a misread of the T+1
semantics, which `data/chain_store.py` already encodes in `as_of_close` and
`prior_close`.

**Barchart** — `VERIFIED 2026-08-17:` their Terms of Service prohibit *"any
data mining, robots, or similar data gathering and extraction tools to
capture data or content from the Barchart Services"*, so scraping is out.
Their OnDemand `getEquityOptions` API does return open interest, implied
volatility, all four greeks and bid/ask with sizes — but it is contact-sales
only, with no self-serve signup, no published pricing, no free tier, and
documentation describing *"intraday or end-of-day options data"* without ever
claiming real-time. Closed.

**The general principle, which is worth more than either finding.** Barchart,
Schwab and Alpaca all source options data from OPRA. A third vendor reading
the same tape checks that vendor's PROCESSING, not the tape — three vendors
agreeing tells you they agree, not that they are right.

Prefer **structural** checks over second opinions. Put-call parity is
arithmetic the data must satisfy regardless of who publishes it, costs
nothing, needs no vendor, and caught a 4.97% spot error on 2026-08-17 that
three agreeing vendors would have sailed past. See
`analysis/option_walls.check_spot_consistency`.

## How it works today

    .\.venv\Scripts\python.exe -m scripts.market SNDK
    .\.venv\Scripts\python.exe -m scripts.market SNDK --chain --dte 45
    .\.venv\Scripts\python.exe -m scripts.fetch_option_chain --symbol SNDK

`scripts/market.py` prints a readable snapshot and writes
`data/snapshots/<SYMBOL>.json`, which Claude reads directly off disk through
the Cowork device bridge. **One command, then ask the question.** No MCP
server, no registration, no copy/paste.

`scripts/fetch_option_chain.py` is the OPTIONS half and the only path that
carries open interest. Since 6b28873 it stores every fetched chain to
`data/chains/chains.db` by default (`--no-store` opts out), because open
interest is a T+1 figure: stable intraday, overwritten overnight, and an
uncaptured session is gone permanently. Day-over-day OI change is the only
measurement that separates opening from closing flow — volume cannot,
because volume carries no direction. First captures: SPCX and SNDK, both
2026-08-17, 640 contracts each.

**`scripts/market.py --chain` still calls ALPACA**, which has no open
interest, so it cannot show walls. `from_schwab()` remains unwired. Use
`fetch_option_chain` for anything involving positioning.

`mcp_server/server.py` is a skeleton of the smoother version — it would let
Claude make the call itself. It is not registered and is **convenience only**.
Do not treat registering it as a prerequisite for anything.

## The modules that belong to THIS project

    data/alpaca_rest.py        equity tape, bars, OPRA option chains
    data/schwab_quotes.py      real-time NBBO equity snapshots
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
- **Godzilla is on Mountain time. Market hours are ET.** Measure, never
  extrapolate — and measure THIS, in the message where the time is cited:

      TZ=America/New_York date      # via device_bash

  `VERIFIED 2026-08-17:` the Cowork `device_bash` VM runs on Godzilla and its
  clock tracks Godzilla's — `device_bash` read 07:31:30 ET against a Windows
  `Get-Date` of 07:32:55 ET, the 85-second gap being the round trip. So a
  session can self-serve Godzilla time rather than asking for it, and must
  not substitute its own container clock: that one was **two hours wrong** at
  the start of the 2026-08-15 session and produced three incorrect time
  claims before anyone noticed. See Rule 30 clause 2.

  Godzilla's clock itself is NTP-disciplined and trustworthy — W32Time was
  found `Stopped`/`Manual` on 2026-08-15 with the machine free-running at
  ~41 ppm, and after the fix it held to 86 ms over 24 hours against two
  independent references. Do not re-investigate it; the 500 ms budget that
  made it urgent belongs to the deferred microstructure layer, and nothing
  in on-demand analysis breaks at 86 ms.
- **The metric traps are real and there are FIVE.** Each produced
  confident, plausible, wrong output before being caught.

  1. The 0DTE volume/OI artifact. Guarded in `fetch_option_chain`.
  2. IV explosion at expiry. Guarded in `analysis/option_walls.py`.
  3. Vendor IV computed against a stale underlying. Guarded by
     `check_spot_consistency`.
  4. Alpaca's daily bar not rolling at the pre-market open, making gap and
     change span two sessions. Guarded by `day_bar_matches_last`.
  5. **NEW 2026-08-17, UNGUARDED: the strike window silently changes the
     answer.** `--strikes` truncates the chain symmetrically around ATM, so
     any statistic aggregated across the whole chain is a function of the
     fetch parameter rather than the market. Two live instances on SPCX,
     twenty minutes apart:

     - put/call OI ratio read **0.890** at `--strikes 25` and **0.968** at
       `--strikes 40`. The wider fetch pulled in put OI at 130, 85 and 70
       that the narrow one never saw. The number was quoted as evidence of
       call-heavy positioning. It was evidence of a fetch parameter.
     - the IV skew looked like a MONOTONIC call ramp at 25 strikes, because
       the put wing was truncated at 135. At 40 strikes it is a SMILE with
       both wings bid, minimum near 143. Call skew is real but roughly 6 vol
       points, not the one-way slope first described.

     `tests/test_option_walls.py:155` already names `strike_count=25` as too
     narrow for the parity check. Same defect, two more surfaces. Until it is
     guarded: never quote a chain-wide aggregate without the strike window
     attached, and prefer `--strikes 40` or wider for anything but a glance.

## Where the truth lives

1. **This file** — what the project is.
2. **`SESSION_RESUME_2026-08-16.md`** — the corrected record and open items.
3. **`CLAUDE_PREFLIGHT.md`** — the rules, through Rule 30.
4. Everything else — constraints only.
