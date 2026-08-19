"""Dealer gamma exposure by strike, and the gamma flip point.

`analysis/option_walls.py` answers WHERE open interest sits. This answers what
that open interest does to price when spot moves — which is a different
question and, on 2026-08-19, the more useful one.

    from analysis.gamma_exposure import gamma_profile, from_stored
    prof = gamma_profile(from_stored(rows), spot=1594.6)
    print(prof.flip, prof.total_gex, prof.band_summary())

THE ASSUMPTION THIS ENTIRE MODULE RESTS ON
------------------------------------------
Public data says how much open interest exists at each strike. It does NOT say
who is long and who is short. Every dealer-gamma model therefore assumes a
convention, and this one uses the common retail-flow convention:

    dealers are LONG calls and SHORT puts

which makes call gamma positive and put gamma negative in the sum. The
reasoning is that customers predominantly sell covered calls and buy protective
puts, leaving the dealer on the other side. It is a heuristic, not a fact, and
it is wrong for any name where flow runs the other way.

Treat the FLIP LEVEL as the output worth trusting and the SIGN CONVENTION as
the thing to falsify. On 2026-08-19 SNDK this convention produced a flip at
~1570 from the 08-14 chain; the session low printed 1570.00 and the level held
three separate tests before breaking. That is one observation, not validation.

WHAT POSITIVE AND NEGATIVE GAMMA MEAN
-------------------------------------
A dealer who is long gamma gets longer as price falls and shorter as it rises,
so staying hedged means buying dips and selling rallies. That damps moves —
like a brake.

A dealer who is short gamma must do the opposite: sell as price falls, buy as
it rises. That amplifies moves — like a hand in the back. The flip point is
where the sum crosses zero and the behaviour inverts.

TWO GAMMAS, DELIBERATELY
------------------------
Vendor gamma (Schwab supplies it per contract) is the vendor's own model,
consistent with the delta and IV they publish, and is the right input for
exposure AT THE CURRENT SPOT. It is useless for the flip search, because a
gamma computed at spot 1594 does not describe the same contract at spot 1500.

So the flip solver recomputes gamma from Black-Scholes at each candidate spot.
`cross_check()` compares the two at current spot; a large divergence means the
vendor is using a different rate, dividend or day-count than this module and
the flip level inherits that difference.

GUARDS
------
- 0DTE is excluded by default. Gamma diverges as time to expiry approaches
  zero, and OI on a same-day expiry is near zero by construction (repo trap
  #1). Both together produce enormous meaningless numbers.
- Contracts with no OI or no IV are counted and reported, never silently
  treated as zero.
- The profile carries `contracts_used` and `strike_span` so a GEX computed
  from a windowed chain cannot be read as a whole-chain figure (repo trap #5).
  A GEX from a +/-40 fetch is a different number from a +/-200 fetch on the
  same chain, for the same reason the walls were.
- IV is held FIXED while spot is varied in the flip search. Real surfaces
  shift as spot moves, so the flip is an estimate under a frozen surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

Side = Literal["CALL", "PUT"]

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_gamma(spot: float, strike: float, t_years: float, iv: float,
             rate: float = 0.0) -> float | None:
    """Black-Scholes gamma. None when the inputs cannot produce one.

    Returns None rather than 0.0 for degenerate inputs: a contract whose gamma
    is unknowable must not contribute zero to a sum that is then reported as
    complete.
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
        return None
    vol_t = iv * math.sqrt(t_years)
    if vol_t <= 0:
        return None
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol_t
    return _norm_pdf(d1) / (spot * vol_t)


@dataclass(frozen=True, slots=True)
class GammaRow:
    """Minimal contract shape this module needs. Vendor gamma optional."""

    put_call: Side
    strike: float
    dte: int
    open_interest: int | None
    iv: float | None            # decimal, never percent
    multiplier: float = 100.0
    vendor_gamma: float | None = None
    chain_underlying: float | None = None   # spot the vendor gamma assumed

    @property
    def t_years(self) -> float:
        return self.dte / 365.0


