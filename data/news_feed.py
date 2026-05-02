"""Real-time news ingestion from Alpaca's news WebSocket.

Alpaca's news feed (sourced from Benzinga) is free with any account, paper
or live. Headlines arrive within seconds of publication. We subscribe to the
full firehose and filter to S&P 500 tickers in code rather than trying to
maintain a 500-symbol subscribe list (Alpaca caps subscriptions and "*" is
simpler).

Why a keyword pre-filter?
-------------------------
Alpaca emits ~3,000-8,000 headlines/day. Most are noise: republished press
releases, generic market wraps, non-S&P names. Sending all of them through
Claude would cost $20-30/day and overwhelm the signal engine. The keyword
list below catches the headlines that historically drive intraday moves
(earnings, analyst actions, M&A, regulatory, C-suite changes). After this
filter, ~10-20% of headlines survive, which is what we batch to Claude.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

ALPACA_NEWS_WS = "wss://stream.data.alpaca.markets/v1beta1/news"

# Catalysts that historically drive intraday equity moves. Substring matched
# against headline + summary (lowercased). Keep this list tight; every false
# positive costs API tokens.
HIGH_IMPACT_KEYWORDS: frozenset[str] = frozenset({
    # Earnings & guidance
    "earnings", "eps", "beats", "misses", "beat estimates", "missed estimates",
    "guidance", "outlook", "revenue", "raises forecast", "lowers forecast",
    "preannounces", "warns",
    # Analyst actions
    "upgrade", "downgrade", "price target", "initiated coverage",
    "reiterates buy", "reiterates sell", "overweight", "underweight",
    "outperform", "underperform",
    # M&A and corporate actions
    "acquires", "acquisition", "merger", "buyout", "takeover", "to acquire",
    "spinoff", "stock split", "dividend", "buyback", "share repurchase",
    # Regulatory & legal
    "fda approval", "fda rejects", "fda grants", "lawsuit", "settlement",
    "investigation", "sec charges", "doj", "antitrust", "subpoena", "fined",
    # Operational / personnel
    "ceo", "cfo", "resigns", "steps down", "appointed", "fired", "ousted",
    "layoffs", "restructuring", "recall", "halts production", "delays",
    "bankruptcy", "chapter 11", "going private",
    # Market structure / activist
    "short report", "activist investor", "stake", "13d", "13g",
    "insider buying", "insider selling",
})


def passes_keyword_filter(headline: str, summary: str = "") -> bool:
    """Return True if the news item is worth scoring with Claude.

    Cheap substring scan. Runs in the WebSocket callback before queueing.
    """
    text = f"{headline} {summary}".lower()
    return any(kw in text for kw in HIGH_IMPACT_KEYWORDS)


class AlpacaNewsFeed:
    """Streams real-time news from Alpaca with auto-reconnect.

    The on_news callback is invoked for every news item that passes the
    keyword filter. Items that fail the filter are dropped silently (logged
    at DEBUG only).

    Usage
    -----
        async def handle(news: dict) -> None:
            print(news["headline"])

        feed = AlpacaNewsFeed(api_key, secret, watchlist, handle)
        await feed.run()
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        watchlist: set[str],
        on_news: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.watchlist = {s.upper() for s in watchlist}
        self.on_news = on_news
        self._running = False

    async def _authenticate(self, ws: websockets.WebSocketClientProtocol) -> None:
        await ws.send(json.dumps({
            "action": "auth",
            "key": self.api_key,
            "secret": self.api_secret,
        }))
        msg = json.loads(await ws.recv())
        if not (isinstance(msg, list) and msg and msg[0].get("msg") == "authenticated"):
            raise RuntimeError(f"Auth failed: {msg}")
        logger.info("News WS authenticated")

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Subscribe to the firehose; we filter to watchlist + keywords ourselves.
        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        ack = await ws.recv()
        logger.info("News WS subscribed: %s", ack)

    async def _process_message(self, raw: str | bytes) -> None:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON news message: %r", raw[:200])
            return

        for msg in messages:
            if msg.get("T") != "n":
                continue  # not a news event

            symbols = {s.upper() for s in msg.get("symbols", [])}
            # Only care about names on our watchlist
            if not (symbols & self.watchlist):
                continue

            headline = msg.get("headline", "")
            summary = msg.get("summary", "")
            if not passes_keyword_filter(headline, summary):
                logger.debug("Filtered out: %s", headline[:80])
                continue

            try:
                await self.on_news(msg)
            except Exception:
                logger.exception("on_news callback failed for: %s", headline[:80])

    async def run(self) -> None:
        """Run the WebSocket loop with exponential-backoff reconnect."""
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    ALPACA_NEWS_WS,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    # First message after connect is {"T":"success","msg":"connected"}
                    await ws.recv()
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    backoff = 1.0  # reset after stable connection

                    async for raw in ws:
                        await self._process_message(raw)

            except ConnectionClosed as e:
                logger.warning("News WS closed (%s), reconnecting in %.1fs", e, backoff)
            except Exception:
                logger.exception("News WS error, reconnecting in %.1fs", backoff)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._running = False
