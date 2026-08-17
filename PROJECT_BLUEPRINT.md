> **CORRECTED 2026-08-16.** Read `SESSION_RESUME_2026-08-16.md` alongside this
> file. Four corrections affecting §2 and §3:
>
> - **Schwab token expiry.** §2's "Token valid 7 days" premise is wrong. The
>   2026-08-14 flow wrote a partial token with no `refresh_token` and access
>   died the same evening at 21:44. Re-authed 2026-08-16; expires 2026-08-23.
>   `auth_state` was fixed in `c446793` — it now reads the file rather than
>   its mtime, and `OK` alone is no longer sufficient. Check
>   `has_refresh_token`.
> - **Schwab options data is real-time**, not delayed. `/chains` returns
>   `delayed=False` with open interest, volume, IV and delta together.
> - **§3's SSH line is stale.** `~\.ssh\hetzner_trader` does not exist. SSH
>   authenticates with a different key, so the documented command works by
>   accident. Correct the path before anyone follows it.
> - **Nothing is trading anywhere as of 2026-08-16.** `main.py` is not running
>   on Godzilla. `trader.service` on the VPS at `5.161.199.155` is `inactive`
>   AND `disabled`, so it will not restart on boot; the box is up with 112
>   days uptime and load 0.00, still costing ~$8/mo. It is therefore NOT
>   decommissioned, and Rule 26's prohibitions stand in full. The only live
>   process on Godzilla is `C:\trading\LLM_SWING_MODEL` running
>   `research.daily_loop watch`, up since 2026-07-15 — a separate codebase
>   that Rule 26's partition does not mention.


# Trading Platform — Project Blueprint

**Read this file first.** Everything Claude needs to pick up this project is here. Do not refer to past conversations — they don't exist in this Cowork session.

---

## 1. What this is

An intraday equity trading platform that combines news sentiment (Claude Haiku), technical indicators, and a planned futures-walls or options-walls confirmation layer. Paper trading on Alpaca. Deployed to a Hetzner VPS, runs 24/7 via systemd.

The user is paper-trading first to validate signals before going live. **No real money is at risk** as currently configured.

---

## 2. Locked-in stack (do not re-debate)

| Component | Service | Cost/mo |
|---|---|---|
| Equity broker | Alpaca paper | $0 |
| Equity real-time bars | Alpaca SIP (Algo Trader Plus) | $99 | *Active on all 3 accounts, verified 2026-08-14*
| Equity historical | Polygon Stocks Starter | $29 |
| News firehose | Alpaca News WebSocket (Benzinga) | $0 |
| Sentiment | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | ~$2-4/day |
| ~~Futures walls~~ | ~~Databento CME Standard~~ | ~~$179~~ — **CANCELED** (Standard does not include live MBP-10) |
| Compute | Hetzner CPX21 Ashburn VA | $8 |
| Storage | SQLite | $0 |

**Current monthly cost: ~$200-260/mo** (sentiment is variable).

**Key facts that have already cost time when forgotten:**
- Polygon Stocks Starter is 15-minute DELAYED. Used for HISTORICAL ONLY. Real-time bars come from Alpaca SIP.
- Databento Standard ($179) does NOT include live MBP-10. Plus ($1,500) does. The user canceled Databento on 2026-04-28.
- Claude Pro/Max subscription does NOT include API access. Separate billing at console.anthropic.com.
- **2026-08-14**: `alpaca_data_feed` was on `iex` from 2026-05-14 to 2026-08-14 on the stated basis that PA3QAZ941NFN lacked Algo Trader Plus. That was wrong or became wrong: the subscription is Active across all three Alpaca accounts. IEX is a single exchange and understates pre-market volume to 5-15% of true, so every RVOL-gated signal computed in that window ran against a fraction of real volume. Restored to `sip`. Do not re-flip to `iex` without re-checking the Alpaca dashboard first.
- **2026-08-14**: Schwab Trader API app `trading-feed-daemon` created (Production, Ready For Use, Order Limit 0). Schwab has NO time-and-sales service - level one is officially conflated and there is no tape endpoint anywhere in the API. Schwab's role is `NASDAQ_BOOK` depth only (MPID, per-MM size, per-MM quote time). The tape comes from Alpaca SIP. Do not re-litigate this.

---

## 3. Current deployment state (as of 2026-04-28 ~22:00 UTC / 6 PM ET)

> **STALE as of 2026-08-14.** This section describes April. The repo was dormant from mid-May to mid-August, and the gap-and-go fork has since been shut down. Treat every status line below as historical until re-verified against the live systems. Do not act on it.

### VPS

