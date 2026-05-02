"""Standalone Databento connection test for ES futures MBP-10.

Run AFTER:
  1. Subscribing to CME Standard plan ($179/mo) on databento.com
  2. Completing the non-professional license questionnaire in the portal
  3. Setting DATABENTO_API_KEY environment variable

Usage:
    export DATABENTO_API_KEY=db-...your-key-here...
    python scripts/test_databento_connection.py

Expected output (during ES trading hours):
  Connecting to Databento Live...
  Subscribed. Waiting for first MBP-10 snapshot (timeout 30s)...
    [1] SymbolMappingMsg (skipping)
  SUCCESS: ES.c.0 top of book at 2025-...
    Best bid: 47 @ 5234.0
    Best ask: 23 @ 5234.25
    Spread:   0.25
    Mid:      5234.1250
  ...full 10-level book printed...

Exits with status 0 on success, 1 on any failure with diagnostic messages.

Trading hours: ES trades Sun 6 PM ET through Fri 5 PM ET, with a daily
maintenance halt from 5-6 PM ET. Outside these windows, the connection
will succeed but no book updates arrive (you'll see the timeout path).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

try:
    import databento as db
except ImportError:
    print("ERROR: databento package not installed.", file=sys.stderr)
    print("       Run: pip install databento", file=sys.stderr)
    sys.exit(1)


PRICE_SCALE = 1e9  # Databento prices are int * 1e-9
TIMEOUT_SEC = 30


async def main() -> int:
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        print("ERROR: DATABENTO_API_KEY environment variable not set", file=sys.stderr)
        print("       Get your key from databento.com → Portal → API Keys", file=sys.stderr)
        return 1

    if not api_key.startswith("db-") or len(api_key) < 30:
        print(f"WARN: API key looks malformed ({len(api_key)} chars, "
              f"prefix={api_key[:3]}). Expected 32 chars starting with 'db-'.")

    print("Connecting to Databento Live...")
    client = db.Live(key=api_key)

    try:
        client.subscribe(
            dataset="GLBX.MDP3",
            schema="mbp-10",
            symbols="ES.c.0",
            stype_in="continuous",
        )
    except Exception as e:
        print(f"\nERROR: subscribe failed: {e}", file=sys.stderr)
        _diagnose(e)
        return 1

    print(f"Subscribed (GLBX.MDP3 / mbp-10 / ES.c.0). "
          f"Waiting for first snapshot (timeout {TIMEOUT_SEC}s)...")

    record_count = 0
    got_snapshot = False

    try:
        async with asyncio.timeout(TIMEOUT_SEC):
            async for record in client:
                record_count += 1

                # System messages and symbol-mapping records arrive first.
                # They have no `levels` attribute. Skip and keep waiting.
                levels = getattr(record, "levels", None)
                if levels is None:
                    record_type = type(record).__name__
                    print(f"  [{record_count:3d}] {record_type} (skipping)")
                    continue

                # Got an MBP-10 record with full top-10 book.
                ts_event = getattr(record, "ts_event", 0)
                ts_dt = datetime.fromtimestamp(ts_event / PRICE_SCALE, tz=timezone.utc)

                top = levels[0]
                bid_px = top.bid_px / PRICE_SCALE if top.bid_sz > 0 else None
                ask_px = top.ask_px / PRICE_SCALE if top.ask_sz > 0 else None

                print()
                print(f"SUCCESS: ES.c.0 top of book at {ts_dt.isoformat()}")
                if bid_px is not None:
                    print(f"  Best bid: {top.bid_sz:>5} @ {bid_px:>9.2f}")
                if ask_px is not None:
                    print(f"  Best ask: {top.ask_sz:>5} @ {ask_px:>9.2f}")
                if bid_px is not None and ask_px is not None:
                    print(f"  Spread:   {ask_px - bid_px:.2f}")
                    print(f"  Mid:      {(bid_px + ask_px) / 2:.4f}")

                print()
                print(f"  Full 10-level book:")
                print(f"  {'lvl':>3}  {'bid_sz':>7} {'bid_px':>10}  |  {'ask_px':>10} {'ask_sz':>7}")
                print(f"  {'-' * 3}  {'-' * 7} {'-' * 10}  |  {'-' * 10} {'-' * 7}")
                for i, lvl in enumerate(levels):
                    bp = f"{lvl.bid_px / PRICE_SCALE:.2f}" if lvl.bid_sz > 0 else "—"
                    ap = f"{lvl.ask_px / PRICE_SCALE:.2f}" if lvl.ask_sz > 0 else "—"
                    bs = lvl.bid_sz if lvl.bid_sz > 0 else "—"
                    as_ = lvl.ask_sz if lvl.ask_sz > 0 else "—"
                    print(f"  {i:>3}  {bs:>7} {bp:>10}  |  {ap:>10} {as_:>7}")

                got_snapshot = True
                break

    except asyncio.TimeoutError:
        print(f"\nTIMEOUT after {TIMEOUT_SEC}s. Received {record_count} non-book records.")
        _diagnose_timeout(record_count)
        return 1
    except Exception as e:
        print(f"\nERROR during streaming: {e}", file=sys.stderr)
        _diagnose(e)
        return 1
    finally:
        try:
            client.terminate()
        except Exception:
            pass

    return 0 if got_snapshot else 1


def _diagnose(exc: Exception) -> None:
    """Map common Databento errors to actionable hints."""
    msg = str(exc).lower()
    if "auth" in msg or "api key" in msg or "unauthorized" in msg:
        print("  → API key rejected. Verify the key in your Databento portal "
              "(Portal → API Keys) and check it isn't disabled.", file=sys.stderr)
    elif "license" in msg or "permission" in msg or "not authorized" in msg:
        print("  → License missing or not yet active. Two things to verify:", file=sys.stderr)
        print("    1. CME Standard plan is active (Portal → Plans and live data)", file=sys.stderr)
        print("    2. Non-professional questionnaire is submitted and processed", file=sys.stderr)
        print("       (can take a few minutes after submission)", file=sys.stderr)
    elif "subscription" in msg or "dataset" in msg:
        print("  → Dataset GLBX.MDP3 not enabled on your account. Subscribe to "
              "CME Standard plan in the portal.", file=sys.stderr)
    elif "symbol" in msg or "resolution" in msg:
        print("  → Symbol resolution failed. ES.c.0 should resolve to the front "
              "month — verify CME data is actually licensed.", file=sys.stderr)
    elif "connection" in msg or "timeout" in msg:
        print("  → Network issue. Check connectivity and Databento status page: "
              "https://status.databento.com", file=sys.stderr)


def _diagnose_timeout(record_count: int) -> None:
    """Connection succeeded but no book records arrived in 30s."""
    now_utc = datetime.now(timezone.utc)
    print()
    print(f"  Current UTC time: {now_utc.isoformat()}")
    print()
    print("  Connection authenticated, but no MBP-10 book updates arrived.")
    print("  Most likely causes:")
    if record_count == 0:
        print("  → Zero records of any kind — license may not be activated yet.")
        print("    Check Portal → Plans and live data for green status.")
    else:
        print("  → System messages received but no book data. Likely outside")
        print("    ES trading hours:")
        print("      Open:  Sunday 6:00 PM ET")
        print("      Close: Friday 5:00 PM ET")
        print("      Daily maintenance halt: 5:00-6:00 PM ET (M-Th)")
        print("    Re-run during active hours and you should see book updates")
        print("    immediately (ES emits thousands per second).")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
