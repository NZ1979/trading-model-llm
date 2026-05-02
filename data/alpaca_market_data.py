"""Real-time 1-minute bars from Alpaca.

Two feeds available, selected via the `feed` parameter:
- "sip" (default): wss://stream.data.alpaca.markets/v2/sip
    Full SIP coverage. Requires Algo Trader Plus subscription ($99/mo).
    Accurate pre-market volume, full RTH/extended-hours coverage.
- "iex": wss://stream.data.alpaca.markets/v2/iex
    Free with any account. IEX-routed trades only. Pre-market volume is
    understated (~5-15% of true). Use only for development/testing.

Bars arrive seconds after each minute close. Timestamp is the START of the
1-min window in UTC, which matches our shared MinuteBar convention.

Subscriptions: send {"action":"subscribe","bars":["AAPL","MSFT",...]}.
There's no documented hard cap on subscription count for either feed; 500
S&P names is fine. The cap is per-account on concurrent connections (1 on
all individual plans), so don't try to run two of these at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed

from data.bar_types import MinuteBar

logger = logging.getLogger(__name__)

ALPACA_BARS_WS_SIP = "wss://stream.data.alpaca.markets/v2/sip"
ALPACA_BARS_WS_IEX = "wss://stream.data.alpaca.markets/v2/iex"


class AlpacaBarStream:
    """Real-time 1-min bar WebSocket with auto-reconnect.

    Usage
    -----
        async def handle(bar: MinuteBar) -> None:
            print(bar.symbol, bar.timestamp, bar.close, bar.volume)

        stream = AlpacaBarStream(key, secret, watchlist, handle, feed="sip")
        await stream.run()  # runs until stop() is called
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: set[str],
        on_bar: Callable[[MinuteBar], Awaitable[None]],
        feed: str = "sip",
    ) -> None:
        if feed not in ("sip", "iex"):
            raise ValueError(f"feed must be 'sip' or 'iex', got {feed!r}")
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = {s.upper() for s in symbols}
        self._on_bar = on_bar
        self._feed = feed
        self._url = ALPACA_BARS_WS_SIP if feed == "sip" else ALPACA_BARS_WS_IEX
        self._running = False

    async def _authenticate(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Alpaca sends {"T":"success","msg":"connected"} immediately on connect
        await ws.recv()
        await ws.send(json.dumps({
            "action": "auth",
            "key": self._api_key,
            "secret": self._api_secret,
        }))
        msg = json.loads(await ws.recv())
        items = msg if isinstance(msg, list) else [msg]
        ok = any(
            isinstance(m, dict) and m.get("msg") == "authenticated"
            for m in items
        )
        if not ok:
            # Common cause: trying to use SIP feed without Algo Trader Plus
            raise RuntimeError(
                f"Alpaca bars auth failed on {self._feed} feed. "
                f"If using 'sip', verify Algo Trader Plus is active on your "
                f"account. Response: {msg}"
            )
        logger.info("Alpaca bars WS authenticated (%s feed)", self._feed)

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        await ws.send(json.dumps({
            "action": "subscribe",
            "bars": sorted(self._symbols),
        }))
        ack = await ws.recv()
        logger.info(
            "Alpaca bars subscribed: %d symbols. Ack[:200]: %s",
            len(self._symbols), str(ack)[:200],
        )

    async def _process_message(self, raw: str | bytes) -> None:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON Alpaca message: %r", raw[:200])
            return

        for msg in messages:
            if msg.get("T") != "b":
                continue  # not a bar

            try:
                # Alpaca bar fields:
                #   T="b", S=symbol, t=ISO timestamp (start of minute),
                #   o, h, l, c, v, n (trade count), vw (vwap)
                ts_raw = msg["t"]
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                bar = MinuteBar(
                    symbol=msg["S"],
                    timestamp=ts,
                    open=float(msg["o"]),
                    high=float(msg["h"]),
                    low=float(msg["l"]),
                    close=float(msg["c"]),
                    volume=int(msg["v"]),
                    vwap=float(msg["vw"]) if msg.get("vw") is not None else None,
                )
            except (KeyError, ValueError, TypeError):
                logger.exception("Malformed Alpaca bar: %s", msg)
                continue

            try:
                await self._on_bar(bar)
            except Exception:
                logger.exception("on_bar callback failed for %s", bar.symbol)

    async def run(self) -> None:
        """Run the WebSocket loop with exponential-backoff reconnect."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**24,
                ) as ws:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    backoff = 1.0
                    async for raw in ws:
                        await self._process_message(raw)
            except ConnectionClosed as e:
                logger.warning("Alpaca bars WS closed (%s), retry in %.1fs",
                               e, backoff)
            except Exception:
                logger.exception("Alpaca bars WS error, retry in %.1fs", backoff)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._running = False
