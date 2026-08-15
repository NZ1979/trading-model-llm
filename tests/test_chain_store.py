"""Tests for the option-chain corpus writer.

Covers the properties that actually matter for an OI-change signal:

  1. A re-fetch on the same date UPDATES rather than duplicates. Two rows for
     one contract on one date would double every downstream OI diff.
  2. The prior session is the previous STORED session, not the previous
     calendar day. Weekends and missed fetches must not read as one day.
  3. Contracts present on only one side are excluded from oi_change, because
     absent-treated-as-zero reports a new listing's entire OI as accumulation.
  4. WAL holds, so the pre-market fetch can write while a reader queries.
  5. The T+1 labelling survives into the result objects.

No network. tmp_path only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from data.chain_store import ChainStore, OIChange, session_date_et
from data.schwab_chains import OptionChain, OptionContract


# ---------------------------------------------------------------- factories

def _contract(symbol="SNDK  260919C01600000", *, put_call="CALL",
              strike=1600.0, expiration="2026-09-19", dte=35,
              volume=1000, oi=5000, multiplier=100.0, delta=0.55,
              volatility=42.0, is_mini=False) -> OptionContract:
    return OptionContract(
        symbol=symbol, underlying="SNDK", put_call=put_call, strike=strike,
        expiration=expiration, days_to_expiration=dte,
        bid=10.0, ask=10.4, last=10.2, mark=10.2, bid_size=12, ask_size=15,
        volume=volume, open_interest=oi,
        volatility=volatility, delta=delta, gamma=0.004, theta=-0.8,
        vega=1.2, rho=0.3,
        in_the_money=False, intrinsic_value=0.0, time_value=10.2,
        multiplier=multiplier, is_penny_pilot=True, is_mini=is_mini,
        is_non_standard=False, option_root="SNDK",
    )


def _chain(*contracts, price=1610.0, when=None, delayed=True) -> OptionChain:
    return OptionChain(
        underlying="SNDK",
        underlying_price=price,
        fetched_at=when or datetime(2026, 8, 14, 13, 0, tzinfo=timezone.utc),
        contracts=list(contracts),
        is_delayed=delayed,
        status="SUCCESS",
    )


@pytest.fixture()
def store(tmp_path):
    with ChainStore(str(tmp_path / "chains")) as s:
        yield s


# ------------------------------------------------------------------- basics

def test_session_date_is_et_not_utc():
    """01:00 UTC on the 15th is still 21:00 ET on the 14th. A UTC date would
    file a late fetch under the wrong session."""
    late = datetime(2026, 8, 15, 1, 0, tzinfo=timezone.utc)
    assert session_date_et(late) == "20260814"


def test_wal_is_enforced(store):
    mode = sqlite3.connect(store.db_path).execute(
        "PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_write_and_read_back(store):
    n = store.write_chain(_chain(_contract()), session_date="20260814")
    assert n == 1
    rows = store.snapshot("SNDK", "20260814")
    assert len(rows) == 1
    assert rows[0]["open_interest"] == 5000
    assert rows[0]["is_delayed"] == 1


def test_reader_can_query_while_writer_open(store):
    """WAL's whole purpose here: a reader must not block the fetch."""
    store.write_chain(_chain(_contract()), session_date="20260814")
    other = sqlite3.connect(store.db_path)
    got = other.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0]
    assert got == 1
    other.close()


# ------------------------------------------------------------- idempotency

def test_refetch_same_date_updates_not_duplicates(store):
    """The single most important property. A retry after a partial failure
    must not create a second row, or every OI diff crossing that date doubles.
    """
    store.write_chain(_chain(_contract(oi=5000)), session_date="20260814")
    store.write_chain(_chain(_contract(oi=5250)), session_date="20260814")

    rows = store.snapshot("SNDK", "20260814")
    assert len(rows) == 1, "re-fetch duplicated the contract"
    assert rows[0]["open_interest"] == 5250, "re-fetch did not update OI"


def test_refetch_updates_fetch_ledger(store):
    store.write_chain(_chain(_contract(), _contract(
        symbol="SNDK  260919P01600000", put_call="PUT")),
        session_date="20260814")
    store.write_chain(_chain(_contract()), session_date="20260814")
    assert store.stats()["fetches"] == 1
    assert store.stats()["snapshot_rows"] == 2  # stale row is NOT purged


