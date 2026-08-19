"""Tests for dealer gamma exposure and the flip-point solver.

The module's whole output rests on an unverifiable assumption — that dealers
are long calls and short puts — so these tests do not try to prove the model
is right about the market. They prove it is right about its own arithmetic,
and that every place it cannot know something says so instead of guessing.

Properties asserted:

  1. Black-Scholes gamma has the shape gamma must have: peaks at the money,
     rises as expiry approaches, decays into the wings.
  2. Degenerate inputs return None, never 0.0. A contract whose gamma is
     unknowable must not contribute zero to a sum reported as complete.
  3. The sign convention is exactly the documented one, and flipping the
     book flips the regime.
  4. The flip lands between the strikes that produce it, and returns None -
     an honest answer - when no crossing exists in the searched window.
  5. Vendor gamma is used at spot and NEVER in the flip search, because a
     gamma published at one spot does not describe the contract at another.
  6. Absent open interest and absent IV are counted and reported, not
     silently dropped into the total.
  7. The profile carries its strike span, so a windowed chain cannot be
     read as a whole-chain figure (repo trap #5, which produced a 1570 flip
     from a +/-40 fetch and 1590.32 from +/-200 on the same 2026-08-19 chain).
  8. 0DTE is excluded by default, where gamma diverges and OI is near zero
     by construction (repo trap #1).

No network.
"""

from __future__ import annotations

import pytest

from analysis.gamma_exposure import (
    GammaRow,
    bs_gamma,
    cross_check,
    from_stored,
    gamma_profile,
)


# --------------------------------------------------------------- bs_gamma

def test_gamma_peaks_at_the_money():
    atm = bs_gamma(100, 100, 30 / 365, 0.30)
    otm = bs_gamma(100, 130, 30 / 365, 0.30)
    itm = bs_gamma(100, 70, 30 / 365, 0.30)
    assert atm > otm > 0
    assert atm > itm > 0


def test_atm_gamma_rises_as_expiry_approaches():
    near = bs_gamma(100, 100, 2 / 365, 0.30)
    far = bs_gamma(100, 100, 180 / 365, 0.30)
    assert near > far
    # and the divergence is steep, which is why 0DTE is excluded by default
    assert near / far > 5


def test_gamma_falls_as_vol_rises_at_the_money():
    low = bs_gamma(100, 100, 30 / 365, 0.20)
    high = bs_gamma(100, 100, 30 / 365, 0.90)
    assert low > high


@pytest.mark.parametrize("args", [
    (0.0, 100, 0.1, 0.3),      # no spot
    (100, 0.0, 0.1, 0.3),      # no strike
    (100, 100, 0.0, 0.3),      # expired
    (100, 100, 0.1, 0.0),      # no vol
    (100, 100, -1.0, 0.3),     # negative time
])
def test_degenerate_inputs_return_none_not_zero(args):
    """None, so an unknowable gamma cannot be summed as if it were zero."""
    assert bs_gamma(*args) is None


# ------------------------------------------------------- sign convention

def test_calls_positive_puts_negative():
    call = gamma_profile([GammaRow("CALL", 100, 30, 1000, 0.30)], spot=100)
    put = gamma_profile([GammaRow("PUT", 100, 30, 1000, 0.30)], spot=100)
    assert call.total_gex > 0
    assert put.total_gex < 0
    assert call.total_gex == pytest.approx(-put.total_gex, rel=1e-9)


