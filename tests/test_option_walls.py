"""Tests for option-chain analytics and the spot-consistency guard.

The guard is the reason this module exists, so most of these tests are about
it. The failure it prevents was measured on live SNDK data on 2026-08-16: a
$14.39 spot divergence turned into a ~20 vol-point call/put IV gap at the
front expiry, which a naive 25-delta risk reversal would have reported as
extreme put skew.

Properties asserted:

  1. Parity recovers the spot the options were actually quoted against.
  2. Too few pairs yields None, and None is treated as UNTRUSTWORTHY.
  3. When the guard fails, skew functions return nothing at all - not a
     flagged number. A warning beside a plausible figure gets read past.
  4. The guard cannot be silenced by handing it a corrected spot, because the
     skew functions take a SpotCheck rather than a bare float.
  5. The vendor IV unit difference (Schwab percent, Alpaca decimal) is
     converted in exactly one place.
  6. Absent open interest is never read as zero.
  7. The 0DTE guards from the original inspection script survive the move.

No network.
"""

from __future__ import annotations

import pytest

from analysis.option_walls import (
    DEFAULT_SPOT_TOLERANCE,
    ChainRow,
    StrikeCoverage,
    check_spot_consistency,
    coverage_table,
    detect_oi_walls,
    flow_outliers,
    from_alpaca,
    from_schwab,
    implied_spot_from_parity,
    iv_skew_curve,
    put_call_oi_ratio,
    risk_reversal,
    zero_dte_volume,
)


def _row(put_call="CALL", strike=100.0, *, exp="2026-09-19", dte=30,
         bid=None, ask=None, volume=0, oi=None, iv=None, delta=None,
         multiplier=100.0, source="test") -> ChainRow:
    return ChainRow(
        symbol=f"X{exp}{put_call[0]}{strike}", put_call=put_call,
        strike=strike, expiration=exp, dte=dte, bid=bid, ask=ask,
        volume=volume, open_interest=oi, iv=iv, delta=delta,
        multiplier=multiplier, quote_age_ms=None, source=source)


def _parity_chain(spot: float, strikes, *, exp="2026-09-19", dte=30,
                  spread=0.10):
    """Synthetic pairs satisfying C - P = S - K exactly.

    Intrinsic-only pricing keeps the fixture honest: the parity estimator is
    supposed to work without knowing anything about volatility, so the
    fixture must not encode a vol assumption for it to exploit.
    """
    rows = []
    for k in strikes:
        c_mid = max(spot - k, 0.0) + 1.0
        p_mid = c_mid - (spot - k)
        for side, mid in (("CALL", c_mid), ("PUT", p_mid)):
            rows.append(_row(side, k, exp=exp, dte=dte,
                             bid=mid - spread / 2, ask=mid + spread / 2))
    return rows


# ------------------------------------------------------------ parity estimate

def test_parity_recovers_the_spot():
    rows = _parity_chain(1642.61, [400, 500, 600, 700, 800, 900])
    got, n, sd = implied_spot_from_parity(rows, reference_spot=1657.0)
    assert got == pytest.approx(1642.61, abs=0.01)
    assert n == 6
    assert sd == pytest.approx(0.0, abs=0.01)


def test_parity_needs_at_least_three_pairs():
    """A median of one or two pairs is not an estimate. Returning one anyway
    would give the guard false confidence exactly when it has least data."""
    rows = _parity_chain(1000.0, [100, 200])
    got, n, _ = implied_spot_from_parity(rows, reference_spot=1000.0)
    assert got is None
    assert n == 2


def test_parity_ignores_near_the_money_strikes():
    """Deep-ITM only: their parity relation is nearly vol-independent, so a
    wrong vol assumption cannot bias the estimate."""
    rows = _parity_chain(1000.0, [950, 960, 970, 980])
    got, n, _ = implied_spot_from_parity(rows, reference_spot=1000.0)
    assert got is None and n == 0


def test_parity_skips_pairs_missing_a_leg():
    rows = _parity_chain(1000.0, [100, 200, 300])
    rows = [r for r in rows if not (r.strike == 200 and r.is_call)]
    _, n, _ = implied_spot_from_parity(rows, reference_spot=1000.0)
    assert n == 2


# -------------------------------------------------------------- guard verdict

def test_guard_passes_when_spot_agrees():
    rows = _parity_chain(1000.0, [100, 200, 300, 400])
    chk = check_spot_consistency(rows, 1000.0)
    assert chk.trustworthy
    assert "consistent" in chk.explain().lower()


