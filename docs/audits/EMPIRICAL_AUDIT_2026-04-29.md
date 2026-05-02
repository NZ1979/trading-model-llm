# Empirical Audit — 2026-04-29

This document records what was tested in a sandbox using the actual code from
`cowork_migration/`, what was found, and the testing depth of each finding
per Rule 11.

All test scripts live in the sandbox at
`/sessions/.../outputs/code_under_test/test_*.py`. Each test imports and
exercises the production module unmodified. No hypotheticals; only what
the code actually does when run.

## Confirmed bugs

### Bug A — Gap-and-go is structurally unreachable
**Severity:** Critical. The platform's primary momentum-trade path cannot
fire under any market conditions.

**Mechanism:** `_evaluate_and_execute` in `main.py` short-circuits when
`len(df_ind) < min_rth_bars_warmup` (config: 50). `df_ind` is RTH-filtered
5-min bars. RTH bars accumulate at one per 5 min from 9:30 ET, so the gate
opens at **9:30 + 50×5 = 13:40 ET = 1:40 PM ET**.

`_check_gap_and_go` in `analysis/indicators.py` (line 467-470) requires the
last bar's ET timestamp to fall in the **9:35-10:00 ET window**:

```python
in_window = (
    (last_ts.hour == 9 and last_ts.minute >= 35)
    or (last_ts.hour == 10 and last_ts.minute == 0)
)
if not in_window:
    return None
```

The gap-and-go window closes 3h40m before the warmup gate opens. The two
gates are mutually exclusive.

**Testing depth:** `integration-tested`.
- `test_gng_unreachable.py` replays a synthetic full-day session with strong
  PM RVOL (37.82x, well above the 5x threshold) and a 2.19% gap up.
  At every bar from 9:35-10:00 ET, the warmup gate trips
  (`rth=2, 3, 4, 5, 6, 7`). First eval that runs is 13:35 ET — past the
  window. Output:
  ```
  Evals that ran during 9:35-10:00 ET (gap_and_go window): 0
  >>> GAP_AND_GO IS STRUCTURALLY UNREACHABLE IN THIS CODE PATH <<<
  ```

**Why this hasn't been noticed:** The gap-and-go gate at line 457
(`if not premarket_ctx.is_unusual_volume: return None`) is also gated on
PM volume baselines. If `pm_baselines` were ever empty for all tickers
(`historical_pm_volumes=None` → RVOL=0 → `is_unusual_volume=False`), the
path would silently no-op even if the warmup gate were satisfied. So
gap-and-go has TWO independent conditions that both block it; either alone
would suffice to explain zero firings.

**Fix sketch:** The gap-and-go check has to run BEFORE the 50-bar warmup
gate. Either move `_check_gap_and_go` upstream of the warmup check (it only
needs PM context + the last close + the time window — not 50 bars of
indicators), or lower the warmup specifically for the gap-and-go path.

### Bug B — SymbolState dedup defaults make the decisions table empty
**Severity:** High (observability). Doesn't kill trading directly but
makes "did anything happen?" impossible to answer from the DB.

**Mechanism:** `SymbolState` in `main.py` defaults
`last_decision_action="Hold"` and `last_decision_setup="none"`. The dedup
check in `_evaluate_and_execute` is:

```python
is_actionable = decision.action != "Hold"
is_changed = (decision.action != state.last_decision_action
              or decision.setup != state.last_decision_setup)
if is_actionable or is_changed:
    self._log_decision(decision)
```

When the engine returns the steady-state Hold/none for a ticker that has
never had a decision logged, `is_actionable=False`, `is_changed=False`,
nothing is written. The state stays at defaults. Same outcome on every
subsequent steady-state Hold.

**Testing depth:** `integration-tested`. The end-to-end replay of a full
synthetic session (`test_e2e_sharp_v.py`) ran 28 evaluations through
`evaluate_trade`, all returning Hold/none, all matching the defaults,
zero rows written to the in-memory SQLite.

**Independently of Bug A:** Bug B alone explains why baseline activity
isn't visible. Even when bars flow correctly and signal eval runs, the DB
remains empty unless an actual setup transition occurs.

**Fix:** Two-line change in `SymbolState`:
```python
last_decision_action: str | None = None
last_decision_setup: str | None = None
```
The `is_changed` comparison still works (`None != "Hold"` is True on first
call), so the first decision per ticker per day always logs.

## Components verified working

