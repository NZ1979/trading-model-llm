"""Tests for the Schwab /quotes parser.

Built from the real SNDK response captured 2026-08-17 06:51 ET, which is the
fixture below verbatim. Properties asserted:

  1. Real-time is confirmed from BOTH `realtime` and `quoteType`, never one.
  2. Schwab's 0.0 sentinel on price fields becomes None, not zero. An equity
     never trades at zero, and `openPrice: 0.0` before the open means "not
     yet".
  3. Every timestamped field carries an age.
  4. The volume ratio is NOT called rvol and is documented as a fraction of a
     full day - it compares a partial session against a full-session average.
  5. Missing sub-blocks degrade to None rather than raising. A missing PE
     ratio must not cost you the price.
  6. A non-real-time quote is logged loudly, because a silent downgrade to
     delayed data is invisible in the numbers.

No network.
"""

from __future__ import annotations

import logging

import pytest

from data.schwab_quotes import (
    EquityQuote,
    fetch_quote,
    fetch_quotes,
    parse_quote,
)

# Captured live from Schwab, 2026-08-17 06:51:55 ET. Trimmed to the fields
# the parser reads; values are unaltered.
SNDK_BLOCK = {
    "assetMainType": "EQUITY",
    "assetSubType": "COE",
    "quoteType": "NBBO",
    "realtime": True,
    "symbol": "SNDK",
    "extended": {"lastPrice": 1740.44, "lastSize": 6,
                 "tradeTime": 1786953597000},
    "fundamental": {"avg10DaysVolume": 19702637.0,
                    "avg1YearVolume": 10301133.0,
                    "eps": 73.76, "peRatio": 19.65,
                    "sharesOutstanding": 149000000,
                    "lastEarningsDate": "2026-08-05T00:00:00Z",
                    "divYield": 0.0},
    "quote": {
        "52WeekHigh": 2354.39, "52WeekLow": 42.82,
        "askMICId": "XNAS", "askPrice": 1734.0, "askSize": 80,
        "bidMICId": "ARCX", "bidPrice": 1732.0, "bidSize": 40,
        "closePrice": 1641.11,
        "highPrice": 0.0, "lowPrice": 0.0, "openPrice": 0.0,
        "lastMICId": "XADF", "lastPrice": 1733.0, "lastSize": 1,
        "mark": 1732.0, "netChange": 91.89,
        "netPercentChange": 5.59925904,
        "postMarketChange": 91.89,
        "postMarketPercentChange": 5.59925904,
        "quoteTime": 1786963915234, "tradeTime": 1786963914261,
        "securityStatus": "Normal", "totalVolume": 521010,
    },
    "reference": {"description": "SANDISK CORP", "exchange": "Q",
                  "exchangeName": "Nasdaq", "isHardToBorrow": False,
                  "isShortable": True, "htbQuantity": 5268067,
                  "optionable": True},
    "regular": {"regularMarketLastPrice": 1641.11,
                "regularMarketLastSize": 534067},
}


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text

    def json(self):
        return self._p


class _Client:
    def __init__(self, payload, status=200, text=""):
        self._r = _Resp(payload, status, text)
        self.seen = None

    def get_quotes(self, symbols):
        self.seen = symbols
        return self._r


def _q(**over) -> EquityQuote:
    block = {**SNDK_BLOCK}
    block.update(over)
    return parse_quote("SNDK", block)


# ------------------------------------------------------------ real-time flag

def test_realtime_requires_both_flag_and_nbbo():
    q = _q()
    assert q.realtime is True and q.quote_type == "NBBO"
    assert q.is_realtime is True


def test_nfl_quote_type_is_not_realtime():
    """`realtime` is an account-level assertion, `quoteType` is per-response.
    Requiring both means a downgrade on either surfaces."""
    assert _q(quoteType="NFL").is_realtime is False


def test_realtime_false_is_not_realtime():
    assert _q(realtime=False).is_realtime is False


def test_non_realtime_quote_is_logged_loudly(caplog):
    """A silent downgrade to delayed data is invisible in the numbers, and
    real-time is the entire reason this source is preferred."""
    block = {**SNDK_BLOCK, "quoteType": "NFL"}
    with caplog.at_level(logging.WARNING):
        fetch_quotes(_Client({"SNDK": block}), ["SNDK"])
    assert any("NOT real-time" in r.message for r in caplog.records)


# ---------------------------------------------------------- zero sentinels

def test_zero_prices_become_none_not_zero():
    """openPrice/highPrice/lowPrice are 0.0 before the open. An equity never
    trades at zero, and a percentage computed against 0.0 yields infinity or
    a division error - both of which read as data rather than absence."""
    q = _q()
    assert q.open_price is None
    assert q.high_price is None
    assert q.low_price is None
    assert q.close_price == 1641.11   # a real value survives


def test_real_prices_are_preserved():
    q = _q()
    assert q.last_price == 1733.0
    assert q.bid == 1732.0 and q.ask == 1734.0
    assert q.week52_high == 2354.39 and q.week52_low == 42.82