- **Server**: Hetzner CPX21, Ashburn VA, Ubuntu 24.04
- **IP**: `5.161.199.155`
- **SSH**: `ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155` (from Windows PowerShell)
- **App location**: `/opt/trader/app/`
- **Python venv**: `/opt/trader/.venv/`
- **Systemd unit**: `trader.service`
- **Logs**: `journalctl -u trader.service`
- **Secrets**: `/etc/trading-platform/env` (mode 0600, owned by root)
- **Service user**: `trader` (no shell login)

### Application status

- ✅ Service running, S&P 500 watchlist (503 symbols)
- ✅ News + sentiment pipeline working (~120-150 sentiment scores/hour)
- ✅ Backfill working (499/503 daily contexts on 2026-04-28)
- ✅ Daily routine fires (8:30 ET backfill, 9:30 ET PM context, 15:55 ET flatten, 16:30 ET journal)
- ⚠️ **OBSERVABILITY GAP** (re-diagnosed 2026-04-29): the prior "WebSocket died silently" entry was a misdiagnosis. The verification command it relied on greps for keywords that don't fire during normal runtime, so it produced false-negatives regardless of pipeline health. The real blocker is in `main.py` `SymbolState`: `last_decision_action` defaults to `"Hold"` and `last_decision_setup` to `"none"`, which match the steady-state Hold/none decision the engine emits before any setup is detected. The dedup check in `_evaluate_and_execute` (`is_actionable or is_changed`) therefore never logs the first decision per ticker, and the decisions table stays empty even when bars are flowing. Empty table tells us nothing. Pipeline likely healthy. Fix: change the two defaults to `None` sentinel values so the first-per-ticker call always logs. See "Active issue" in section 5.
- ⚠️ Task supervisor patch (`_task_supervisor` in `main.py`, deployed 2026-04-28 ~6 PM ET): defensive only, not validated against any real failure mode. `unverified` per Rule 11. It only catches *completed/raised* tasks; a hung WebSocket idle in `await ws.recv()` would not trigger it. Keep, but don't treat it as a fix.
- ⏸ Futures wall scanner DISABLED in config (Databento canceled)
- ⏸ Phase 7 (Polygon options walls) DEFERRED — see section 7 below

### What runs daily (ET)

| Time | Event |
|---|---|
| 04:00 | Pre-market trading begins — Alpaca starts streaming PM bars |
| 08:30 | Polygon backfill: 300d daily bars + 20d PM volume baselines for all 503 tickers |
| 09:00 | Market data subsystems active |
| 09:30 | Pre-market context computed per symbol from buffered PM bars |
| 09:35 | Signal evaluation begins — first orders possible |
| 15:55 | Flatten routine: cancel all orders + close all positions |
| 16:00 | Market closes |
| 16:30 | EOD journal written to `/opt/trader/app/journals/<date>.md` |

---

## 4. Architecture

```
trading_platform/
├── README.md
├── requirements.txt
├── main.py                          # asyncio orchestrator + execution pipeline
├── config/
│   └── settings.yaml                # all tunable params, watchlist, schedule
├── data/
│   ├── bar_types.py                 # MinuteBar, FiveMinBar dataclasses
│   ├── news_feed.py                 # Alpaca News WebSocket
│   ├── news_pipeline.py             # batch headlines + Haiku scoring + SQLite
│   ├── alpaca_market_data.py        # Alpaca SIP bars WebSocket
│   ├── bar_aggregator.py            # 1-min → 5-min rollup
│   ├── polygon_feed.py              # historical REST + concurrent backfill
│   └── databento_feed.py            # ES MBP-10 (currently unused, code dormant)
├── analysis/
│   ├── sentiment.py                 # Claude Haiku batch scorer with prompt caching
│   ├── indicators.py                # SMA/EMA/RSI/MACD/Bollinger/ADX/VWAP + signal logic
│   └── futures_walls.py             # detect_walls + persistent scanner (dormant)
├── strategy/
│   ├── signal_engine.py             # evaluate_trade() — combines sentiment + tech + walls
│   └── risk.py                      # validate_order() with 20%/90%/2% caps + size_from_risk()
├── execution/
│   └── alpaca_orders.py             # bracket orders + 30s equity cache
├── journal/
│   └── eod_report.py                # daily markdown report generator
└── scripts/
    └── test_databento_connection.py # diagnostic (currently irrelevant)
```

### Data flow