def from_stored(rows: Iterable) -> list[GammaRow]:
    """Adapt sqlite3.Row objects from data/chain_store.chain_snapshots.

    That table stores `volatility` as Schwab publishes it — percent — so it is
    divided here, matching analysis/option_walls.from_schwab.
    """
    out = []
    for r in rows:
        vol = r["volatility"]
        out.append(GammaRow(
            put_call=r["put_call"], strike=r["strike"],
            dte=r["days_to_expiration"] or 0,
            open_interest=r["open_interest"],
            iv=(vol / 100.0) if vol else None,
            multiplier=r["multiplier"] or 100.0,
            vendor_gamma=r["gamma"],
            chain_underlying=r["underlying_price"],
        ))
    return out


@dataclass(frozen=True, slots=True)
class StrikeGamma:
    strike: float
    call_gex: float
    put_gex: float

    @property
    def net_gex(self) -> float:
        return self.call_gex + self.put_gex


@dataclass(frozen=True, slots=True)
class GammaProfile:
    """Gamma exposure across strikes at one spot, plus the flip estimate.

    `total_gex` is dollars of dealer delta per 1% move in spot, under the
    convention documented at the top of this module.
    """

    spot: float
    by_strike: tuple[StrikeGamma, ...]
    total_gex: float          # vendor gamma when available
    total_gex_bs: float       # same sum, Black-Scholes gamma throughout
    flip: float | None        # ALWAYS Black-Scholes
    flip_searched: tuple[float, float]

    contracts_used: int
    vendor_gamma_rows: int          # rows whose vendor gamma was fresh enough
    chain_underlying: float | None  # spot the chain published against
    spot_drift_pct: float | None    # how far spot has moved from it
    contracts_skipped_zero_dte: int
    contracts_skipped_no_oi: int
    contracts_skipped_no_iv: int
    strike_span: tuple[float, float] | None
    used_vendor_gamma: bool

    @property
    def regime(self) -> str:
        """Regime AT THIS SPOT, read from the sign of net exposure.

        Deliberately NOT `spot > flip`. The flip search runs over a window
        relative to spot, so when spot sits far from the crossing the flip
        falls outside its own search range and comes back None — which would
        report "unknown" for a profile whose regime is not in doubt. The sign
        of net GEX at spot is the definition; the flip is where that sign
        would change. Derive from the definition.
        """
        if self.total_gex > 0:
            return "positive (dampening)"
        if self.total_gex < 0:
            return "negative (amplifying)"
        return "neutral"

    @property
    def vendor_gamma_stale(self) -> bool:
        """True when spot has drifted far enough that no vendor gamma was used.

        Not an error — the profile is still correct, computed entirely from
        Black-Scholes. It means `total_gex` and `total_gex_bs` are the same
        number and `basis_divergence_pct` is 0 by construction, so the absence
        of disagreement says nothing about whether the models agree.
        """
        return self.vendor_gamma_rows == 0 and self.used_vendor_gamma

    @property
    def basis_divergence_pct(self) -> float | None:
        """How far the vendor-gamma total sits from the Black-Scholes total.

        `total_gex` and `flip` come from DIFFERENT gamma sources, and they can
        contradict each other. Measured on the 2026-08-19 SNDK chain: at spot
        1590.32 the Black-Scholes flip says net exposure is zero while the
        vendor-gamma total reads +11.7M. Both cannot be right. This number is
        the size of that disagreement; `cross_check()` explains where it comes
        from. Neither basis is verifiable from outside, so the gap is reported
        rather than resolved.
        """
        if not self.total_gex_bs:
            return None
        return (self.total_gex - self.total_gex_bs) / abs(self.total_gex_bs) * 100

    @property
    def regime_bases_agree(self) -> bool | None:
        """False when the two gamma bases imply different regimes at spot.

        A False here means the flip level should not be treated as precise:
        the two models disagree about which side of it you are on.
        """
        if self.flip is None:
            return None
        by_total = self.total_gex > 0
        by_flip = self.spot > self.flip
        return by_total == by_flip

    @property
    def flip_outside_window(self) -> bool:
        """True when no crossing was found in the searched range.

        Distinguishes "this chain has no flip nearby" from "the search was
        too narrow to find it" — the caller should widen flip_lo_pct /
        flip_hi_pct before concluding the former.
        """
        return self.flip is None

    def band_summary(self, edges: Sequence[float] | None = None
                     ) -> list[tuple[float, float, float]]:
        """Net GEX bucketed into price bands. Returns (lo, hi, net)."""
        if not self.by_strike:
            return []
        strikes = [s.strike for s in self.by_strike]
        if edges is None:
            lo, hi = min(strikes), max(strikes)
            step = (hi - lo) / 8 or 1.0
            edges = [lo + i * step for i in range(9)]
        out = []
        for a, b in zip(edges, edges[1:]):
            net = sum(s.net_gex for s in self.by_strike if a <= s.strike < b)
            out.append((a, b, net))
        return out

    def nearest_walls(self, top: int = 5) -> list[StrikeGamma]:
        """Strikes with the largest absolute net gamma."""
        return sorted(self.by_strike, key=lambda s: -abs(s.net_gex))[:top]