# ------------------------------------------------------------------- ages

def test_timestamps_carry_ages():
    q = _q()
    assert q.quote_age_ms is not None and q.quote_age_ms > 0
    assert q.trade_age_ms is not None and q.trade_age_ms > 0


def test_zero_timestamp_is_absent_not_epoch():
    """Schwab writes 0 for 'no value'. Treating it as 1970 would report an
    age of fifty-six years and a stale flag for a field that simply is not
    populated."""
    block = {**SNDK_BLOCK, "quote": {**SNDK_BLOCK["quote"], "quoteTime": 0}}
    q = parse_quote("SNDK", block)
    assert q.quote_ts_ns is None
    assert q.quote_age_ms is None
    assert q.is_stale is True   # unknown age is stale, not fresh


def test_stale_threshold_applies():
    q = _q()
    assert q.is_stale is True   # the fixture's timestamps are long past


# ------------------------------------------------------- the volume ratio

def test_volume_ratio_is_a_fraction_of_a_full_day():
    """NOT rvol. 521,010 against a 19.7M full-day average is 0.0264, which
    says nothing about whether that is unusual for 06:51. Real RVOL needs a
    time-of-day baseline, which Schwab cannot supply."""
    q = _q()
    assert q.volume_vs_avg_full_day == pytest.approx(521010 / 19702637)
    assert q.volume_vs_avg_full_day == pytest.approx(0.0264, abs=0.0005)


def test_there_is_no_rvol_attribute():
    """Naming this rvol would put a number that looks like a gate input in
    front of a caller who has one. It is not that number."""
    with pytest.raises(AttributeError):
        _ = _q().rvol


def test_volume_ratio_none_without_baseline():
    block = {**SNDK_BLOCK, "fundamental": {}}
    assert parse_quote("SNDK", block).volume_vs_avg_full_day is None


# ------------------------------------------------------ the pre-market win

def test_change_from_close_is_correct_premarket():
    """The figure alpaca_rest must suppress. Schwab carries closePrice as its
    own field rather than inferring it from a daily bar that has not rolled,
    so +5.60% is right where Alpaca could only offer a two-session span."""
    q = _q()
    assert q.change_from_close_pct == pytest.approx(5.599, abs=0.001)
    assert q.close_price == 1641.11
    assert q.post_market_percent_change == pytest.approx(5.599, abs=0.001)


def test_change_falls_back_to_computation():
    block = {**SNDK_BLOCK,
             "quote": {k: v for k, v in SNDK_BLOCK["quote"].items()
                       if k != "netPercentChange"}}
    q = parse_quote("SNDK", block)
    assert q.change_from_close_pct == pytest.approx(
        (1733.0 - 1641.11) / 1641.11 * 100, abs=0.001)


def test_todays_volume_is_present():
    """The number alpaca_rest cannot supply pre-market, because its dailyBar
    is still the previous session's."""
    assert _q().total_volume == 521010


# ------------------------------------------------------------- resilience

def test_missing_fundamental_block_does_not_lose_the_price():
    block = {**SNDK_BLOCK}
    del block["fundamental"]
    q = parse_quote("SNDK", block)
    assert q.last_price == 1733.0
    assert q.pe_ratio is None and q.avg_10day_volume is None


def test_missing_reference_block_is_survivable():
    block = {**SNDK_BLOCK}
    del block["reference"]
    q = parse_quote("SNDK", block)
    assert q.description is None and q.is_shortable is None
    assert q.last_price == 1733.0


def test_block_without_quote_is_skipped_not_fatal():
    got = fetch_quotes(_Client({"SNDK": SNDK_BLOCK, "JUNK": {"x": 1}}),
                       ["SNDK", "JUNK"])
    assert set(got) == {"SNDK"}


def test_non_200_raises_with_body():
    with pytest.raises(RuntimeError, match="401"):
        fetch_quotes(_Client({}, status=401, text="unauthorized"), ["SNDK"])


def test_missing_symbol_raises_clearly():
    with pytest.raises(RuntimeError, match="no usable block"):
        fetch_quote(_Client({}), "NOPE")


def test_empty_symbol_list_makes_no_call():
    c = _Client({})
    assert fetch_quotes(c, []) == {}
    assert c.seen is None


# ---------------------------------------------------------------- derived

def test_spread_and_halt_state():
    q = _q()
    assert q.mid == pytest.approx(1733.0)
    assert q.spread == pytest.approx(2.0)
    assert q.spread_bps == pytest.approx(2.0 / 1733.0 * 10_000)
    assert q.is_halted is False


def test_halted_status_is_detected():
    block = {**SNDK_BLOCK,
             "quote": {**SNDK_BLOCK["quote"], "securityStatus": "Halted"}}
    assert parse_quote("SNDK", block).is_halted is True


def test_borrow_and_reference_data_survives():
    q = _q()
    assert q.is_shortable is True
    assert q.is_hard_to_borrow is False
    assert q.htb_quantity == 5268067
    assert q.description == "SANDISK CORP"
    assert q.optionable is True
