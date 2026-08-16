"""Tests for the Alpaca REST client.

Covers the properties that decide whether an answer from this module can be
trusted:

  1. Every price carries its age, and staleness is computed rather than left
     for the caller to notice. A last price with no age is the single most
     misleading field this module could return.
  2. Odd lots are flagged. If the latest print is condition `I`, the
     consolidated 'last' the rest of the market sees is an OLDER print.
  3. Missing inputs yield None, never 0.0. 'no gap' and 'gap unknown' are
     different answers and a gate keyed on 0.0 conflates them.
  4. The requested feed is actually sent. A silent downgrade from sip/opra to
     the free tier is invisible in the response body.
  5. Truncation is loud. A capped chain must not read as a complete one.
  6. Credentials go in headers, never URLs, and never into an exception.

No network. httpx.MockTransport throughout - no mocking dependency, no live
keys, and the request objects are inspectable so we can assert on what was
actually sent rather than only on what came back.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from data.alpaca_rest import (
    DEFAULT_STALE_MS,
    AlpacaRESTClient,
    Bar,
    EquitySnapshot,
    OptionQuote,
    _ns,
    _require_alpaca_keys,
    parse_occ_symbol,
)


def _client(handler, **kwargs) -> AlpacaRESTClient:
    return AlpacaRESTClient(
        "test-key", "test-secret",
        transport=httpx.MockTransport(handler), **kwargs)


def _run(coro):
    return asyncio.run(coro)


SNAPSHOT_BODY = {
    "snapshots": {
        "SNDK": {
            "latestTrade": {"t": "2026-08-14T20:00:00.123456789Z",
                            "p": 1666.5, "s": 100, "x": "D", "c": ["@", "T"]},
            "latestQuote": {"t": "2026-08-14T20:00:00.500000000Z",
                            "bp": 1665.0, "bs": 12, "ap": 1667.0, "as": 15},
            "dailyBar": {"o": 1600.0, "h": 1680.0, "l": 1590.0,
                         "c": 1666.5, "v": 5_300_000, "vw": 1640.0},
            "prevDailyBar": {"c": 1580.0, "v": 4_100_000},
        }
    }
}


# ---------------------------------------------------------------- timestamps

def test_ns_preserves_nanoseconds():
    """Nanosecond integers, not datetime. Python truncates datetime to
    microseconds, collapsing prints that are nanoseconds apart."""
    got = _ns("2026-08-14T20:00:00.123456789Z")
    assert got % 1_000_000_000 == 123_456_789


def test_ns_handles_missing_and_fractionless():
    assert _ns(None) is None
    assert _ns("2026-08-14T20:00:00Z") % 1_000_000_000 == 0


# ------------------------------------------------------------------ snapshot

def test_snapshot_parses_all_blocks():
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert snap.last_price == 1666.5
    assert snap.bid == 1665.0 and snap.ask == 1667.0
    assert snap.day_volume == 5_300_000
    assert snap.prev_close == 1580.0


def test_every_price_carries_an_age():
    """FEED_SPEC_V4 §1a. A last price without an age reads as current when it
    may be hours old - which matters MORE for on-demand queries than for a
    live tape."""
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert snap.last_age_ms is not None and snap.last_age_ms > 0
    assert snap.quote_age_ms is not None and snap.quote_age_ms > 0


def test_stale_when_last_print_is_old():
    """The 2026-08-14 fetch timestamps are long past, so this snapshot must
    report itself stale rather than presenting an old price as current."""
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert snap.is_stale


def test_stale_when_no_timestamp_at_all():
    """Absent timestamp is stale, not fresh. Defaulting to fresh would make
    a missing field indistinguishable from a current price."""
    body = {"snapshots": {"X": {"latestTrade": {"p": 1.0}}}}
    snap = _run(_client(lambda r: httpx.Response(200, json=body)).snapshot("X"))
    assert snap.last_age_ms is None
    assert snap.is_stale


def test_odd_lot_last_print_is_flagged():
    """87-97% of post-market prints carry condition I. If the latest trade is
    an odd lot, the consolidated last is an older print than this one."""
    body = json.loads(json.dumps(SNAPSHOT_BODY))
    body["snapshots"]["SNDK"]["latestTrade"]["c"] = ["@", "T", "I"]
    snap = _run(_client(
        lambda r: httpx.Response(200, json=body)).snapshot("SNDK"))
    assert snap.last_is_odd_lot


def test_regular_print_is_not_flagged_as_odd_lot():
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert not snap.last_is_odd_lot


# -------------------------------------------------------------- derived math

def test_spread_and_bps():
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert snap.mid == pytest.approx(1666.0)
    assert snap.spread == pytest.approx(2.0)
    assert snap.spread_bps == pytest.approx(2.0 / 1666.0 * 10_000)


def test_gap_and_change_percent():
    snap = _run(_client(
        lambda r: httpx.Response(200, json=SNAPSHOT_BODY)).snapshot("SNDK"))
    assert snap.gap_pct == pytest.approx((1600.0 - 1580.0) / 1580.0 * 100)
    assert snap.change_pct == pytest.approx((1666.5 - 1580.0) / 1580.0 * 100)


def test_missing_prior_close_yields_none_not_zero():
    """'No gap' and 'gap unknown' are different answers. A gate keyed on 0.0
    would treat a missing prior close as a flat open."""
    body = {"snapshots": {"X": {"dailyBar": {"o": 10.0}}}}
    snap = _run(_client(lambda r: httpx.Response(200, json=body)).snapshot("X"))
    assert snap.gap_pct is None
    assert snap.change_pct is None
    assert snap.spread is None and snap.spread_bps is None and snap.mid is None


# ------------------------------------------------------------ request shape

def test_sip_feed_is_actually_requested():
    """A silent downgrade to the free IEX feed is invisible in the response
    body, and understates pre-market volume to 5-15% of true."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=SNAPSHOT_BODY)

    _run(_client(handler).snapshot("SNDK"))
    assert "feed=sip" in seen["url"]


