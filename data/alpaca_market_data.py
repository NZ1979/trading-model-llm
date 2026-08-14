"""Real-time market data from Alpaca: 1-min bars, trades, quotes, status.

Two feeds available, selected via the `feed` parameter:
- "sip" (default): wss://stream.data.alpaca.markets/v2/sip
    Full SIP coverage. Requires Algo Trader Plus subscription ($99/mo).
    Accurate pre-market volume, full RTH/extended-hours coverage.
- "iex": wss://stream.data.alpaca.markets/v2/iex
    Free with any account. IEX-routed trades only. Pre-market volume is
    understated (~5-15% of true). Use only for development/testing.

Bars arrive seconds after each minute close. Timestamp is the START of the
1-min window in UTC, which matches our shared MinuteBar convention.

ONE CONNECTION PER ACCOUNT
--------------------------
Alpaca allows a single concurrent market-data WebSocket per account on all
individual plans. That is why this is one class handling bars, trades, and
quotes rather than three classes: a separate AlpacaTradeStream running
beside this one would fight for the same connection and produce a reconnect
loop that reads as a network fault. If you genuinely need a second
concurrent connection, use a different account's keys.

Pass `lock_path` to enforce that at process level. It is None by default so
`main.py`'s existing bar-only usage is unaffected; the microstructure daemon
passes it.

Subscription shape:
    {"action":"subscribe","bars":[...],"trades":[...],"quotes":[...],
     "statuses":[...]}

Message rate warning: trades and quotes on 503 S&P names is orders of
magnitude more traffic than bars. Use `tick_symbols` to subscribe ticks on a
small subset while bars stay on the full watchlist over the same socket.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed

from data.bar_types import MinuteBar
from data.tick_types import Quote, Trade, TradingStatus

logger = logging.getLogger(__name__)

ALPACA_BARS_WS_SIP = "wss://stream.data.alpaca.markets/v2/sip"
ALPACA_BARS_WS_IEX = "wss://stream.data.alpaca.markets/v2/iex"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Memo for the whole-second portion of RFC-3339 timestamps. The seconds part
# changes once per second while trade messages can arrive thousands of times
# per second, so parsing it once per second instead of once per message is a
# meaningful saving on the hot path. Bounded so a long session cannot grow it
# without limit.
_SECONDS_MEMO: dict[str, int] = {}
_SECONDS_MEMO_MAX = 4096


def parse_rfc3339_ns(ts: str) -> int:
    """Parse an Alpaca RFC-3339 timestamp to integer nanoseconds since epoch.

    Handles 0-9 fractional digits.

    Why not `datetime.fromisoformat`: on Python 3.11+ it ACCEPTS 9 fractional
    digits but silently truncates to microseconds. Two prints 300ns apart then
    compare EQUAL, which corrupts print ordering — and print ordering is the
    input to the Lee-Ready tick test. The failure would be silent and would
    only bite on the busiest names. Verified on CPython 3.11.15, 2026-08-14:
        fromisoformat("...44.208123456+00:00") == fromisoformat("...44.208123999+00:00")
        -> True

    Raises ValueError on anything unparseable — callers must not swallow it
    silently (Rule 18).
    """
    if ts.endswith("Z"):
        ts = ts[:-1]
    head, _, frac = ts.partition(".")

    secs = _SECONDS_MEMO.get(head)
    if secs is None:
        dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        secs = int((dt - _EPOCH).total_seconds())
        if len(_SECONDS_MEMO) >= _SECONDS_MEMO_MAX:
            _SECONDS_MEMO.clear()
        _SECONDS_MEMO[head] = secs

    if frac:
        # Right-pad to nanosecond width; truncate anything beyond 9 digits.
        frac_ns = int((frac + "000000000")[:9])
    else:
        frac_ns = 0
    return secs * 1_000_000_000 + frac_ns


class SingleConnectionLock:
    """Process-level guard against opening a second Alpaca connection.

    Deliberately does NOT try to detect stale locks by probing the recorded
    PID. Cross-platform liveness checks are unreliable, and a wrong guess
    either kills a healthy daemon or silently permits the double-connect this
    exists to prevent. Instead it fails loud and names the file and PID so a
    human can decide (Rule 18).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    holder = f.read().strip()
            except OSError:
                holder = "<unreadable>"
            raise RuntimeError(
                f"Alpaca stream lock already held: {self._path} (contents: "
                f"{holder!r}). Alpaca permits ONE market-data connection per "
                f"account; starting a second produces a reconnect loop that "
                f"looks like a network fault. If no daemon is running, delete "
                f"the lock file and retry."
            ) from None
        os.write(fd, f"pid={os.getpid()} started={time.time():.0f}".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def __enter__(self) -> SingleConnectionLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class AlpacaMarketStream:
    """Market-data WebSocket with auto-reconnect.

    Handles bars (`b`), trades (`t`), quotes (`q`), and trading status (`s`)
    over a single connection. Callbacks are optional; a channel is only
    subscribed when its callback is supplied, so bar-only usage costs nothing
    extra.

    Usage
    -----
        async def handle_bar(bar: MinuteBar) -> None: ...
        async def handle_trade(t: Trade) -> None: ...

        stream = AlpacaMarketStream(
            key, secret, watchlist, handle_bar, feed="sip",
            on_trade=handle_trade, tick_symbols={"SNDK", "MU"},
            lock_path=r"C:\\trading\\LLM model\\data\\ticks\\alpaca.lock",
        )
        await stream.run()

    The callbacks run ON the read loop. Keep them to an enqueue-and-return:
    Alpaca drops slow consumers, and a blocking computation on a fast tape
    will disconnect you (spec v4 §10).
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: set[str],
        on_bar: Callable[[MinuteBar], Awaitable[None]],
        feed: str = "sip",
        *,
        on_trade: Callable[[Trade], Awaitable[None]] | None = None,
        on_quote: Callable[[Quote], Awaitable[None]] | None = None,
        on_status: Callable[[TradingStatus], Awaitable[None]] | None = None,
        tick_symbols: set[str] | None = None,
        lock_path: str | None = None,
    ) -> None:
        if feed not in ("sip", "iex"):
            raise ValueError(f"feed must be 'sip' or 'iex', got {feed!r}")
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = {s.upper() for s in symbols}
        self._on_bar = on_bar
        self._on_trade = on_trade
        self._on_quote = on_quote
        self._on_status = on_status

        # Ticks default to the full symbol set only if the caller asked for
        # tick callbacks without narrowing. Warn loudly, because trades+quotes
        # on a 500-name watchlist is a different order of traffic than bars.
        if tick_symbols is not None:
            self._tick_symbols = {s.upper() for s in tick_symbols}
            unknown = self._tick_symbols - self._symbols
            if unknown:
                logger.warning(
                    "tick_symbols not in the bar watchlist, subscribing them "
                    "anyway: %s", sorted(unknown),
                )
                self._symbols |= unknown
        else:
            self._tick_symbols = set(self._symbols)
            if (on_trade or on_quote) and len(self._tick_symbols) > 25:
                logger.warning(
                    "Tick callbacks registered for %d symbols with no "
                    "tick_symbols narrowing. Trades+quotes at this breadth is "
                    "a very high message rate; measure before scaling.",
                    len(self._tick_symbols),
                )

        # Diagnostic throughput counters (added 2026-05-12 to investigate
        # silent bar-flow failure). Periodic log every 60s shows messages
        # and bars received, distinguishing "WS dead" from "WS alive but
        # no bars" from "bars flowing but downstream silent".
        self._msg_count_window = 0
        self._bar_count_window = 0
        self._trade_count_window = 0
        self._quote_count_window = 0
        self._parse_fail_window = 0
        self._window_start_monotonic = 0.0

        self._feed = feed
        self._url = ALPACA_BARS_WS_SIP if feed == "sip" else ALPACA_BARS_WS_IEX
        self._running = False
        self._lock = SingleConnectionLock(lock_path) if lock_path else None

    # ---------------------------------------------------------------- setup

    async def _authenticate(self, ws) -> None:
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
                f"Alpaca auth failed on {self._feed} feed. "
                f"If using 'sip', verify Algo Trader Plus is active on your "
                f"account. Response: {msg}"
            )
        logger.info("Alpaca market data WS authenticated (%s feed)", self._feed)

    async def _subscribe(self, ws) -> None:
        sub: dict[str, object] = {
            "action": "subscribe",
            "bars": sorted(self._symbols),
        }
        ticks = sorted(self._tick_symbols)
        if self._on_trade:
            sub["trades"] = ticks
        if self._on_quote:
            sub["quotes"] = ticks
        if self._on_status:
            sub["statuses"] = ticks

        await ws.send(json.dumps(sub))
        ack = await ws.recv()
        logger.info(
            "Alpaca subscribed: %d bars, %d trades, %d quotes, %d statuses. "
            "Ack[:200]: %s",
            len(self._symbols),
            len(ticks) if self._on_trade else 0,
            len(ticks) if self._on_quote else 0,
            len(ticks) if self._on_status else 0,
            str(ack)[:200],
        )

    # -------------------------------------------------------------- parsing

    def _parse_bar(self, msg: dict) -> MinuteBar:
        # T="b", S=symbol, t=ISO timestamp (start of minute),
        # o, h, l, c, v, n (trade count), vw (vwap)
        ts = datetime.fromisoformat(msg["t"].replace("Z", "+00:00"))
        return MinuteBar(
            symbol=msg["S"],
            timestamp=ts,
            open=float(msg["o"]),
            high=float(msg["h"]),
            low=float(msg["l"]),
            close=float(msg["c"]),
            volume=int(msg["v"]),
            vwap=float(msg["vw"]) if msg.get("vw") is not None else None,
        )

    def _parse_trade(self, msg: dict) -> Trade:
        # T="t", S, i=trade id, x=exchange, p=price, s=size,
        # c=[conditions], t=timestamp, z=tape
        return Trade(
            symbol=msg["S"],
            ts_ns=parse_rfc3339_ns(msg["t"]),
            price=float(msg["p"]),
            size=int(msg["s"]),
            exchange=msg.get("x", ""),
            conditions=tuple(msg.get("c") or ()),
            tape=msg.get("z", ""),
            trade_id=int(msg["i"]) if msg.get("i") is not None else None,
        )

    def _parse_quote(self, msg: dict) -> Quote:
        # T="q", S, bx/bp/bs, ax/ap/as, c=[conditions], t, z
        return Quote(
            symbol=msg["S"],
            ts_ns=parse_rfc3339_ns(msg["t"]),
            bid_price=float(msg["bp"]),
            bid_size=int(msg["bs"]),
            bid_exchange=msg.get("bx", ""),
            ask_price=float(msg["ap"]),
            ask_size=int(msg["as"]),
            ask_exchange=msg.get("ax", ""),
            conditions=tuple(msg.get("c") or ()),
            tape=msg.get("z", ""),
        )

    def _parse_status(self, msg: dict) -> TradingStatus:
        # T="s", S, sc, sm, rc, rm, t, z
        return TradingStatus(
            symbol=msg["S"],
            ts_ns=parse_rfc3339_ns(msg["t"]),
            status_code=msg.get("sc", ""),
            status_message=msg.get("sm", ""),
            reason_code=msg.get("rc", ""),
            reason_message=msg.get("rm", ""),
            tape=msg.get("z", ""),
        )

    # ------------------------------------------------------------ dispatch

    def _heartbeat(self) -> None:
        now = time.monotonic()
        if self._window_start_monotonic == 0.0:
            self._window_start_monotonic = now
            return
        if now - self._window_start_monotonic < 60.0:
            return
        logger.info(
            "Alpaca WS throughput: last %.1fs -> %d msgs, %d bars, "
            "%d trades, %d quotes, %d parse failures",
            now - self._window_start_monotonic,
            self._msg_count_window,
            self._bar_count_window,
            self._trade_count_window,
            self._quote_count_window,
            self._parse_fail_window,
        )
        if self._parse_fail_window:
            logger.warning(
                "Alpaca WS: %d messages failed to parse in the last window. "
                "This is data loss, not noise — investigate.",
                self._parse_fail_window,
            )
        self._msg_count_window = 0
        self._bar_count_window = 0
        self._trade_count_window = 0
        self._quote_count_window = 0
        self._parse_fail_window = 0
        self._window_start_monotonic = now

    async def _process_message(self, raw: str | bytes) -> None:
        self._heartbeat()
        self._msg_count_window += 1

        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON Alpaca message: %r", raw[:200])
            self._parse_fail_window += 1
            return

        for msg in messages:
            kind = msg.get("T")

            if kind == "b":
                self._bar_count_window += 1
                await self._emit(self._parse_bar, self._on_bar, msg, "bar")
            elif kind == "t" and self._on_trade:
                self._trade_count_window += 1
                await self._emit(self._parse_trade, self._on_trade, msg, "trade")
            elif kind == "q" and self._on_quote:
                self._quote_count_window += 1
                await self._emit(self._parse_quote, self._on_quote, msg, "quote")
            elif kind == "s" and self._on_status:
                await self._emit(
                    self._parse_status, self._on_status, msg, "status"
                )
            elif kind == "error":
                # Surface Alpaca-side errors loudly rather than letting them
                # scroll past as unhandled message types (Rule 18).
                logger.error("Alpaca WS error message: %s", msg)

    async def _emit(self, parser, callback, msg: dict, label: str) -> None:
        try:
            obj = parser(msg)
        except (KeyError, ValueError, TypeError):
            logger.exception("Malformed Alpaca %s: %s", label, msg)
            self._parse_fail_window += 1
            return
        try:
            await callback(obj)
        except Exception:
            logger.exception(
                "on_%s callback failed for %s", label, msg.get("S", "?")
            )

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Run the WebSocket loop with exponential-backoff reconnect."""
        if self._lock:
            self._lock.acquire()
        self._running = True
        backoff = 1.0
        try:
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
                    logger.warning("Alpaca WS closed (%s), retry in %.1fs",
                                   e, backoff)
                except Exception:
                    logger.exception("Alpaca WS error, retry in %.1fs", backoff)

                if self._running:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            if self._lock:
                self._lock.release()

    def stop(self) -> None:
        self._running = False


# Backward-compatible alias. `main.py` imports AlpacaBarStream and constructs
# it with (api_key, api_secret, symbols, on_bar, feed) — that call signature is
# unchanged, so the live signal path needs no edit.
AlpacaBarStream = AlpacaMarketStream
