# Patches for the 2026-04-29 audit findings

Two bug fixes addressing the empirically-confirmed bugs from
`EMPIRICAL_AUDIT_2026-04-29.md`. Both fixes are tested in
`test_bug_a_fix.py` and `test_bug_b_fix.py` (sandbox-runnable).

## Files

- `main.py.patched` — full patched `main.py` (replace
  `/opt/trader/app/main.py` with this).
- `main.py.diff` — unified diff vs. the version in `cowork_migration/main.py`
  at audit time. Two hunks: SymbolState defaults (Bug B) and
  `_evaluate_and_execute` warmup gate (Bug A).
- `indicators.py.patched` — full patched `analysis/indicators.py`.
- `indicators.py.diff` — unified diff. One hunk: `generate_signal`
  reordered so opening-volatility + gap-and-go run before indicator-warmup
  gates (Bug A).
- `test_bug_a_fix.py`, `test_bug_b_fix.py` — regression tests. Run from
  the sandbox with the patched modules on `sys.path`.

## What the patches do

### Bug A (gap-and-go reachability)

Two coordinated changes:

1. **`analysis/indicators.py:generate_signal`** — moves the opening-
   volatility check and the `_check_gap_and_go` call ABOVE the existing
   `len < 50` and NaN-warmup gates. Pullback path is unchanged: it still
   gates on `daily_ctx is None or len(intraday_df) < 50` and the NaN
   check.
2. **`main.py:_evaluate_and_execute`** — removes the second warmup
   short-circuit (`if len(df_ind) < min_bars: return`). Replaced with
   `df_ind.empty` check. The pullback path still self-gates inside
   `generate_signal`, so this only opens the gap-and-go path; pullback
   is unchanged.

After this fix, gap-and-go can fire from the 9:35 ET 5-min bar onward
(when `state.bars` has 2+ RTH bars) instead of being permanently
blocked by the 50-bar warmup.

### Bug B (SymbolState dedup defaults)

One change in `main.py:SymbolState`:

```diff
-    last_decision_action: str = "Hold"
-    last_decision_setup: str = "none"
+    last_decision_action: str | None = None
+    last_decision_setup: str | None = None
```

The dedup comparison `decision.action != state.last_decision_action`
still works (`"Hold" != None` is True), so the first decision per ticker
per day always logs. Subsequent identical decisions still dedup correctly
because state is updated to the actual values after the first write.

## Test results against patched code

Run from sandbox with all source on `sys.path`:

| Suite | Pass/Total |
|---|---|
| test_aggregator.py | 5/5 |
| test_indicators.py | 7/7 |
| test_signal_engine.py | 7/8 (1 ERROR is a length-mismatch in the test fixture, not the production code) |
| test_evaluate_trade.py | 8/8 |
| test_bug_a_fix.py (this patch's regression tests) | 4/4 |
| test_bug_b_fix.py (this patch's regression tests) | 5/5 |

All previously-passing tests continue to pass against the patched code.
No regressions detected.

## What these patches do NOT do

- They do NOT add the instrumentation discussed earlier (per-bar
  counters, gate-hit tallies). That can be added separately if desired.
- They do NOT change any signal thresholds or strategy parameters.
- They do NOT touch the `_task_supervisor`, news pipeline, order
  submission, or any other module.
- They do NOT change config files. The VPS already has
  `require_walls_for_pullback: false`, which is correct.

## Suggested deploy sequence (after market close, ~16:01 ET)

1. SCP the two patched files to the VPS:
   ```powershell
   scp -i $env:USERPROFILE\.ssh\hetzner_trader main.py.patched root@5.161.199.155:/opt/trader/app/main.py
   scp -i $env:USERPROFILE\.ssh\hetzner_trader indicators.py.patched root@5.161.199.155:/opt/trader/app/analysis/indicators.py
   ```
2. Restart the service:
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "systemctl restart trader.service"
   ```
3. Verify clean boot and module imports load:
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "journalctl -u trader.service --since '10 sec ago' --no-pager | grep -E 'Started|booted|ERROR|Traceback' | head -10"
   ```
4. Tomorrow (2026-04-30) by 14:30 UTC, check the decisions table — expect
   ~503 baseline rows (one first-decision-per-ticker) plus any setup
   transitions or actionable trades.

## Verification command (replaces the broken one in PROJECT_BLUEPRINT.md)

```powershell
@'
python3 - << 'PYEOF'
import sqlite3
conn = sqlite3.connect('/opt/trader/app/trading.db')
print("Decisions by date:")
for row in conn.execute("SELECT date(ts,'unixepoch'), COUNT(*) FROM decisions GROUP BY 1 ORDER BY 1").fetchall():
    print(f"  {row[0]}: {row[1]}")
print("\nToday by action+setup:")
for row in conn.execute("SELECT action, setup, COUNT(*) FROM decisions WHERE date(ts,'unixepoch')=date('now') GROUP BY 1,2 ORDER BY 1,2").fetchall():
    print(f"  {row[0]:5} / {row[1]:12}: {row[2]}")
PYEOF
'@ | ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 'bash -s'
```