def test_regime_label_follows_the_flip():
    rows = [GammaRow("CALL", 110, 30, 10_000, 0.30),
            GammaRow("PUT", 90, 30, 10_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.flip is not None
    above = gamma_profile(rows, spot=prof.flip + 20)
    below = gamma_profile(rows, spot=prof.flip - 20)
    assert "positive" in above.regime
    assert "negative" in below.regime


# ------------------------------------------------------------ flip point

def test_flip_lands_between_the_strikes_that_produce_it():
    rows = [GammaRow("CALL", 110, 30, 10_000, 0.30),
            GammaRow("PUT", 90, 30, 10_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.flip is not None
    assert 90 < prof.flip < 110


def test_no_crossing_returns_none_not_a_number():
    """Calls only: net gamma is positive everywhere, so there is no flip."""
    rows = [GammaRow("CALL", 100, 30, 10_000, 0.30),
            GammaRow("CALL", 120, 30, 10_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.flip is None
    assert prof.total_gex > 0


def test_flip_search_window_is_reported():
    rows = [GammaRow("CALL", 110, 30, 1000, 0.30)]
    prof = gamma_profile(rows, spot=100, flip_lo_pct=0.8, flip_hi_pct=1.2)
    assert prof.flip_searched == pytest.approx((80.0, 120.0))


def test_vendor_gamma_never_moves_the_flip():
    """Vendor gamma is valid only at the spot it was published for.

    A wildly wrong vendor gamma must change the at-spot exposure and leave
    the flip untouched, because the solver recomputes from Black-Scholes.
    """
    plain = [GammaRow("CALL", 110, 30, 10_000, 0.30),
             GammaRow("PUT", 90, 30, 10_000, 0.30)]
    poisoned = [GammaRow("CALL", 110, 30, 10_000, 0.30, vendor_gamma=99.0,
                         chain_underlying=100.0),
                GammaRow("PUT", 90, 30, 10_000, 0.30, vendor_gamma=99.0,
                         chain_underlying=100.0)]
    a = gamma_profile(plain, spot=100)
    b = gamma_profile(poisoned, spot=100)
    assert b.total_gex != pytest.approx(a.total_gex)
    assert b.flip == pytest.approx(a.flip)


# --------------------------------------------------------------- guards

def test_zero_dte_excluded_and_counted():
    rows = [GammaRow("CALL", 100, 0, 5_000, 0.30),
            GammaRow("CALL", 100, 30, 5_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.contracts_skipped_zero_dte == 1
    assert prof.contracts_used == 1


def test_absent_open_interest_is_counted_never_zero():
    rows = [GammaRow("CALL", 100, 30, None, 0.30),
            GammaRow("CALL", 105, 30, 0, 0.30),
            GammaRow("CALL", 110, 30, 1_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.contracts_skipped_no_oi == 2
    assert prof.contracts_used == 1


def test_absent_iv_is_counted_and_contributes_nothing():
    rows = [GammaRow("CALL", 100, 30, 1_000, None),
            GammaRow("CALL", 110, 30, 1_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert prof.contracts_skipped_no_iv == 1
    assert prof.contracts_used == 1


def test_strike_span_is_carried_so_a_window_cannot_pose_as_a_chain():
    narrow = [GammaRow("CALL", 95, 30, 1000, 0.3),
              GammaRow("CALL", 105, 30, 1000, 0.3)]
    wide = narrow + [GammaRow("CALL", 200, 30, 1000, 0.3)]
    assert gamma_profile(narrow, spot=100).strike_span == (95.0, 105.0)
    assert gamma_profile(wide, spot=100).strike_span == (95.0, 200.0)


def test_nonpositive_spot_raises():
    with pytest.raises(ValueError):
        gamma_profile([GammaRow("CALL", 100, 30, 1000, 0.30)], spot=0)


# ---------------------------------------------------------- aggregation

def test_by_strike_aggregates_across_expirations():
    rows = [GammaRow("CALL", 100, 10, 1_000, 0.30),
            GammaRow("CALL", 100, 40, 1_000, 0.30),
            GammaRow("PUT", 100, 40, 400, 0.30)]
    prof = gamma_profile(rows, spot=100)
    assert len(prof.by_strike) == 1
    only = prof.by_strike[0]
    assert only.strike == 100
    assert only.call_gex > 0 and only.put_gex < 0
    assert only.net_gex == pytest.approx(only.call_gex + only.put_gex)


def test_bands_partition_the_total():
    rows = [GammaRow("CALL", k, 30, 1_000, 0.30) for k in (90, 100, 110, 120)]
    prof = gamma_profile(rows, spot=100)
    banded = sum(net for _, _, net in prof.band_summary())
    # the top edge is exclusive, so the highest strike falls outside
    top = max(s.net_gex for s in prof.by_strike if s.strike == 120)
    assert banded == pytest.approx(prof.total_gex - top, rel=1e-9)


# ---------------------------------------------------------- cross_check

def test_cross_check_reports_zero_divergence_against_itself():
    g = bs_gamma(100, 100, 30 / 365, 0.30)
    rows = [GammaRow("CALL", 100, 30, 1_000, 0.30, vendor_gamma=g)]
    out = cross_check(rows, spot=100)
    assert out["contracts"] == 1
    assert out["median_pct_diff"] == pytest.approx(0.0, abs=1e-9)


def test_cross_check_is_empty_when_no_vendor_gamma():
    rows = [GammaRow("CALL", 100, 30, 1_000, 0.30)]
    assert cross_check(rows, spot=100)["contracts"] == 0


# -------------------------------------------------------------- adapter

class _Row(dict):
    """Stand-in for sqlite3.Row, which indexes by column name."""


def test_from_stored_divides_schwab_percent_iv():
    """chain_snapshots stores volatility as Schwab publishes it: percent."""
    row = _Row(put_call="CALL", strike=1600.0, days_to_expiration=2,
               open_interest=1234, volatility=99.6, multiplier=100.0,
               gamma=0.002, underlying_price=1594.6)
    got = from_stored([row])[0]
    assert got.iv == pytest.approx(0.996)
    assert got.vendor_gamma == pytest.approx(0.002)
    assert got.open_interest == 1234


def test_from_stored_keeps_missing_iv_as_none():
    row = _Row(put_call="PUT", strike=1500.0, days_to_expiration=9,
               open_interest=10, volatility=None, multiplier=100.0,
               gamma=None, underlying_price=1594.6)
    got = from_stored([row])[0]
    assert got.iv is None
    assert got.vendor_gamma is None


# ------------------------------------------------------- basis agreement

def test_bs_total_is_zero_at_the_flip():
    """The flip is defined as the spot where Black-Scholes net GEX crosses 0."""
    rows = [GammaRow("CALL", 110, 30, 10_000, 0.30),
            GammaRow("PUT", 90, 30, 10_000, 0.30)]
    prof = gamma_profile(rows, spot=100)
    at_flip = gamma_profile(rows, spot=prof.flip)
    # "zero" only means small against the exposure a few percent either side;
    # an absolute tolerance would be arbitrary across underlyings.
    scale = max(abs(gamma_profile(rows, spot=prof.flip * m).total_gex_bs)
                for m in (0.9, 1.1))
    assert abs(at_flip.total_gex_bs) < scale * 0.02


def test_bases_identical_when_vendor_gamma_disabled():
    rows = [GammaRow("CALL", 110, 30, 1_000, 0.30, vendor_gamma=99.0,
                     chain_underlying=100.0)]
    prof = gamma_profile(rows, spot=100, use_vendor_gamma=False)
    assert prof.total_gex == pytest.approx(prof.total_gex_bs)
    assert prof.basis_divergence_pct == pytest.approx(0.0, abs=1e-9)


def test_basis_divergence_is_reported_not_hidden():
    rows = [GammaRow("CALL", 110, 30, 1_000, 0.30, vendor_gamma=99.0,
                     chain_underlying=100.0)]
    prof = gamma_profile(rows, spot=100, use_vendor_gamma=True)
    assert prof.basis_divergence_pct is not None
    assert abs(prof.basis_divergence_pct) > 100  # poisoned vendor gamma shows up


# ------------------------------------------------- vendor gamma staleness

def test_vendor_gamma_rejected_once_spot_drifts():
    """The 2026-08-19 artifact, as a regression guard.

    A chain fetched at 04:45 carried gamma against underlying 1594.60. Read at
    spot 1561.94 — 2.1% away — the vendor basis said +3.5M (dampening) and
    Black-Scholes said -30.1M (amplifying): opposite regimes from identical
    open interest. Re-fetching at the live spot collapsed the disagreement.
    """
    rows = [GammaRow("CALL", 1600, 30, 5_000, 0.9, vendor_gamma=0.002,
                     chain_underlying=1594.60)]
    fresh = gamma_profile(rows, spot=1594.60)
    stale = gamma_profile(rows, spot=1561.94)
    assert fresh.vendor_gamma_rows == 1
    assert stale.vendor_gamma_rows == 0
    assert stale.spot_drift_pct == pytest.approx(2.05, abs=0.05)
    assert stale.total_gex == pytest.approx(stale.total_gex_bs)


def test_vendor_gamma_needs_a_reference_spot():
    """Vendor gamma with no chain_underlying cannot be freshness-checked.

    Rejecting it is the conservative read: an unverifiable gamma is not a
    usable one. Visible in vendor_gamma_rows rather than silent.
    """
    rows = [GammaRow("CALL", 110, 30, 1_000, 0.30, vendor_gamma=99.0)]
    prof = gamma_profile(rows, spot=100)
    assert prof.vendor_gamma_rows == 0
    assert prof.total_gex == pytest.approx(prof.total_gex_bs)


def test_drift_threshold_is_tunable():
    rows = [GammaRow("CALL", 1600, 30, 5_000, 0.9, vendor_gamma=0.002,
                     chain_underlying=1594.60)]
    assert gamma_profile(rows, spot=1580.0, max_drift_pct=0.5).vendor_gamma_rows == 0
    assert gamma_profile(rows, spot=1580.0, max_drift_pct=5.0).vendor_gamma_rows == 1


def test_stale_flag_warns_that_agreement_is_vacuous():
    """Every vendor row rejected means the two bases are the SAME number, so
    basis_divergence_pct is 0 by construction and says nothing."""
    rows = [GammaRow("CALL", 1600, 30, 5_000, 0.9, vendor_gamma=0.002,
                     chain_underlying=1594.60)]
    prof = gamma_profile(rows, spot=1400.0)
    assert prof.vendor_gamma_stale is True
    assert prof.basis_divergence_pct == pytest.approx(0.0, abs=1e-9)


def test_chain_underlying_is_carried_on_the_profile():
    rows = [GammaRow("CALL", 1600, 30, 5_000, 0.9, vendor_gamma=0.002,
                     chain_underlying=1594.60)]
    prof = gamma_profile(rows, spot=1594.60)
    assert prof.chain_underlying == pytest.approx(1594.60)
    assert prof.spot_drift_pct == pytest.approx(0.0, abs=1e-9)