```
News firehose (Alpaca) → keyword filter → queue → Haiku batch (60s) → SQLite sentiment
Equity bars (Alpaca SIP) → bar aggregator (1m→5m) → on_5min_bar handler
Daily routine (timer) → Polygon REST → DailyContext + PremarketContext per symbol

For each new 5-min RTH bar:
  on_5min_bar(bar)
    → compute_intraday_indicators(symbol_df)
    → generate_signal(df, daily_ctx, premarket_ctx)  # gap_and_go OR pullback OR hold
    → latest_sentiment(db, ticker, max_age=3600)
    → evaluate_trade(ticker, sentiment, tech, walls)  # walls=None now
    → if Buy/Sell:
        validate_order(...) → submit_bracket_order(...) → log to SQLite
```

### Signal logic (key decisions)

**Pullback setup (mean reversion in trend):**
- Bull regime + close > SMA20 + RSI < 35 + MACD turning up + close ≥ VWAP
- Sentiment ≥ +5 required
- Walls: would require support nearby + no overhead resistance, but `require_walls_for_pullback: false` in config so walls are confirming-only since Databento canceled

**Gap-and-go setup (news-driven momentum):**
- 9:35-10:00 ET window only
- Unusual RVOL (≥5x) + gap >1% + price holding gap level
- Sentiment ≥ +3 required (lower bar than pullback)
- Walls confirming-only

**Risk validation:**
- Max position: 20% of equity
- Max total exposure: 90% of equity
- Auto stop-loss: 2% from entry
- Sizing: target 0.5% portfolio risk per trade via `size_from_risk()`
- No pyramiding (skip if same-direction position exists)
- No flips (opposite signal with existing position is logged but not executed)

---

## 5. Known issues + open items

### Active issue: SymbolState dedup default masks all baseline activity (re-diagnosed 2026-04-29)

**Symptom**: `decisions` and `orders` tables in `/opt/trader/app/trading.db` have been empty across every day the platform has run (Apr 27, 28, 29 all show `COUNT(*) = 0`). News + sentiment writes work fine on the same DB (546 rows across the same dates), so the DB and write path are healthy.

**Root cause** (verified by reading source):
- `SymbolState` in `main.py` defaults `last_decision_action = "Hold"` and `last_decision_setup = "none"`.
- `signal_engine.evaluate_trade` returns Hold/none for the steady state (no qualifying setup, warmup, insufficient data).
- The dedup gate `if is_actionable or is_changed:` in `_evaluate_and_execute` therefore evaluates False on the very first call per ticker because the decision matches the defaults. Nothing is logged. State stays at defaults. Same outcome on every subsequent call until a setup transitions away from Hold/none — which has not happened.

**Implication**: the empty decisions table is *not* evidence of a broken bar pipeline. We have no empirical proof the platform works AND no empirical proof it doesn't. Yesterday's "WebSocket died silently" diagnosis was an inference from the same flawed grep documented below; it should not be treated as confirmed.

