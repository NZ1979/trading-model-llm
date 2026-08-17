# Session resume — 2026-08-17

Written 12:30 MDT (14:30 ET), regular hours, watcher running clean.
Ten commits this session: `6b28873..cd1b8b4`. Test suite **1173 -> 1176**.

---

## 1. What this session was for

The user's goal, unchanged and now met:

> "I want you to be able to directly access my Schwab and Alpaca api to be able
> to provide me with live stock and options data. I want to be able to ask you
> about a stock, and have you directly access live stock data."

**As of today that works with no command from the user.** Ask about a stock,
get live price and live options with open interest, from data seconds old.

Mid-session the user extended it: *"I want you to be able to watch one stock at
a time, watch it live, and respond to my questions about it without the delays
of running a prompt with each inquiry on my part."*

Three layers were offered. The user chose **poller + MCP ("Both")**, **no
autonomous reporting**, **SNDK first**. Layer 1 shipped and works. Layer 2 is
deferred: the MCP server has exactly one tool (`get_health`) and new MCP tools
only appear in a NEW session, which mid-trading-day is a bad trade.

`CURRENT_SCOPE.md`'s **"Nothing autonomous. Nothing watching."** is UNCHANGED.
The user explicitly declined the alerting layer. `watch.py` refreshes a file;
the model still speaks only when messaged.

---

## 2. What shipped