DEFAULT_MAX_DRIFT_PCT = 0.5


def vendor_gamma_usable(row: GammaRow, spot: float,
                        max_drift_pct: float = DEFAULT_MAX_DRIFT_PCT) -> bool:
    """Whether this row's vendor gamma still describes the contract at `spot`.

    Vendor gamma is published against the underlying price in that same chain
    fetch. Once spot moves away from it the number describes a contract that no
    longer exists, and using it anyway produces a confident wrong answer rather
    than a missing one.

    Measured 2026-08-19 on SNDK: a chain fetched at 04:45 carried gamma against
    an underlying of 1594.60. Evaluated at a spot of 1561.94 — 2.1% away — the
    vendor basis reported +3.5M (dampening) while Black-Scholes reported -30.1M
    (amplifying). Opposite regimes, from the same open interest. Re-fetching the
    chain at the live spot collapsed the disagreement to nothing: both bases
    then read negative. Most of that "model divergence" was staleness.

    The 0.5% default is a judgement, not a measured optimum: tight enough that
    a 2% drift can never repeat, loose enough that a fresh chain still gets to
    use the vendor's own internally consistent model.
    """
    if not row.vendor_gamma or not row.chain_underlying:
        return False
    drift = abs(spot - row.chain_underlying) / row.chain_underlying * 100
    return drift <= max_drift_pct


def _contract_gex(row: GammaRow, spot: float, rate: float,
                  use_vendor: bool,
                  max_drift_pct: float = DEFAULT_MAX_DRIFT_PCT) -> float | None:
    if not row.open_interest:
        return None
    if use_vendor and vendor_gamma_usable(row, spot, max_drift_pct):
        g = row.vendor_gamma
    else:
        g = bs_gamma(spot, row.strike, row.t_years, row.iv or 0.0, rate)
    if g is None:
        return None
    # dollars of dealer delta per 1% move in spot
    mag = g * row.open_interest * row.multiplier * spot * spot * 0.01
    return mag if row.put_call == "CALL" else -mag