These are `unit-tested` against the actual production code (no
mocks/replacements of the module under test):

| Module | Test file | Result |
|---|---|---|
| `data.bar_aggregator` | `test_aggregator.py` | 5/5 pass — emits 78 5-min bars over 6.5h RTH, correct timestamps, gap handling |
| `analysis.indicators._filter_to_rth` | `test_indicators.py` | DST handled, 78 RTH bars from 13h span |
| `analysis.indicators.compute_intraday_indicators` | `test_indicators.py` | All indicators non-NaN at 50 RTH bars, all NaN at <50 |
| `analysis.indicators.compute_daily_context` | `test_indicators.py` | regime=bull/neutral classified correctly |
| `analysis.indicators.generate_signal` (pullback) | `test_signal_engine.py` | Buy fires when conditions met (conf=100), Sell symmetric, all gates correct |
| `strategy.signal_engine.evaluate_trade` | `test_evaluate_trade.py` | Pullback Buy with PROD config + walls=None + sentiment≥+5 → Buy. setup field preserved when collapsed to Hold |

Total: 27/28 unit-test cases pass. The one failure is a bug in my test
harness (length mismatch when reassigning a DatetimeIndex), not in the
production code; the same scenario passed when reconstructed.

## Hypotheses I cannot empirically rule out

### Pullback may genuinely fire near-zero times even if Bug A is fixed
**Status:** `unverified`. Needs real Polygon historical data to test.

The pullback technical conditions require simultaneously:
- `close > sma_20` (current price above 20-bar mean)
- `rsi_14 < 35` (oversold)
- `macd_hist` cross from negative to positive in last 3 bars
- `close > vwap × 0.997`

In a smooth dip these conditions are mutually exclusive: when RSI is
oversold the price is below SMA20 by definition. The conjunction only
holds for very sharp V-shaped reversals where price snaps back above SMA20
within 1-2 bars while RSI is still recovering. Two synthetic price
constructions (one moderate, one sharp) both produced zero pullback
detections in unit-tested replays.

**This is a strategy design property**, not a bug per se. But combined
with Bug A killing gap-and-go, it would mean even a fully working platform
would generate very few signals.

To verify the actual frequency, we'd need to replay historical Polygon
1-min data for SPX names through the pipeline.

## Components NOT tested

I did not exercise these in the sandbox:
- `data.news_feed` (Alpaca News WebSocket)
- `data.news_pipeline` (Haiku scoring + DB write)
- `data.alpaca_market_data` WebSocket auth/subscribe handshake
- `data.polygon_feed` baseline backfill paths
- `execution.alpaca_orders` order submission
- `journal.eod_report` daily report generation
- The `_task_supervisor` restart logic
- `main.py` daily routine timing (8:30 / 9:30 / 15:55 / 16:30 sequencing)

These are not blockers for the question "why is the decisions table empty"
because we have positive evidence the news pipeline writes to DB
(sentiment row counts: 73/305/169 across Apr 27/28/29) and we have direct
empirical proof of bugs A and B being independent and sufficient
explanations for the bar-side path producing zero decision rows.

## Confidence assessment

- **Bug A (gap-and-go unreachable):** 99%. Direct empirical demonstration
  via `test_gng_unreachable.py`.
- **Bug B (dedup-default masks steady state):** 99%. Direct empirical
  demonstration via `test_e2e_sharp_v.py`.
- **No other bugs in the bar-to-decision pipeline (aggregator, indicators,
  signal_engine, evaluate_trade):** ~85%. Tested all the obvious failure
  modes; could still be a bug in code paths I didn't construct test data
  for, especially around timezone edges or partial-day boots.
- **Bugs in untested modules (news, orders, polygon backfill, supervisor):**
  unknown. Not asserting either way.

## What was wrong with prior diagnoses

- "WebSocket died silently on 2026-04-28" — was an inference from a grep
  that searched for keywords that don't fire during normal runtime.
  No evidence of WebSocket failure. False positive.
- "require_walls_for_pullback: true is the bug" — was based on reading
  the local workspace settings.yaml. The VPS has `false` (correct).
  The local file is stale and should not be treated as authoritative for
  what's running.
- "Dedup-default is the only observability bug" — incomplete. There's
  also Bug A above, which is structurally more severe.

## Files in this audit

- `cowork_migration/EMPIRICAL_AUDIT_2026-04-29.md` (this file)
- Sandbox tests at `/sessions/.../outputs/code_under_test/test_*.py`
