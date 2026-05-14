"""Diagnostic: test Alpaca WS auth on IEX, SIP, and News endpoints in isolation.

Prints raw auth replies so we can distinguish:
  - 'subscription not available' (no Algo Trader Plus on this account)
  - 'connection limit exceeded' (key already in use elsewhere)
  - 'authenticated' (clean — problem is in main.py concurrency, not the account)

Run from C:\\trading\\LLM model with .venv activated and
ALPACA_API_KEY + ALPACA_API_SECRET set in the same shell:

    python scripts/probe_alpaca_endpoints.py

Each endpoint is probed in isolation with a 3-second gap between, so a
connection-limit error in endpoint #1 cannot interfere with endpoint #2.
No credential material is printed (only the auth reply, which contains an
auth status string, never the key).
"""
import asyncio
import json
import os

import websockets


ENDPOINTS = [
    ("IEX bars  (free, requires no subscription)", "wss://stream.data.alpaca.markets/v2/iex"),
    ("SIP bars  (requires Algo Trader Plus $99/mo)", "wss://stream.data.alpaca.markets/v2/sip"),
    ("News feed (free, requires no subscription)", "wss://stream.data.alpaca.markets/v1beta1/news"),
]


async def probe(label: str, url: str) -> None:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not (key and secret):
        print("ERROR: ALPACA_API_KEY / ALPACA_API_SECRET not in env")
        return
    print(f"\n--- {label}")
    print(f"    {url}")
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            try:
                connect_msg = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"    connect: {connect_msg[:200]}")
            except asyncio.TimeoutError:
                print("    connect: <no greeting within 5s>")
            await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
            try:
                auth_reply = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"    auth   : {auth_reply[:200]}")
            except asyncio.TimeoutError:
                print("    auth   : <no reply within 5s>")
    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")


async def main() -> None:
    for label, url in ENDPOINTS:
        await probe(label, url)
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