def test_guard_fails_on_the_real_sndk_divergence():
    """Regression against the live 2026-08-16 measurement: options quoted at
    1642.61, equity printed 1657.00, a 0.88% divergence."""
    rows = _parity_chain(1642.61, [400, 600, 800, 1000, 1100])
    chk = check_spot_consistency(rows, 1657.00)
    assert not chk.trustworthy
    assert chk.divergence == pytest.approx(14.39, abs=0.05)
    assert chk.divergence_pct == pytest.approx(0.00876, abs=0.0005)
    assert "SPOT MISMATCH" in chk.explain()


def test_guard_estimates_the_iv_distortion():
    """The operator needs the consequence, not just the cause. A $14 error at
    5 DTE is ~19 vol points; the same error at 40 DTE is a fraction of that.
    """
    strikes = [400, 600, 800, 1000, 1100]
    near = check_spot_consistency(
        _parity_chain(1642.61, strikes, dte=5), 1657.00)
    far = check_spot_consistency(
        _parity_chain(1642.61, strikes, dte=40), 1657.00)
    assert near.est_iv_error_vol_points == pytest.approx(19, abs=3)
    assert far.est_iv_error_vol_points < near.est_iv_error_vol_points / 2


def test_unknown_spot_is_untrustworthy_not_trustworthy():
    """Defaulting to trustworthy when there is no evidence is how a stale
    spot silently reaches a skew calculation."""
    chk = check_spot_consistency([_row("CALL", 100.0)], 100.0)
    assert chk.implied_spot is None
    assert not chk.trustworthy
    assert "UNKNOWN" in chk.explain()


def test_narrow_chain_cannot_be_checked_and_says_why():
    """Operational limit worth naming: parity needs deep-ITM strikes, and a
    chain fetched with a tight strike window around ATM has none. The verdict
    is untrustworthy - correct, but the operator needs to know it is a fetch
    problem, not a data problem, or they will chase the wrong thing.

    scripts/fetch_option_chain.py defaults to strike_count=25, which is
    exactly this case.
    """
    rows = _parity_chain(1000.0, [960, 980, 1000, 1020, 1040])
    chk = check_spot_consistency(rows, 1000.0)
    assert not chk.trustworthy
    assert chk.implied_spot is None
    msg = chk.explain()
    assert "narrow strike window" in msg
    assert "Refetch" in msg


def test_missing_printed_spot_is_untrustworthy():
    rows = _parity_chain(1000.0, [100, 200, 300])
    assert not check_spot_consistency(rows, None).trustworthy


def test_tolerance_is_configurable():
    rows = _parity_chain(1000.0, [100, 200, 300])
    assert not check_spot_consistency(rows, 1005.0).trustworthy
    assert check_spot_consistency(rows, 1005.0, tolerance=0.01).trustworthy


# ------------------------------------------------------- skew gated by guard

def _skew_rows(spot: float, dte: int = 5):
    # Strikes as FRACTIONS of spot, all comfortably under the deep-ITM
    # cutoff. Absolute strikes made this fixture silently spot-dependent:
    # at spot 1000 only two of them cleared the cutoff, the estimator
    # correctly returned None, and the skew tests failed for the right
    # reason against the wrong data.
    deep = [round(spot * f, 2) for f in (0.25, 0.35, 0.45, 0.55, 0.65)]
    rows = _parity_chain(spot, deep, dte=dte)
    for k, d, iv in ((spot * 0.94, -0.25, 1.04), (spot * 0.97, -0.40, 0.95)):
        rows.append(_row("PUT", round(k, 2), dte=dte, iv=iv, delta=d))
    for k, d, iv in ((spot * 1.09, 0.25, 0.93), (spot * 1.03, 0.40, 0.90)):
        rows.append(_row("CALL", round(k, 2), dte=dte, iv=iv, delta=d))
    return rows


def test_risk_reversal_returns_none_when_guard_fails():
    """Not a flagged number - nothing. Equities normally carry put skew, so a
    call-skew reading is a real signal and must not be manufacturable by a
    stale spot."""
    rows = _skew_rows(1642.61)
    chk = check_spot_consistency(rows, 1657.00)
    assert not chk.trustworthy
    assert risk_reversal(rows, chk) is None