def test_opra_feed_is_actually_requested():
    """The free options tier is 'indicative', not OPRA. Requesting it by
    accident would silently downgrade greeks and IV."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"snapshots": {}})

    _run(_client(handler).option_chain("SNDK"))
    assert "feed=opra" in seen["url"]


def test_credentials_go_in_headers_not_url():
    """Alpaca authenticates by header, which is why this module needs no
    apiKey-scrubbing of the kind polygon_feed carries."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("APCA-API-KEY-ID")
        seen["secret"] = request.headers.get("APCA-API-SECRET-KEY")
        return httpx.Response(200, json=SNAPSHOT_BODY)

    _run(_client(handler).snapshot("SNDK"))
    assert seen["key"] == "test-key" and seen["secret"] == "test-secret"
    assert "test-key" not in seen["url"]
    assert "test-secret" not in seen["url"]


def test_multi_symbol_snapshot_is_one_request():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"snapshots": {
            "A": {"latestTrade": {"p": 1.0}},
            "B": {"latestTrade": {"p": 2.0}},
        }})

    got = _run(_client(handler).snapshots(["A", "B"]))
    assert len(calls) == 1
    assert set(got) == {"A", "B"}


def test_empty_symbol_list_makes_no_request():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={})

    assert _run(_client(handler).snapshots([])) == {}
    assert not calls


# ------------------------------------------------------------------- errors