# ------------------------------------------------------------- session gaps

def test_prior_session_is_previous_stored_not_previous_day(store):
    """Friday -> Monday must resolve to Friday, not to a nonexistent Sunday."""
    store.write_chain(_chain(_contract()), session_date="20260814")  # Fri
    store.write_chain(_chain(_contract()), session_date="20260817")  # Mon
    assert store.prior_session("SNDK", "20260817") == "20260814"


def test_prior_session_none_on_first_fetch(store):
    store.write_chain(_chain(_contract()), session_date="20260814")
    assert store.prior_session("SNDK", "20260814") is None


def test_oi_change_empty_on_first_fetch(store):
    """Not an error. The first fetch has nothing to difference against."""
    store.write_chain(_chain(_contract()), session_date="20260814")
    assert store.oi_change("SNDK") == []


def test_failed_fetch_is_not_a_prior_session(store):
    """A recorded failure must not become the comparison baseline — that would
    diff against an empty chain and report every contract as brand new."""
    store.write_chain(_chain(_contract()), session_date="20260813")
    store.record_failure("SNDK", "HTTP 429", session_date="20260814")
    store.write_chain(_chain(_contract()), session_date="20260817")
    assert store.prior_session("SNDK", "20260817") == "20260813"


# --------------------------------------------------------------- oi_change

def test_oi_change_computes_delta(store):
    store.write_chain(_chain(_contract(oi=5000)), session_date="20260813")
    store.write_chain(_chain(_contract(oi=6200, volume=3100)),
                      session_date="20260814")

    changes = store.oi_change("SNDK")
    assert len(changes) == 1
    ch = changes[0]
    assert ch.open_interest == 6200
    assert ch.prior_open_interest == 5000
    assert ch.oi_change == 1200
    assert ch.volume == 3100
    assert ch.oi_change_pct == pytest.approx(24.0)
    assert ch.oi_change_shares == pytest.approx(120_000.0)


def test_oi_change_handles_decline(store):
    store.write_chain(_chain(_contract(oi=9000)), session_date="20260813")
    store.write_chain(_chain(_contract(oi=7000)), session_date="20260814")
    ch = store.oi_change("SNDK")[0]
    assert ch.oi_change == -2000
    assert ch.oi_change_pct == pytest.approx(-22.222, rel=1e-3)


def test_oi_change_pct_is_none_when_prior_is_zero(store):
    """0 -> 5000 is a new position, not an infinite percentage. Returning a
    number here would rank it top of any accumulation table by construction.
    """
    store.write_chain(_chain(_contract(oi=0)), session_date="20260813")
    store.write_chain(_chain(_contract(oi=5000)), session_date="20260814")
    ch = store.oi_change("SNDK")[0]
    assert ch.oi_change == 5000
    assert ch.oi_change_pct is None


def test_oi_change_excludes_contracts_absent_on_prior_date(store):
    """A newly listed strike has no prior OI. Treating absent as zero would
    report its entire OI as one day's accumulation."""
    store.write_chain(_chain(_contract(oi=5000)), session_date="20260813")
    store.write_chain(
        _chain(_contract(oi=5100),
               _contract(symbol="SNDK  260919C01700000", strike=1700.0,
                         oi=8000)),
        session_date="20260814")

    changes = store.oi_change("SNDK")
    assert [c.strike for c in changes] == [1600.0]

    new = store.new_contracts("SNDK")
    assert [r["strike"] for r in new] == [1700.0]
    assert new[0]["open_interest"] == 8000


def test_oi_change_sorted_by_absolute_move(store):
    store.write_chain(
        _chain(_contract(symbol="A", oi=1000),
               _contract(symbol="B", oi=1000),
               _contract(symbol="C", oi=1000)),
        session_date="20260813")
    store.write_chain(
        _chain(_contract(symbol="A", oi=1100),      # +100
               _contract(symbol="B", oi=100),        # -900
               _contract(symbol="C", oi=1500)),      # +400
        session_date="20260814")
    assert [c.symbol for c in store.oi_change("SNDK")] == ["B", "C", "A"]


