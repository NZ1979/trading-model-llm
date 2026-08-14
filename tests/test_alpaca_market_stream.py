"""Tests for the Alpaca market-data stream extension (spec v4 phase 1).

Covers the three things most likely to break silently:
  1. Nanosecond timestamp fidelity — the stdlib truncates to microseconds and
     would collapse distinct prints, corrupting Lee-Ready tick-test ordering.
  2. Fail-loud dispatch — a malformed message must be counted and skipped, not
     swallowed, and must not kill the read loop (Rule 18).
  3. Backward compatibility — main.py constructs AlpacaBarStream with the old
     signature and must keep working untouched.

No network. Everything here runs against synthetic payloads.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from data.alpaca_market_data import (
    AlpacaBarStream,
    AlpacaMarketStream,
    SingleConnectionLock,
    parse_rfc3339_ns,
)
from data.tick_types import Quote, Trade, TradingStatus

BASE = "2026-08-14T19:51:44"
BASE_S = 1786737104  # verified against datetime.timestamp() and calendar.timegm()


async def _noop(_obj) -> None:
    return None


# --------------------------------------------------------------- timestamps

@pytest.mark.parametrize("suffix,expected_ns", [
    ("Z", 0),
    (".208Z", 208_000_000),
    (".208123Z", 208_123_000),
    (".208123456Z", 208_123_456),
])
def test_parse_rfc3339_ns_fractional_widths(suffix, expected_ns):
    assert parse_rfc3339_ns(BASE + suffix) == BASE_S * 10**9 + expected_ns


def test_parser_preserves_nanoseconds_stdlib_would_lose():
    """The reason parse_rfc3339_ns exists at all.

    datetime.fromisoformat accepts 9 fractional digits on 3.11+ but truncates
    to microseconds, making two prints 543ns apart compare equal. Sorting a
    tape on that would silently reorder prints.
    """
    a = BASE + ".208123456+00:00"
    b = BASE + ".208123999+00:00"
    assert datetime.fromisoformat(a) == datetime.fromisoformat(b)
    assert parse_rfc3339_ns(a.replace("+00:00", "Z")) != parse_rfc3339_ns(
        b.replace("+00:00", "Z")
    )


def test_parse_rfc3339_ns_is_strictly_ordered():
    a = parse_rfc3339_ns(BASE + ".208123456Z")
    b = parse_rfc3339_ns(BASE + ".208123457Z")
    assert b - a == 1


def test_parse_rfc3339_ns_rejects_garbage():
    with pytest.raises(ValueError):
        parse_rfc3339_ns("not-a-timestamp")


# ------------------------------------------------------------ message parse

@pytest.fixture()
def stream() -> AlpacaMarketStream:
    return AlpacaMarketStream("k", "s", {"SNDK"}, on_bar=_noop, feed="sip")


def test_parse_trade(stream):
    t = stream._parse_trade({
        "T": "t", "S": "SNDK", "i": 9876, "x": "V", "p": 1614.25, "s": 760,
        "c": ["@", "F"], "t": BASE + ".208123456Z", "z": "C",
    })
    assert isinstance(t, Trade)
    assert (t.price, t.size, t.trade_id) == (1614.25, 760, 9876)
    assert t.conditions == ("@", "F")
    assert t.ts_ns == BASE_S * 10**9 + 208_123_456


def test_parse_trade_without_id(stream):
    t = stream._parse_trade({
        "T": "t", "S": "X", "x": "V", "p": 1.0, "s": 1, "c": [],
        "t": BASE + "Z", "z": "C",
    })
    assert t.trade_id is None


def test_parse_trade_missing_price_raises(stream):
    """Fail loud: a print with no price is data loss, not a defaultable field."""
    with pytest.raises(KeyError):
        stream._parse_trade({"T": "t", "S": "X", "x": "V", "s": 1,
                             "t": BASE + "Z"})


def test_parse_quote(stream):
    q = stream._parse_quote({
        "T": "q", "S": "SNDK", "bx": "V", "bp": 1613.90, "bs": 300,
        "ax": "Q", "ap": 1614.10, "as": 500, "c": ["R"],
        "t": BASE + ".3Z", "z": "C",
    })
    assert isinstance(q, Quote)
    assert q.bid_size == 300 and q.ask_size == 500
    assert q.spread == pytest.approx(0.20)
    assert q.midpoint == pytest.approx(1614.00)


@pytest.mark.parametrize("code,is_halt", [
    ("H", True), ("T", False), ("Q", False), ("R", False), ("ZZZ", True),
])
def test_status_halt_detection_is_conservative(stream, code, is_halt):
    """Unknown codes must read as halted so metrics suppress rather than
    silently compute across a gap."""
    st = stream._parse_status({
        "T": "s", "S": "SNDK", "sc": code, "sm": "", "rc": "", "rm": "",
        "t": BASE + "Z", "z": "C",
    })
    assert isinstance(st, TradingStatus)
    assert st.is_halt is is_halt


# ---------------------------------------------------------------- dispatch

def test_malformed_message_is_counted_and_skipped_without_killing_loop():
    seen = {"bar": 0, "trade": 0, "quote": 0}

    async def ob(_): seen["bar"] += 1
    async def ot(_): seen["trade"] += 1
    async def oq(_): seen["quote"] += 1

    s = AlpacaMarketStream("k", "s", {"SNDK"}, on_bar=ob, on_trade=ot,
                           on_quote=oq, tick_symbols={"SNDK"})
    payload = json.dumps([
        {"T": "t", "S": "SNDK", "i": 1, "x": "V", "p": 10.0, "s": 5, "c": [],
         "t": BASE + ".1Z", "z": "C"},
        {"T": "t", "S": "SNDK", "x": "V", "s": 5, "t": BASE + ".2Z"},  # bad
        {"T": "q", "S": "SNDK", "bx": "V", "bp": 9.9, "bs": 1, "ax": "Q",
         "ap": 10.1, "as": 1, "c": [], "t": BASE + ".3Z", "z": "C"},
        {"T": "b", "S": "SNDK", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100,
         "vw": 1.2, "t": "2026-08-14T19:51:00Z"},
        {"T": "error", "code": 400, "msg": "boom"},
    ])
    asyncio.run(s._process_message(payload))

    assert seen == {"bar": 1, "trade": 1, "quote": 1}
    assert s._parse_fail_window == 1


def test_raising_callback_does_not_propagate():
    async def boom(_):
        raise RuntimeError("callback exploded")

    s = AlpacaMarketStream("k", "s", {"A"}, on_bar=boom, tick_symbols=set())
    asyncio.run(s._process_message(json.dumps([
        {"T": "b", "S": "A", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "vw": 1,
         "t": "2026-08-14T19:51:00Z"},
    ])))


def test_non_json_message_counted_not_raised():
    s = AlpacaMarketStream("k", "s", {"A"}, on_bar=_noop)
    asyncio.run(s._process_message("<html>gateway error</html>"))
    assert s._parse_fail_window == 1


# ------------------------------------------------------------ subscriptions

class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, m: str) -> None:
        self.sent.append(json.loads(m))

    async def recv(self) -> str:
        return "[]"


def test_bar_only_subscription_omits_tick_channels():
    s = AlpacaMarketStream("k", "s", {"A", "B"}, on_bar=_noop)
    ws = _FakeWS()
    asyncio.run(s._subscribe(ws))
    assert set(ws.sent[0]) == {"action", "bars"}


def test_tick_symbols_narrow_ticks_but_not_bars():
    """Bars stay on the full watchlist for main.py; ticks stay narrow so the
    shared connection is not flooded."""
    s = AlpacaMarketStream("k", "s", {"A", "B", "C"}, on_bar=_noop,
                           on_trade=_noop, tick_symbols={"A"})
    ws = _FakeWS()
    asyncio.run(s._subscribe(ws))
    assert ws.sent[0]["bars"] == ["A", "B", "C"]
    assert ws.sent[0]["trades"] == ["A"]
    assert "quotes" not in ws.sent[0]


def test_tick_symbol_outside_watchlist_is_added_to_bars():
    s = AlpacaMarketStream("k", "s", {"A"}, on_bar=_noop, on_trade=_noop,
                           tick_symbols={"B"})
    ws = _FakeWS()
    asyncio.run(s._subscribe(ws))
    assert ws.sent[0]["bars"] == ["A", "B"]


# ------------------------------------------------------------------- lock

def test_lock_refuses_second_holder_and_names_the_file(tmp_path):
    lp = str(tmp_path / "sub" / "alpaca.lock")
    first = SingleConnectionLock(lp)
    first.acquire()
    assert os.path.exists(lp)

    with pytest.raises(RuntimeError) as exc:
        SingleConnectionLock(lp).acquire()
    assert lp in str(exc.value)

    first.release()
    assert not os.path.exists(lp)


def test_lock_is_reusable_after_release(tmp_path):
    lp = str(tmp_path / "alpaca.lock")
    with SingleConnectionLock(lp):
        assert os.path.exists(lp)
    assert not os.path.exists(lp)
    with SingleConnectionLock(lp):
        assert os.path.exists(lp)


# --------------------------------------------------- backward compatibility

def test_alias_is_the_same_class():
    assert AlpacaBarStream is AlpacaMarketStream


def test_legacy_call_signature_from_main_py_still_works():
    """main.py line ~482 constructs with exactly these kwargs. If this test
    fails, the live signal path is broken."""
    s = AlpacaBarStream(
        api_key="k", api_secret="s", symbols={"AAPL"},
        on_bar=_noop, feed="iex",
    )
    assert s._url.endswith("/iex")
    assert s._lock is None  # legacy path must not acquire a lock


def test_invalid_feed_still_rejected():
    with pytest.raises(ValueError):
        AlpacaMarketStream("k", "s", set(), _noop, feed="nope")