def test_403_names_entitlement_and_does_not_retry():
    """403 on Alpaca almost always means the feed is not entitled. Retrying
    an entitlement failure three times only delays the answer."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="forbidden: subscription required")

    with pytest.raises(RuntimeError, match="not entitled"):
        _run(_client(handler).snapshot("SNDK"))
    assert len(calls) == 1


def test_403_message_does_not_contain_the_secret():
    def handler(request):
        return httpx.Response(403, text="nope")

    try:
        _run(_client(handler).snapshot("SNDK"))
    except RuntimeError as exc:
        assert "test-secret" not in str(exc)
    else:
        pytest.fail("expected RuntimeError")


def test_missing_symbol_in_response_raises_clearly():
    def handler(request):
        return httpx.Response(200, json={"snapshots": {}})

    with pytest.raises(RuntimeError, match="no snapshot"):
        _run(_client(handler).snapshot("NOPE"))


def test_require_keys_raises_naming_both(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(RuntimeError) as exc:
        _require_alpaca_keys()
    assert "ALPACA_API_KEY" in str(exc.value)
    assert "ALPACA_API_SECRET" in str(exc.value)


# -------------------------------------------------------------------- bars

def test_bars_parse_and_order():
    body = {"bars": {"SNDK": [
        {"t": "2026-08-14T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5,
         "v": 100, "n": 7, "vw": 1.4},
        {"t": "2026-08-14T13:31:00Z", "o": 1.5, "h": 3, "l": 1, "c": 2.5,
         "v": 200, "n": 9, "vw": 2.1},
    ]}}
    bars = _run(_client(
        lambda r: httpx.Response(200, json=body)).bars("SNDK"))
    assert len(bars) == 2
    assert bars[0].close == 1.5 and bars[1].volume == 200
    assert bars[1].ts_ns > bars[0].ts_ns
    assert bars[0].timestamp.year == 2026


def test_bars_timeframe_is_passed_through():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"bars": {}})

    _run(_client(handler).bars("SNDK", timeframe="5Min", limit=42))
    assert "timeframe=5Min" in seen["url"]
    assert "limit=42" in seen["url"]


# ------------------------------------------------------------------ options

OPTION_BODY = {
    "snapshots": {
        "SNDK260919C01600000": {
            "latestQuote": {"t": "2026-08-14T20:00:00Z", "bp": 10.0,
                            "bs": 12, "ap": 10.4, "as": 15},
            "latestTrade": {"t": "2026-08-14T19:59:00Z", "p": 10.2, "s": 3},
            "impliedVolatility": 0.42,
            "greeks": {"delta": 0.55, "gamma": 0.004, "theta": -0.8,
                       "vega": 1.2, "rho": 0.3},
        }
    }
}


def test_option_chain_parses_greeks_and_iv():
    chain = _run(_client(
        lambda r: httpx.Response(200, json=OPTION_BODY)).option_chain("SNDK"))
    assert len(chain) == 1
    c = chain[0]
    assert c.underlying == "SNDK" and c.strike == 1600.0
    assert c.put_call == "CALL" and c.is_call
    assert c.expiration == "2026-09-19"
    assert c.implied_volatility == 0.42
    assert c.delta == 0.55
    assert c.mid == pytest.approx(10.2)
    assert c.quote_age_ms is not None


def test_option_quote_has_no_open_interest_attribute():
    """Alpaca's options market data API has no OI field. Absent rather than
    None so a caller reaching for it fails at the call site instead of
    silently treating None as zero. OI comes from Schwab."""
    chain = _run(_client(
        lambda r: httpx.Response(200, json=OPTION_BODY)).option_chain("SNDK"))
    with pytest.raises(AttributeError):
        _ = chain[0].open_interest


def test_unparseable_option_symbol_is_skipped_not_fatal():
    body = {"snapshots": {
        "GARBAGE": {"latestQuote": {"bp": 1.0}},
        "SNDK260919C01600000": OPTION_BODY["snapshots"][
            "SNDK260919C01600000"],
    }}
    chain = _run(_client(
        lambda r: httpx.Response(200, json=body)).option_chain("SNDK"))
    assert [c.symbol for c in chain] == ["SNDK260919C01600000"]


def test_chain_pagination_follows_next_token():
    pages = [
        {"snapshots": {"SNDK260919C01600000":
                       OPTION_BODY["snapshots"]["SNDK260919C01600000"]},
         "next_page_token": "tok"},
        {"snapshots": {"SNDK260919C01700000":
                       OPTION_BODY["snapshots"]["SNDK260919C01600000"]}},
    ]
    state = {"i": 0}

    def handler(request):
        body = pages[state["i"]]
        state["i"] += 1
        return httpx.Response(200, json=body)

    chain = _run(_client(handler).option_chain("SNDK"))
    assert len(chain) == 2


def test_chain_truncation_is_loud(caplog):
    """Rule 18: a capped result must not read as a complete one. Silent
    truncation is the failure this test exists to prevent."""
    def handler(request):
        return httpx.Response(200, json={
            "snapshots": {"SNDK260919C01600000":
                          OPTION_BODY["snapshots"]["SNDK260919C01600000"]},
            "next_page_token": "always-more",
        })

    with caplog.at_level(logging.ERROR):
        _run(_client(handler).option_chain("SNDK", max_pages=2))
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_chain_filters_are_passed_through():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"snapshots": {}})

    _run(_client(handler).option_chain(
        "SNDK", expiration_lte="2026-09-30", strike_gte=1500.0,
        contract_type="call"))
    url = seen["url"]
    assert "expiration_date_lte=2026-09-30" in url
    assert "strike_price_gte=1500" in url
    assert "type=call" in url


# ------------------------------------------------------------- OCC symbols

@pytest.mark.parametrize("sym,expected", [
    ("SNDK260919C01600000", ("SNDK", "2026-09-19", "CALL", 1600.0)),
    ("AAPL260116P00150000", ("AAPL", "2026-01-16", "PUT", 150.0)),
    ("SPY261218C00700500", ("SPY", "2026-12-18", "CALL", 700.5)),
])
def test_occ_symbol_parsing(sym, expected):
    assert parse_occ_symbol(sym) == expected


def test_occ_rejects_non_option_symbol():
    with pytest.raises(ValueError):
        parse_occ_symbol("SNDK")