def test_zero_dte_excluded_by_default(store):
    """The 0DTE artifact, in its OI-change form: an expiring contract's OI
    collapses to zero mechanically, which reads as mass liquidation."""
    store.write_chain(_chain(_contract(dte=1, oi=9000)),
                      session_date="20260813")
    store.write_chain(_chain(_contract(dte=0, oi=200)),
                      session_date="20260814")
    assert store.oi_change("SNDK") == []
    assert len(store.oi_change("SNDK", min_days_to_expiration=0)) == 1


def test_min_open_interest_filter(store):
    store.write_chain(
        _chain(_contract(symbol="THIN", oi=10),
               _contract(symbol="LIQUID", oi=5000)),
        session_date="20260813")
    store.write_chain(
        _chain(_contract(symbol="THIN", oi=90),
               _contract(symbol="LIQUID", oi=6000)),
        session_date="20260814")
    got = store.oi_change("SNDK", min_open_interest=250)
    assert [c.symbol for c in got] == ["LIQUID"]


def test_min_abs_change_filter(store):
    store.write_chain(
        _chain(_contract(symbol="FLAT", oi=5000),
               _contract(symbol="MOVER", oi=5000)),
        session_date="20260813")
    store.write_chain(
        _chain(_contract(symbol="FLAT", oi=5002),
               _contract(symbol="MOVER", oi=8000)),
        session_date="20260814")
    got = store.oi_change("SNDK", min_abs_change=100)
    assert [c.symbol for c in got] == ["MOVER"]


def test_explicit_date_pair_overrides_default(store):
    """Comparing across a gap must be possible, and must report the dates it
    actually used rather than implying adjacency."""
    for d, oi in (("20260810", 1000), ("20260813", 4000), ("20260814", 4100)):
        store.write_chain(_chain(_contract(oi=oi)), session_date=d)
    ch = store.oi_change("SNDK", prior_session_date="20260810")[0]
    assert ch.oi_change == 3100
    assert ch.session_date == "20260814"
    assert ch.prior_session_date == "20260810"


# ------------------------------------------------------------ T+1 labelling

def test_result_carries_both_closes_not_just_fetch_dates(store):
    """OI on a fetch dated D is the close of D-1. The result must say so, or
    the caller will report it as 'yesterday's change'."""
    store.write_chain(_chain(_contract(oi=5000)), session_date="20260813")
    store.write_chain(_chain(_contract(oi=6000)), session_date="20260814")
    ch = store.oi_change("SNDK")[0]
    assert ch.session_date == "20260814"
    assert "2026-08-14" in ch.as_of_close
    assert "before" in ch.as_of_close      # not the fetch date itself
    assert "2026-08-13" in ch.prior_close


# ---------------------------------------------------------------- bookkeeping

def test_multiplier_preserved_for_notional(store):
    """Mini contracts are 10 shares, not 100. Folding them into a share-
    denominated wall silently corrupts it by 10x."""
    store.write_chain(_chain(_contract(multiplier=10.0, oi=1000, is_mini=True)),
                      session_date="20260813")
    store.write_chain(_chain(_contract(multiplier=10.0, oi=2000, is_mini=True)),
                      session_date="20260814")
    ch = store.oi_change("SNDK")[0]
    assert ch.oi_change_shares == pytest.approx(10_000.0)


def test_record_failure_is_visible_in_stats(store):
    store.record_failure("SNDK", "HTTP 429 rate limited",
                         session_date="20260814")
    st = store.stats()
    assert st["failed_fetches"] == 1
    assert st["snapshot_rows"] == 0


def test_sessions_and_underlyings(store):
    store.write_chain(_chain(_contract()), session_date="20260813")
    store.write_chain(_chain(_contract()), session_date="20260814")
    assert store.sessions("SNDK") == ["20260813", "20260814"]
    assert store.underlyings() == ["SNDK"]
    assert store.latest_session("SNDK") == "20260814"


def test_use_before_open_raises(tmp_path):
    s = ChainStore(str(tmp_path / "chains"))
    with pytest.raises(RuntimeError, match="before open"):
        s.snapshot("SNDK")
