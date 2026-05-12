"""Standalone Alpaca SIP WS probe. Connects, authenticates, subscribes to
a handful of high-volume tickers, and counts incoming bar messages for
60 seconds. Reports total bars + any errors. Does NOT touch trader.service.

Reads ALPACA_API_KEY / ALPACA_API_SECRET from env. Run on VPS:
    set -a && . /etc/trading-platform/env && set +a
    python3 /tmp/probe_alpaca_ws.py
"""
import asyncio
import json
import os
import sys
import time
from collections import Counter

import websockets

PROBE_TICKERS = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "TSLA", "AMD", "META"]
WS_URL = "wss://stream.data.alpaca.markets/v2/sip"
PROBE_SECONDS = 60


async def main() -> int:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not (key and secret):
        print("ERROR: ALPACA_API_KEY / ALPACA_API_SECRET not in env")
        return 1

    print(f"Connecting to {WS_URL} ...")
    msg_counts: Counter[str] = Counter()
    bar_counts: Counter[str] = Counter()
    t0 = time.monotonic()

    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
        # Auth
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        auth_reply = await ws.recv()
        print(f"Auth reply: {auth_reply[:200]}")
        # Subscribe
        await ws.send(json.dumps({"action": "subscribe", "bars": PROBE_TICKERS}))
        sub_reply = await ws.recv()
        print(f"Subscribe reply: {sub_reply[:200]}")
        print(f"\nListening for {PROBE_SECONDS}s ...\n")
        deadline = t0 + PROBE_SECONDS
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            try:
                msgs = json.loads(raw)
            except json.JSONDecodeError:
                print(f"  non-JSON: {raw[:80]!r}")
                continue
            for m in msgs:
                t = m.get("T") or "?"
                msg_counts[t] += 1
                if t == "b":
                    sym = m.get("S", "?")
                    bar_counts[sym] += 1
                    print(
                        f"  BAR {sym:<6} ts={m.get('t')} "
                        f"c={m.get('c')} v={m.get('v')}"
                    )

    elapsed = time.monotonic() - t0
    print()
    print("=" * 60)
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Message counts by type: {dict(msg_counts)}")
    print(f"Bar counts by ticker  : {dict(bar_counts)}")
    print(f"Total bars received   : {sum(bar_counts.values())}")
    print()
    if sum(bar_counts.values()) > 0:
        print("OK - Alpaca IS delivering bars. Problem is downstream in our app.")
    else:
        print("FAIL - Alpaca delivered ZERO bars in 60s. WS subscribe ACK'd but no data.")
        print("  Likely entitlement: account does not have SIP feed permission,")
        print("  or market is fully closed (no after-hours activity on these tickers).")
    return 0 if sum(bar_counts.values()) > 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