**The flawed verification command** (do not use, keeping here for reference so we don't recreate it):
```powershell
# BROKEN — grep keywords don't fire during normal runtime
ssh ... "journalctl -u trader.service --since '09:30' --no-pager | grep -E 'data\.alpaca_market_data|data\.bar_aggregator|Buy|Sell|Hold|Task' | tail -40"
```
- `data.alpaca_market_data` only emits INFO at boot (auth + subscribe), never per bar.
- `data.bar_aggregator` only emits warnings, silent during normal operation.
- Hold decisions log at DEBUG (filtered out at the configured INFO level).
- Buy/Sell only fire on actionable trades; rare with current strict thresholds.
- "Task" only fires if the supervisor catches a death.

**Correct verification** (use this once the dedup fix is deployed):
```powershell
# Inline-quoted variant breaks under PowerShell on Windows. Use bash -s with a verbatim heredoc:
@'
python3 - << 'PYEOF'
import sqlite3
conn = sqlite3.connect('/opt/trader/app/trading.db')
print(conn.execute("SELECT date(ts,'unixepoch'), COUNT(*) FROM decisions GROUP BY 1 ORDER BY 1").fetchall())
PYEOF
'@ | ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 'bash -s'
```
After dedup fix, expect ~503 rows per trading day (one first-decision-per-ticker baseline) plus any actionable transitions.

**Fix to deploy** (after market close, ~16:01 ET):
- Change `SymbolState.last_decision_action: str = "Hold"` → `str | None = None`
- Change `SymbolState.last_decision_setup: str = "none"` → `str | None = None`
- The `is_changed` comparison still works because `None != "Hold"` is True on the first call.

**Verification pending**: Wednesday 2026-04-29 PM (after dedup fix is deployed at market close, then check decisions table on Thursday 2026-04-30 by 14:00 UTC).

### Sentiment JSON parse error (occasional)

Claude Haiku returns malformed JSON ~once per day. Currently logs error and skips that batch. Not blocking but means a few headlines per day don't get scored.

### Anthropic credits

User started with $20. Will need to top up at https://console.anthropic.com/settings/billing as needed. Auto-reload available there. Burn rate at 503 watchlist scale is roughly $5-15/day.

---

## 6. User preferences (strict)

- Direct/concise. No filler. No "great question."
- Less em dashes.
- Step-by-step with confirmation between steps for any operational task.
- Specific to this situation, not generic.
- Recommendations not "it depends."
- Explicitly distinguish verified-just-now vs from-training-may-be-stale.
- Search the web before claiming current state of any external service.
- Test scripts in sandbox before pasting them.

User has stated they don't have technical depth to question Claude's claims. Apply Rule 5 of CLAUDE_PREFLIGHT.md religiously.

---

## 7. Phase roadmap

### Done
- Phase 1: Architecture scaffolding
- Phase 2: News + sentiment pipeline
- Phase 3: Indicators + market data
- Phase 4: Futures walls (built but DORMANT — Databento canceled)
- Phase 5: Signal engine + orchestrator
- Phase 6: Risk + execution + EOD journaling
- VPS deployment

### Open
- **Phase 7 (DEFERRED)**: Options walls via Polygon Options Starter ($29/mo)
  - **Why**: User wants per-stock call/put walls based on options open interest as a confirmation layer
  - **Concept**: Per-ticker daily snapshot of options chain, identify highest-OI strikes (call wall = resistance, put wall = support)
  - **Replacing**: The futures-walls integration (futures gave one index-wide signal; options walls give per-stock signals — better fit for the use case)
  - **Status**: User has NOT subscribed to Polygon Options Starter yet. Build is gated on:
    - 1+ week of clean paper data on news + tech only
    - Subscription to Polygon Options Starter
    - Sandbox test of options chain fetch at 503-ticker scale before deploying
  - **Estimated build**: ~300-500 lines new code + integration changes

### Possible future
- Phase 8: Dashboard / web UI for visualizing equity curve, decisions, fills

---

## 8. How to use this Cowork session

1. **Read this entire file first.** Do not skim.
2. **Read `CLAUDE_PREFLIGHT.md`** for the rules of engagement on operational tasks.
3. **The codebase lives in `trading_platform/`** in this Cowork folder. Read whichever module is relevant to the user's question.
4. **The actual running platform is on the VPS** at `5.161.199.155`. Code in this folder is the *source of truth* — push changes via scp.
5. **Do not modify or trade with real money** without explicit user confirmation. Paper-only is the default state.
6. **For any change**, follow the workflow:
   - Read the relevant existing file(s)
   - State what's tested and what isn't
   - Make the change
   - Sandbox-test if possible
   - Show the diff or full file
   - Get user confirmation before deploying
7. **Use the user's PowerShell + scp pattern** for deployment, not direct VPS edits via Cowork (Cowork doesn't have SSH access to the VPS).

---

## 9. Quick operational reference

| Task | Command (run from PowerShell) |
|---|---|
| Open VPS shell | `ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155` |
| One-shot command on VPS | `ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "<linux cmd>"` |
| Check service health | `ssh ... "systemctl status trader.service --no-pager \| head -10"` |
| Tail live logs | `ssh ... "journalctl -u trader.service -f"` (Ctrl+C to exit) |
| Read today's journal | `ssh ... "cat /opt/trader/app/journals/$(date +%F).md"` |
| Restart service | `ssh ... "systemctl restart trader.service"` |
| Upload single file | `scp -i $env:USERPROFILE\.ssh\hetzner_trader local\path.py root@5.161.199.155:/opt/trader/app/path.py` |
| Re-enable futures (if Databento ever resolved) | `ssh ... "sed -i 's/^  enabled: false.*/  enabled: true/' /opt/trader/app/config/settings.yaml && systemctl restart trader.service"` |

---

## 10. The five environment variables on the VPS

Located at `/etc/trading-platform/env` with mode 0600:

```
ALPACA_API_KEY        # paper account
ALPACA_API_SECRET     # paper account
ANTHROPIC_API_KEY     # console.anthropic.com — separate from claude.ai subscription
POLYGON_API_KEY       # Stocks Starter tier
DATABENTO_API_KEY     # currently unused (Databento canceled), key still set
```

Loaded into the service via systemd's `EnvironmentFile=/etc/trading-platform/env` directive.

---

_End of blueprint. If you've read this far, you have what you need to pick up this project from cold start._
