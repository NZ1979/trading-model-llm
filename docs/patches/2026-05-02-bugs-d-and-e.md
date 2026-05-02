# Bugs D + E patch — 2026-05-02

Two related fixes in `execution/alpaca_orders.py`. Both touch error handling
in different parts of the same module, so they ship as a coordinated change.

## Bug D — HTTP error bodies were silently discarded

**Symptom**: every Alpaca rejection logged as a bare status code (e.g.
"HTTP 422: Unprocessable Entity") with no detail. Required a separate
diagnostic script to recover the actual reason.

**Root cause**: `_get`, `_post`, `_delete` called `resp.raise_for_status()`
before reading the response body. The resulting `aiohttp.ClientResponseError`
contains only the status code and reason phrase; the body (where Alpaca's
specific code + message live) was already gone.

**Fix**:
- New `AlpacaAPIError` exception class that reads the body and parses it
  as JSON when possible, exposing `code` + `message` separately.
- `_get`, `_post`, `_delete` updated to raise `AlpacaAPIError` instead of
  using `raise_for_status()`. Body is read *before* the exception fires.
- `submit_bracket_order`'s catch clause updated from
  `except aiohttp.ClientResponseError` to `except AlpacaAPIError`.

After this fix, errors look like:
> `Order rejected for MO: HTTP 422 POST /v2/orders: code=40010001 bracket orders require take_profit.limit_price`

## Bug E — multi-status responses were counted as success

**Symptom**: 2026-05-01 flatten routine logged "Closed 1 positions errors=0"
while the CLX short position remained open. User had to manually close it
1+ hour later. Same bug applies to the cancel-orders path.

**Root cause**: Alpaca's bulk DELETE endpoints (`/v2/positions`, `/v2/orders`)
return HTTP 207 multi-status with a JSON array where each item has its own
per-item `status` field. The platform counted `len(array)` as successes,
silently treating per-item failures as success.

**Fix**:
- New `_split_multistatus(items, key)` helper that walks the array and
  returns `(successes, failures)` based on each item's `status`.
- `close_all_positions` updated to call this helper for both the
  cancel-orders and close-positions paths. Per-item failures are logged
  with the symbol/id and the embedded body, and added to `result["errors"]`.

After this fix, the 2026-05-01 scenario logs:
> `position close failed: symbol=CLX status=422 body={'available': '0', 'code': 40310000, 'existing_qty': '225', 'held_for_orders': '225', 'message': 'insufficient qty available for order ...'}`

## Test results

All 13 regression tests pass (`test_bugs_d_and_e.py`):

- 3 tests verify `AlpacaAPIError` parses JSON bodies, falls back on non-JSON,
  and handles empty bodies without crashing.
- 6 tests verify `_split_multistatus` correctly handles all-success,
  all-failure, mixed, empty list, non-list input, and non-dict items.
- 3 tests verify `close_all_positions` integration: the partial-failure
  scenario from 2026-05-01 (cancel succeeds, position close 422s),
  full-success path, and empty-multistatus path.
- 1 additional test verifies `submit_bracket_order`'s catch clause now
  receives `AlpacaAPIError` and surfaces the structured message.

## What this fix does NOT do

- **No retry on held_for_orders.** When the cancel-then-close race
  produces a 422, this fix surfaces the error correctly but doesn't
  automatically retry. The user can decide whether to add a small
  delay between cancel and close (probably the right next move) as a
  separate change.
- **No changes to strategy logic.** The cooldown-after-stop-out and
  fresh-bar-entry improvements are still pending design.

## Files

- `alpaca_orders.py.patched` — full patched file (replaces
  `/opt/trader/app/execution/alpaca_orders.py`).
- `alpaca_orders.py.diff` — unified diff vs. yesterday's deployed version.
- `test_bugs_d_and_e.py` — regression tests.

## Deploy procedure

Same pattern as Bug A/B/C deploys. Market is closed (Saturday); zero risk
of disturbing live trading.

```powershell
# 1. Backup current file
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "cp /opt/trader/app/execution/alpaca_orders.py /opt/trader/app/execution/alpaca_orders.py.bak_2026-05-02"

# 2. Upload patched file
scp -i $env:USERPROFILE\.ssh\hetzner_trader "C:\Users\kings\OneDrive\Documents\Claude\Projects\trading_platform\cowork_migration\patches_2026-05-02\alpaca_orders.py.patched" root@5.161.199.155:/opt/trader/app/execution/alpaca_orders.py

# 3. Syntax check + verify the new symbols are present
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "python3 -m py_compile /opt/trader/app/execution/alpaca_orders.py && echo 'syntax OK' && grep -c 'AlpacaAPIError\|_split_multistatus' /opt/trader/app/execution/alpaca_orders.py"

# 4. Restart service
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "systemctl restart trader.service"

# 5. Verify clean boot
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "sleep 15 && journalctl -u trader.service --since '20 sec ago' --no-pager | grep -E 'authenticated|subscribed|booted|account equity|ERROR|Traceback'"
```

Rollback if needed:

```powershell
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "cp /opt/trader/app/execution/alpaca_orders.py.bak_2026-05-02 /opt/trader/app/execution/alpaca_orders.py && systemctl restart trader.service"
```

## Verification target

Monday 2026-05-04 after the 15:55 ET flatten fires, check that flatten
errors are now visible:

```bash
journalctl -u trader.service --since '19:55:00' --until '19:57:00' --no-pager | grep -iE 'flatten|cancelled|closed|position close failed|order cancel failed'
```

If positions close cleanly, log says `Closed N positions (failures=0)` and
nothing else. If there's a held_for_orders or any other rejection, we'll
now see the per-symbol error with the actual message.