def test_risk_reversal_computes_when_guard_passes():
    rows = _skew_rows(1000.0)
    chk = check_spot_consistency(rows, 1000.0)
    assert chk.trustworthy
    rr = risk_reversal(rows, chk)
    assert rr is not None
    assert rr.put_iv == pytest.approx(1.04)
    assert rr.call_iv == pytest.approx(0.93)
    assert rr.value_vol_points == pytest.approx(11.0, abs=0.01)
    assert rr.direction == "put skew"


def test_risk_reversal_picks_the_25_delta_strikes():
    rows = _skew_rows(1000.0)
    rr = risk_reversal(rows, check_spot_consistency(rows, 1000.0))
    assert abs(rr.put_delta) == pytest.approx(0.25)
    assert abs(rr.call_delta) == pytest.approx(0.25)


def test_skew_functions_take_a_spotcheck_not_a_float():
    """API shape is the enforcement. There is no argument through which a
    parity-corrected spot can be slipped past the guard by accident - which
    would silence it without correcting the vendor's IV."""
    rows = _skew_rows(1000.0)
    with pytest.raises(AttributeError):
        risk_reversal(rows, 1000.0)
    with pytest.raises(AttributeError):
        iv_skew_curve(rows, 1000.0)


def test_iv_skew_curve_empty_when_guard_fails():
    rows = _skew_rows(1642.61)
    assert iv_skew_curve(rows, check_spot_consistency(rows, 1657.00)) == []


def test_iv_skew_curve_is_otm_only_and_sorted():
    rows = _skew_rows(1000.0)
    curve = iv_skew_curve(rows, check_spot_consistency(rows, 1000.0))
    assert curve
    assert curve == sorted(curve, key=lambda r: r.strike)
    for r in curve:
        assert (r.is_call and r.strike >= 1000.0) or \
               (not r.is_call and r.strike <= 1000.0)


def test_zero_dte_never_used_for_skew():
    """IV diverges mechanically as time to expiry approaches zero. The first
    skew table built without this guard showed a delta +0.975 call at IV 245.
    """
    rows = _skew_rows(1000.0, dte=0)
    assert risk_reversal(rows, check_spot_consistency(rows, 1000.0)) is None


# ------------------------------------------------------------------- adapters

class _FakeSchwab:
    symbol = "SNDK  260919C01600000"
    put_call = "CALL"
    strike = 1600.0
    expiration = "2026-09-19"
    days_to_expiration = 30
    bid, ask = 10.0, 10.4
    volume, open_interest = 900, 5000
    volatility = 86.3          # PERCENT
    delta = 0.55
    multiplier = 100.0


class _FakeAlpaca:
    symbol = "SNDK260919C01600000"
    put_call = "CALL"
    strike = 1600.0
    expiration = "2026-09-19"
    bid, ask = 10.0, 10.4
    implied_volatility = 0.863  # DECIMAL
    delta = 0.55
    quote_age_ms = 1234.0


def test_schwab_iv_is_converted_from_percent():
    """Schwab reports 86.3, Alpaca reports 0.863. Mixing them silently is a
    100x error in a field nobody eyeballs."""
    assert from_schwab(_FakeSchwab()).iv == pytest.approx(0.863)


def test_alpaca_iv_is_already_decimal():
    assert from_alpaca(_FakeAlpaca()).iv == pytest.approx(0.863)


def test_alpaca_rows_have_no_open_interest():
    """Alpaca's options market data API has no OI field at all. None must not
    become zero anywhere downstream."""
    assert from_alpaca(_FakeAlpaca()).open_interest is None


def test_schwab_rows_carry_open_interest():
    assert from_schwab(_FakeSchwab()).open_interest == 5000


# ---------------------------------------------------------------- OI walls

def test_walls_aggregate_across_expirations():
    rows = [
        _row("CALL", 1600.0, exp="2026-08-21", oi=4000),
        _row("CALL", 1600.0, exp="2026-09-19", oi=5783),
        _row("CALL", 1700.0, exp="2026-08-21", oi=7420),
    ]
    walls = detect_oi_walls(rows, 1650.0, side="CALL")
    assert [(w.strike, w.open_interest) for w in walls] == [
        (1600.0, 9783), (1700.0, 7420)]


def test_walls_report_shares_not_just_contracts():
    """Mini and adjusted contracts are not 100 shares. Ranking on contracts
    alone silently misstates exposure by up to 10x."""
    rows = [_row("CALL", 1600.0, oi=1000, multiplier=10.0)]
    assert detect_oi_walls(rows, 1600.0)[0].shares == 10_000