def gamma_profile(rows: Sequence[GammaRow], spot: float, *,
                  min_dte: int = 1, rate: float = 0.0,
                  use_vendor_gamma: bool = True,
                  max_drift_pct: float = DEFAULT_MAX_DRIFT_PCT,
                  flip_lo_pct: float = 0.75,
                  flip_hi_pct: float = 1.25) -> GammaProfile:
    """Gamma exposure by strike at `spot`, plus a flip-point estimate.

    `min_dte=1` excludes same-day expiries. `use_vendor_gamma` applies only to
    the at-spot exposure; the flip search always recomputes from Black-Scholes
    because vendor gamma is only valid at the spot it was published for.
    """
    if spot <= 0:
        raise ValueError("spot must be positive")

    unders = [r.chain_underlying for r in rows if r.chain_underlying]
    chain_spot = max(set(unders), key=unders.count) if unders else None
    drift = (abs(spot - chain_spot) / chain_spot * 100) if chain_spot else None

    zero_dte = sum(1 for r in rows if r.dte < min_dte)
    live = [r for r in rows if r.dte >= min_dte]
    no_oi = sum(1 for r in live if not r.open_interest)
    no_iv = sum(1 for r in live if r.open_interest and not r.iv)

    agg: dict[float, list[float]] = {}
    used = 0
    vendor_rows = 0
    for r in live:
        gex = _contract_gex(r, spot, rate, use_vendor_gamma, max_drift_pct)
        if gex is None:
            continue
        used += 1
        if use_vendor_gamma and vendor_gamma_usable(r, spot, max_drift_pct):
            vendor_rows += 1
        c, p = agg.setdefault(r.strike, [0.0, 0.0])
        if r.put_call == "CALL":
            agg[r.strike][0] = c + gex
        else:
            agg[r.strike][1] = p + gex

    by_strike = tuple(sorted(
        (StrikeGamma(k, v[0], v[1]) for k, v in agg.items()),
        key=lambda s: s.strike))
    total = sum(s.net_gex for s in by_strike)
    # Same sum on a pure Black-Scholes basis, so the flip and the total can
    # be compared on like terms. Identical when use_vendor_gamma is False.
    total_bs = _net_at(live, spot, rate)

    lo, hi = spot * flip_lo_pct, spot * flip_hi_pct
    flip = _solve_flip(live, lo, hi, rate)

    strikes = [s.strike for s in by_strike]
    return GammaProfile(
        spot=spot, by_strike=by_strike, total_gex=total,
        total_gex_bs=total_bs, flip=flip, flip_searched=(lo, hi),
        contracts_used=used, vendor_gamma_rows=vendor_rows,
        chain_underlying=chain_spot, spot_drift_pct=drift,
        contracts_skipped_zero_dte=zero_dte,
        contracts_skipped_no_oi=no_oi, contracts_skipped_no_iv=no_iv,
        strike_span=(min(strikes), max(strikes)) if strikes else None,
        used_vendor_gamma=use_vendor_gamma,
    )


def _net_at(rows: Sequence[GammaRow], s: float, rate: float) -> float:
    total = 0.0
    for r in rows:
        gex = _contract_gex(r, s, rate, use_vendor=False)
        if gex is not None:
            total += gex
    return total


def _solve_flip(rows: Sequence[GammaRow], lo: float, hi: float,
                rate: float, steps: int = 48, tol: float = 0.5
                ) -> float | None:
    """Lowest spot in [lo, hi] where net GEX crosses zero.

    Scans coarsely for a sign change then bisects. Returns None when no
    crossing exists in the window — which is a real answer, not a failure: a
    chain can be net-positive or net-negative across the entire searched range.
    """
    if lo >= hi:
        return None
    grid = [lo + (hi - lo) * i / steps for i in range(steps + 1)]
    prev_s, prev_v = grid[0], _net_at(rows, grid[0], rate)
    for s in grid[1:]:
        v = _net_at(rows, s, rate)
        if prev_v == 0.0:
            return prev_s
        if (prev_v < 0) != (v < 0):
            a, b, fa = prev_s, s, prev_v
            while b - a > tol:
                m = (a + b) / 2
                fm = _net_at(rows, m, rate)
                if (fa < 0) != (fm < 0):
                    b = m
                else:
                    a, fa = m, fm
            return (a + b) / 2
        prev_s, prev_v = s, v
    return None


def cross_check(rows: Sequence[GammaRow], spot: float, *,
                min_dte: int = 1, rate: float = 0.0) -> dict:
    """Compare vendor gamma against Black-Scholes gamma at current spot.

    A large divergence means the vendor uses a different rate, dividend
    assumption or day count than this module, and the flip level — which is
    always Black-Scholes — inherits that difference. Reported rather than
    reconciled: there is no way to know which is right from outside.
    """
    diffs, both = [], 0
    for r in rows:
        if r.dte < min_dte or not r.open_interest or not r.vendor_gamma:
            continue
        bs = bs_gamma(spot, r.strike, r.t_years, r.iv or 0.0, rate)
        if bs is None or r.vendor_gamma == 0:
            continue
        both += 1
        diffs.append((bs - r.vendor_gamma) / r.vendor_gamma * 100)
    if not diffs:
        return {"contracts": 0, "note": "no contract had both gammas"}
    diffs.sort()
    n = len(diffs)
    return {
        "contracts": both,
        "median_pct_diff": diffs[n // 2],
        "p10": diffs[n // 10], "p90": diffs[(9 * n) // 10],
        "note": "positive means Black-Scholes gamma exceeds the vendor's",
    }