| Commit | What |
|---|---|
| `6b28873` | Wire `ChainStore` into `fetch_option_chain`; storing is now DEFAULT |
| `af5e6ea` | Record three verifications closed and traps #4/#5 |
| `9be44d0` | **`scripts/watch.py`** — live poller, the zero-round-trip path |
| `4ace1e5` | `watch.py` refreshes the option chain too |
| `63d01e8` | Walls rank only full-coverage strikes (trap #5 guard) |
| `89bf0ab` | Trap #5's real mechanism recorded |
| `a9950ad` | Parse `htbRate`; retract the borrow-scarcity reading |
| `148a61a` | Trap #6 recorded |
| `3b89346` | **`scripts/daily.py`** — multi-day context |
| `cd1b8b4` | Trap #7 recorded |

### `scripts/watch.py` (360 lines) — the headline

Polls Schwab `/quotes` + Alpaca snapshot every 5s, writes
`data/live/<SYM>.json` via temp + `os.replace` so a reader never sees a half
file. Refreshes the Schwab chain every 60s to `data/live/<SYM>_chain.json` as
a background task that cannot delay the equity poll.

- Keeps a 2h in-memory series `[epoch_ms, last, cum_volume, bid, ask]`
- Precomputes 1/5/15-minute change and shares-per-minute
- **Windows return `None` until actually covered.** A 24-second-old process
  reporting a "15-minute change" is the same error as a partial session against
  a full-day average
- `updated_at_epoch` lets a reader detect a dead poller
- Records `strike_window` in the chain file (trap #5)
- Deliberately does NOT write to `chain_store` — OI is static intraday
- Docstring states explicitly it is **not** `feed_daemon.py`

Ran **1,528 polls / 126 chain refreshes / 0 errors** across the session.

### `scripts/daily.py` (110 lines)

Daily bars with `--level`, marking sessions that touched a price and how many
sessions since. Exists because the absence of any multi-day frame caused two
errors (trap #7).

---

## 3. Verifications CLOSED today

- `VERIFIED 09:48 ET:` Schwab `/chains` reports **`delayed=False` during
  REGULAR HOURS** on both SPCX and SNDK. The Sunday-evening sample was not a
  fluke. This was the last open vendor question.
- `VERIFIED 09:48 ET:` the spot-consistency guard observed **PASSING**
  (printed 145.34 vs parity-implied 145.06, +0.19%), not only failing.
- `VERIFIED 09:48 ET:` Alpaca's daily bar **does roll** after the open;
  `day_bar_matches_last` went True and gap/change stopped being suppressed.
  The suppression is a pre-market condition, not permanent.

---

## 4. Three new metric traps (now SEVEN total)

### Trap #5 — the strike window silently changes the answer. GUARDED.

Schwab applies `strikeCount` **per expiration**, around each expiration's own
ATM. Coverage falls off with distance from spot. On SNDK **only 11 of 79
strikes** appeared in all 8 expirations at +/-40.

Consequences, all measured:
- Strike 1900 read OI **2,915** at spot 1770.86 and **3,318** at spot 1808.82
  75 minutes later — on a T+1 figure that CANNOT change intraday. It gained
  expirations as it moved toward the money.
- Put/call OI read 0.890 at 25 strikes, 0.968 at 40, 0.427 at 40 full-coverage,
  0.434 at 90, 0.498 at 160. **It is a fetch parameter, not a market fact.**
- The IV skew looked like a monotonic call ramp at 25 strikes. At 40 it was a
  smile. **At 160 the tails reverse it entirely** — see §6.
- The "largest call wall" was 1800 at +/-40 and **2000 at +/-90**.

Guard: `fetch_option_chain` ranks only full-coverage strikes, lists partial
ones separately WITH coverage counts rather than dropping them, and labels the
put/call ratio with both qualifiers. Still blunt: 1750 at 7/8 coverage with
3,256 contracts is exiled while 1740 at 8/8 with 478 ranks.

### Trap #6 — a field's NAME is not its definition. GUARDED, 3 tests.

`htbQuantity` on SNDK fell 5,268,067 -> 372,188 while the stock rallied 10%.
**Reported four times** as a draining lending pool and a developing squeeze,
escalating from HYPOTHESIS to claimed evidence on nothing but more points from
the same uninterpreted series. Never checked until the user asked.

Schwab's OpenAPI spec defines it only as *"Hard to borrow quantity."* The data
killed it: `htbRate` returns **0.0 for BOTH** SNDK (`isHardToBorrow` false) and
SPCX (`isHardToBorrow` TRUE, `htbQuantity` 11,193,376). A hard-to-borrow
security does not cost zero, so 0.0 is Schwab's no-value sentinel. Quantities
also run 27x OPPOSITE to what "shares available to short" predicts.

**Of Schwab's three borrow fields only the `isHardToBorrow` boolean carries
signal. There is NO borrow analysis available from this API.**

Sub-lesson: the prediction "SNDK's htbRate will read 0" was made, came true,
and would have been banked as validation. Only the SECOND symbol showed 0.0
means "not populated". **A confirmed prediction is not a validated hypothesis.**

### Trap #7 — a measurement compared against nothing is not evidence. GUARDED.

**Instance A (the user caught this, not the model).** An 84-minute window was
used to test whether strike 1800 was attracting SNDK. It found excursions
persisting and reported "1800 is not acting as a magnet." But SNDK had been
below 1800 for **24 sessions** and arrived that morning after a **+41%
four-session run**. Persistence is what a trend produces. The test could not
separate trend from attraction; its negative result was reported as if it could.

**Instance B.** SNDK's ~98 implied vol was called "extreme" three times.
Realized: **104 over ten sessions, 152 over twenty**, mean absolute daily move
6.6% / 8.0% against 6.2% implied. Four-day implied move 12.4% against FIVE
four-day moves above 30% in the same window. **Implied was at or BELOW
realized.** This also reversed the characterization of far-OTM call buying as
"lottery tickets".

Sub-trap: five-session realized reads **54**, which looks calm. Centred vol
measures DISPERSION, not size; five days of +2.6/+5.6/+12.8/+7.1/+9.0 have huge
drift and low variance.

Standing rule: **before reporting that anything is high, low, unusual or
attracting, name the baseline and the window, and check the window is at least
as long as the claim.**

---

## 5. Other errors this session

- **`--raw` recommended without reading it.** It dumps ONE contract and exits
  (documented on line 10). The "capture" produced 1,282 bytes of UTF-16.
- **Overstated urgency** on that capture. OI is T+1 and static intraday.
- **Diff-stat predicted 56/12, actual 50/6.** Summed old/new block sizes as if
  every line were deleted and re-inserted; git skips lines identical on both
  sides. Net (+44) was right, the split wasn't.
- **Stray `</parameter>` appended to a `git push`** — again. Commit landed, push
  failed. Closing-tag token following a long quoted command block.
- **Delta quoted as probability three times.** Delta is N(d1); P(ITM) is N(d2).
  At 91 vol / 60 days they differ by 0.37 sigma — the 2,360 call shows delta
  0.289 but **18%** true probability. A ~60% overstatement.
- **Substituted a 25-32 day expiry** for the user's 10-15 day window and quoted
  43-46% touch when the real figure was 30-38%.

---

## 6. Market record (for tomorrow's comparison)

### SNDK — closed the day's work at 1,788.40, +8.98%, volume 14.1M

```
open 1,700.74   low 1,698.00   high 1,827.99   vwap ~1,779
```
First touch of 1800 in **24 sessions**; last close above it 2026-07-10.

Four-session run **+41%** (1,271.05 -> ~1,790). Prior: top 2,354.39 on 06-22,
bottom 998.19 on 07-29 (-57.6%), reversal candle 07-30 (+25.99%).

**Catalyst was the Investor Day, Thursday 2026-08-13** (+13.67% on 22.2M
shares) — $93.9B backlog, multi-year contracts with **floor pricing
guarantees**, 80% adj. gross margin targets 2028-2030, 100% excess cash return.
JPMorgan reinitiated Overweight $2,250.

**NOT the Aug 5 earnings**, though the user correctly noted the digestion
window. The print was a large BEAT (rev $8.97B vs $8.48B street; EPS $39.25 vs
$34.96; GM 84.6% vs 79-81% guided; FY EPS $70.88 vs $2.99 prior year) but Q1
FY27 guidance came **2.5% below consensus** and the stock fell 15.1% over three
days. Aug 10-12 (+2.1/+2.7/+5.8) was the digestion; Aug 13 was the catalyst.
The Investor Day answered exactly the doubt the guide created.

Analysts, 23 covering, consensus Buy: avg $2,094, median $2,000, high $3,600,
low $1,000. Cantor $2,900, JPM $2,250, GS $2,200, Wedbush $2,000, Jefferies
$1,750, RBC Hold $1,300-1,600.

**Options, spot ~1,791, P(ITM) = N(d2) NOT delta:**

```
              finish above   touch
2,100 @ 11d       15%         30%
2,100 @ 18d       19%         38%
2,100 @ 25d       22%         43%
2,354 @ 60d       18%         36%
```

Finding worth keeping: **lowering the target from 2,354 to 2,100 bought almost
nothing because the clock shortened at the same time.** At ~95 vol, time is
worth about as much as distance.

**The tail skew REVERSES the near-money read.** At +/-160 strikes:
```
-29.9% put  IV 139.4  |  ATM 97.7  |  +28.4% call IV 111.6
```
Mild CALL skew in the body (25d RR -2.6), massive PUT skew in the tails — 28
vol points at +/-29%. Every "call skew" read before the window widened was
true only for the strikes it could see.

Flow: 4-day calls at/above 2,000 traded **34,249 contracts against 20,749 OI**.
The 1800 call did 11,824 on 2,259. Direction UNKNOWN — volume carries none.

### SPCX = SpaceX (`SPACE EX TECH SPACEX A`, Nasdaq)

Listed after the model's knowledge cutoff; nothing about the company is known
beyond the API. ~7.70B shares, ~$1.1T cap, PE -58.6, HARD TO BORROW.

Opened flat 139.97, low 139.58, ran to **149.80 and turned 20 cents short of
the 150 strike** — the largest OI in its chain (103,945 calls / 71,107 puts).
Consistent with dealers long those calls, but one rejection is not proof.
Closed the observation window at 147.63, +5.45%, volume 74.2M (70.7% of a
full-day average), back at VWAP after being 2.3% above it in the morning.

---

## 7. State at write time

- Watcher on **SNDK**, 1,528 polls, 126 chain refreshes, **0 errors**
- `data/chains/chains.db`: **SNDK 1,860 contracts / SPCX 652**, all 8
  expirations each, session `20260817`. Two fetch vintages per symbol (spots
  1.86 and 1.38 apart) — the "keep stale rows" decision working as designed
- Tree clean except **`_r30_clause1_new.txt`**, still untracked
- Tests **1176 passing**
- Schwab token expires **2026-08-23**