def test_walls_skip_rows_with_no_open_interest():
    """An all-Alpaca chain must yield no walls, not walls of zero."""
    rows = [_row("CALL", 1600.0, oi=None), _row("CALL", 1700.0, oi=None)]
    assert detect_oi_walls(rows, 1650.0) == []


def test_walls_report_distance_and_role():
    walls = detect_oi_walls([_row("CALL", 1700.0, oi=100)], 1650.0)
    assert walls[0].distance_pct == pytest.approx(3.03, abs=0.01)
    assert walls[0].role == "resistance"
    puts = detect_oi_walls([_row("PUT", 1600.0, oi=100)], 1650.0, side="PUT")
    assert puts[0].role == "support"


def test_put_call_oi_ratio_none_without_calls():
    assert put_call_oi_ratio([_row("PUT", 100.0, oi=50)]) is None
    assert put_call_oi_ratio([
        _row("PUT", 100.0, oi=50), _row("CALL", 100.0, oi=100)]) == 0.5


# -------------------------------------------------------------------- flow

def test_flow_excludes_zero_dte():
    """The 0DTE artifact: OI is a T+1 figure, so a same-day expiry has OI near
    zero and any volume yields an enormous ratio. The first flow table ranked
    entirely on 0DTE churn and showed v/OI 1882."""
    rows = [
        _row("PUT", 100.0, dte=0, volume=5000, oi=300),
        _row("CALL", 110.0, dte=7, volume=900, oi=300),
    ]
    assert [r.strike for r in flow_outliers(rows)] == [110.0]


def test_flow_requires_minimum_prior_oi_and_volume():
    rows = [
        _row("CALL", 100.0, dte=7, volume=5000, oi=10),    # OI too thin
        _row("CALL", 110.0, dte=7, volume=5, oi=5000),     # volume too thin
        _row("CALL", 120.0, dte=7, volume=900, oi=300),    # qualifies
    ]
    assert [r.strike for r in flow_outliers(rows)] == [120.0]


def test_flow_sorted_by_ratio_descending():
    rows = [
        _row("CALL", 100.0, dte=7, volume=600, oi=300),   # 2.0
        _row("CALL", 110.0, dte=7, volume=3000, oi=300),  # 10.0
        _row("CALL", 120.0, dte=7, volume=1500, oi=300),  # 5.0
    ]
    assert [r.strike for r in flow_outliers(rows)] == [110.0, 120.0, 100.0]


def test_volume_oi_ratio_none_when_no_prior_oi():
    """Not zero and not infinity. A contract with no prior OI has no ratio."""
    assert _row("CALL", 100.0, volume=500, oi=0).volume_oi_ratio is None
    assert _row("CALL", 100.0, volume=500, oi=None).volume_oi_ratio is None


def test_zero_dte_reported_separately_on_raw_volume():
    rows = [
        _row("CALL", 100.0, dte=0, volume=168_217, oi=5),
        _row("PUT", 100.0, dte=0, volume=67_309, oi=5),
        _row("CALL", 110.0, dte=7, volume=900, oi=300),
    ]
    calls, puts, zero = zero_dte_volume(rows)
    assert (calls, puts) == (168_217, 67_309)
    assert [r.volume for r in zero] == [168_217, 67_309]


# --------------------------------------------------------------- trap #5
#
# coverage_table replaces the full-coverage-only ranking that shipped in
# 63d01e8. That guard was right about the disease and wrong about the cure: it
# stopped ranking incomparable totals against each other by DROPPING most of
# them, which hid real open interest. These tests pin the replacement.
#
# Magnitudes are taken from the live SNDK chain of 2026-08-17 at spot ~1786.85
# so the fixture doubles as a record of the case that motivated the change.


