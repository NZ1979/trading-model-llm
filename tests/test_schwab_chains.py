"""Tests for Schwab option chain parsing (spec v4, options layer).

The failure modes that matter here are all silent-corruption ones:

  - the leaf node is a LIST in the live API but an OBJECT in the OpenAPI
    schema; handling only one silently drops contracts
  - mini and adjusted contracts have non-100 multipliers, so counting them in
    an open-interest wall overstates exposure without any error
  - Schwab returns -999.0 and NaN for greeks it cannot price; averaging those
    into a skew produces a number that looks real
  - open interest is a T+1 figure, so volume/OI on a same-day expiry is a
    division artifact, not new positioning

No network. All payloads synthetic.
"""

from __future__ import annotations

import math

import pytest

from data.schwab_chains import (
    OptionContract,
    _iter_leaf_contracts,
    _parse_expiration_key,
    parse_chain,
)


def contract(**kw) -> dict:
    d = dict(
        putCall="CALL", symbol="SNDK  260815C01660000", strikePrice=1660.0,
        expirationDate="2026-08-15T00:00:00.000+00:00", daysToExpiration=1,
        bidPrice=1.0, askPrice=1.2, lastPrice=1.1, markPrice=1.1,
        bidSize=10, askSize=12, totalVolume=500, openInterest=1000,
        volatility=45.5, delta=0.5, gamma=0.01, theta=-0.3, vega=0.2,
        rho=0.05, isInTheMoney=False, intrinsicValue=0.0, timeValue=1.1,
        multiplier=100.0, isPennyPilot=True, isMini=False,
        isNonStandard=False, optionRoot="SNDK",
    )
    d.update(kw)
    return d


def chain_payload(**kw) -> dict:
    d = dict(
        symbol="SNDK", status="SUCCESS", underlyingPrice=1666.0,
        isDelayed=False,
        callExpDateMap={"2026-08-15:1": {
            "1660.0": [contract()],
            "1670.0": [contract(strikePrice=1670.0, openInterest=2500)],
        }},
        putExpDateMap={"2026-08-15:1": {
            "1650.0": [contract(putCall="PUT", strikePrice=1650.0,
                                openInterest=3000, delta=-0.4)],
        }},
    )
    d.update(kw)
    return d


# ------------------------------------------------------------ leaf shapes

def test_expiration_key_splits_date_and_dte():
    assert _parse_expiration_key("2026-08-15:1") == ("2026-08-15", 1)


def test_expiration_key_tolerates_missing_dte():
    assert _parse_expiration_key("2026-08-15") == ("2026-08-15", -1)


@pytest.mark.parametrize("node,expected", [
    ([contract()], 1),                              # live API shape
    (contract(), 1),                                # single object
    ({"a": contract(), "b": contract()}, 2),        # OpenAPI schema shape
    ({"a": [contract(), contract()]}, 2),           # nested list
])
def test_leaf_shapes_all_yield_contracts(node, expected):
    """Guessing one shape and being wrong drops contracts with no error."""
    assert len(list(_iter_leaf_contracts(node))) == expected


# ---------------------------------------------------------------- parsing

def test_parses_calls_and_puts():
    chain = parse_chain(chain_payload())
    assert len(chain.contracts) == 3
    assert len(chain.calls()) == 2
    assert len(chain.puts()) == 1
    assert chain.underlying_price == 1666.0


def test_expiration_is_trimmed_to_a_date():
    chain = parse_chain(chain_payload())
    assert chain.calls()[0].expiration == "2026-08-15"


def test_strike_falls_back_to_the_map_key():
    payload = chain_payload(callExpDateMap={"2026-08-15:1": {
        "1680.0": [contract(strikePrice=None)]}}, putExpDateMap={})
    assert parse_chain(payload).contracts[0].strike == 1680.0


# ------------------------------------------------- silent-corruption guards

def test_mini_and_nonstandard_excluded_by_default():
    """Mini options represent 10 shares, not 100. Counting them in an OI wall
    overstates exposure by 10x with no error raised."""
    payload = chain_payload(callExpDateMap={"2026-08-15:1": {"10.0": [
        contract(isMini=True, multiplier=10.0),
        contract(isNonStandard=True),
        contract(),
    ]}}, putExpDateMap={})
    assert len(parse_chain(payload).contracts) == 1


def test_mini_and_nonstandard_included_on_request():
    payload = chain_payload(callExpDateMap={"2026-08-15:1": {"10.0": [
        contract(isMini=True, multiplier=10.0),
        contract(isNonStandard=True),
        contract(),
    ]}}, putExpDateMap={})
    chain = parse_chain(payload, include_mini=True, include_non_standard=True)
    assert len(chain.contracts) == 3


@pytest.mark.parametrize("field,value", [
    ("delta", -999.0),          # Schwab's "cannot price" sentinel
    ("volatility", float("nan")),
    ("gamma", None),
    ("theta", "not-a-number"),
])
def test_unpriceable_greeks_become_none_not_numbers(field, value):
    payload = chain_payload(
        callExpDateMap={"2026-08-15:1": {"10.0": [contract(**{field: value})]}},
        putExpDateMap={})
    got = getattr(parse_chain(payload).contracts[0], field)
    assert got is None, f"{field}={value!r} leaked through as {got!r}"


def test_zero_open_interest_gives_none_ratio_not_zero():
    """None and 0.0 mean different things: 'no basis to compare' vs 'nothing
    traded'. Collapsing them hides the 0DTE artifact."""
    payload = chain_payload(
        callExpDateMap={"2026-08-15:1": {"10.0": [contract(openInterest=0)]}},
        putExpDateMap={})
    assert parse_chain(payload).contracts[0].volume_oi_ratio is None


def test_volume_oi_ratio_and_notional():
    c = parse_chain(chain_payload()).calls()[0]
    assert c.volume_oi_ratio == pytest.approx(0.5)
    assert c.notional_oi_shares == 100_000.0


def test_notional_uses_the_contract_multiplier():
    payload = chain_payload(callExpDateMap={"2026-08-15:1": {"10.0": [
        contract(isMini=True, multiplier=10.0, openInterest=1000)]}},
        putExpDateMap={})
    c = parse_chain(payload, include_mini=True).contracts[0]
    assert c.notional_oi_shares == 10_000.0


# ------------------------------------------------------------- robustness

@pytest.mark.parametrize("payload", [
    {},
    {"callExpDateMap": None, "putExpDateMap": None},
    {"symbol": "X", "callExpDateMap": {"e:1": {"10.0": "junk"}}},
    {"symbol": "X", "callExpDateMap": {"e:1": None}},
    {"symbol": "X", "callExpDateMap": "not-a-dict"},
])
def test_malformed_payloads_never_raise(payload):
    assert parse_chain(payload).contracts == []


def test_delayed_flag_is_surfaced():
    assert parse_chain({"isDelayed": True}).is_delayed is True


def test_delayed_falls_back_to_underlying_block():
    chain = parse_chain({"underlying": {"delayed": True, "symbol": "X"}})
    assert chain.is_delayed is True


def test_expirations_are_sorted_and_deduped():
    payload = chain_payload(callExpDateMap={
        "2026-09-19:36": {"10.0": [contract(
            expirationDate="2026-09-19T00:00:00.000+00:00")]},
        "2026-08-15:1": {"10.0": [contract()]},
    }, putExpDateMap={})
    assert parse_chain(payload).expirations() == ["2026-08-15", "2026-09-19"]