def _coverage_chain():
    """5 strikes over 8 expirations, two of them deliberately truncated.

    Coverage and magnitudes follow the live 2026-08-17 SNDK chain:

        1700  8/8   6,231     ranked by the old guard
        1800  8/8   4,970     ranked by the old guard
        1740  8/8     478     ranked by the old guard
        1750  7/8   3,256     EXILED by the old guard
        1600  3/8  11,743     EXILED by the old guard, and the largest
                              single concentration in the chain

    That last row is the case for the change: the biggest number in the book
    was in the bucket the previous design refused to rank.
    """
    exps = [f"2026-09-{d:02d}" for d in range(1, 9)]
    rows = []
    for e in exps:
        rows.append(_row("CALL", 1700.0, exp=e, oi=6231 // 8))
        rows.append(_row("CALL", 1800.0, exp=e, oi=4970 // 8))
        rows.append(_row("CALL", 1740.0, exp=e, oi=478 // 8))
        rows.append(_row("PUT", 1700.0, exp=e, oi=100))
    for e in exps[:7]:
        rows.append(_row("CALL", 1750.0, exp=e, oi=3256 // 7))
    for e in exps[:3]:
        rows.append(_row("CALL", 1600.0, exp=e, oi=11743 // 3))
    return rows


def test_partial_coverage_strike_is_ranked_not_exiled():
    """The exact regression: 1750 at 7/8 outranks 1740 at 8/8."""
    table = coverage_table(_coverage_chain(), 1786.85, side="CALL")
    strikes = [s.strike for s in table]
    assert 1750.0 in strikes, "partial-coverage strike was dropped"
    assert strikes.index(1750.0) < strikes.index(1740.0)


def test_no_strike_is_silently_dropped():
    rows = _coverage_chain()
    expected = {r.strike for r in rows if r.is_call}
    assert {s.strike for s in coverage_table(rows, side="CALL")} == expected


def test_coverage_label_carries_the_basis():
    table = {s.strike: s for s in coverage_table(_coverage_chain(), side="CALL")}
    assert table[1750.0].coverage == "7/8"
    assert table[1700.0].coverage == "8/8"
    assert table[1750.0].is_full_coverage is False
    assert table[1700.0].is_full_coverage is True


def test_the_largest_concentration_in_the_chain_is_the_one_that_was_exiled():
    """1600 at 3/8 coverage carries more OI than any full-coverage strike."""
    table = coverage_table(_coverage_chain(), 1786.85, side="CALL")
    assert table[0].strike == 1600.0
    assert table[0].is_full_coverage is False


def test_oi_per_expiration_normalises_across_coverage():
    """1600 is 3,914 per expiration against 1700's 779 — a 5x gap the raw
    total understates as 11,743 against 6,231, under 2x."""
    table = {s.strike: s for s in coverage_table(_coverage_chain(), side="CALL")}
    assert table[1600.0].oi_per_expiration == pytest.approx(11743 // 3)
    assert table[1700.0].oi_per_expiration == pytest.approx(6231 // 8)
    assert (table[1600.0].oi_per_expiration
            > 4 * table[1700.0].oi_per_expiration)


def test_denominator_counts_expirations_across_both_sides():
    """The truncation is a property of the fetch, not of one side of it."""
    rows = [_row("CALL", 100.0, exp="2026-09-01", oi=10),
            _row("PUT", 100.0, exp="2026-09-08", oi=10)]
    call = coverage_table(rows, side="CALL")
    assert call[0].expirations_total == 2
    assert call[0].expirations_covered == 1


def test_absent_open_interest_is_not_read_as_zero():
    """Alpaca has no OI field. A table of zeros would look like an answer."""
    rows = [_row("CALL", 100.0, exp="2026-09-01", oi=None),
            _row("CALL", 110.0, exp="2026-09-01", oi=7)]
    table = coverage_table(rows, side="CALL")
    assert [s.strike for s in table] == [110.0]


def test_ties_break_on_strike_so_ordering_is_deterministic():
    rows = [_row("CALL", 120.0, exp="2026-09-01", oi=50),
            _row("CALL", 110.0, exp="2026-09-01", oi=50)]
    assert [s.strike for s in coverage_table(rows, side="CALL")] == [110.0, 120.0]


def test_distance_pct_is_none_without_a_spot():
    table = coverage_table(_coverage_chain(), side="CALL")
    assert all(s.distance_pct is None for s in table)


def test_distance_pct_is_signed_against_spot():
    table = {s.strike: s for s in coverage_table(_coverage_chain(), 1786.85,
                                                 side="CALL")}
    assert table[1800.0].distance_pct == pytest.approx(0.7359, abs=1e-3)
    assert table[1700.0].distance_pct < 0


def test_top_limits_without_reordering():
    table = coverage_table(_coverage_chain(), side="CALL", top=2)
    assert [s.strike for s in table] == [1600.0, 1700.0]


def test_empty_chain_returns_empty_table():
    assert coverage_table([], side="CALL") == []


def test_put_side_is_selectable():
    table = coverage_table(_coverage_chain(), side="PUT")
    assert [s.strike for s in table] == [1700.0]
    assert table[0].side == "PUT"
