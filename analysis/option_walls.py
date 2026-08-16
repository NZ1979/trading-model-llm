"""Option-chain analytics: OI walls, flow, IV skew — and a spot-consistency
guard that has to run before any of the IV-derived numbers are believed.

Extracted from `scripts/fetch_option_chain.py`, which computed all of this
inline in an inspection script. The computation belongs in a tested module;
the script keeps the printing.

THE SPOT-CONSISTENCY GUARD — read this before using any IV or greek
====================================================================
Discovered 2026-08-16 against live Alpaca OPRA data, and it is the third
member of the family already documented in `SESSION_RESUME_2026-08-14.md`
("both produced confident, plausible, wrong output before being caught").

Vendors compute implied volatility and greeks against the underlying's
**current last price**, not against the underlying that prevailed when the
option was quoted. Those two are the same during regular hours and diverge
badly outside them: US options stop quoting at 16:00 ET while equities print
until 20:00, so every evening and all weekend the option quotes are anchored
to one spot and the IV is computed against another.

Measured on SNDK, 2026-08-16 (Sunday):

    equity last              1657.00   (age 46.7h)
    spot implied by parity   1642.61   (522 pairs, stdev 2.51)
    divergence                 14.39   = 0.88%

    resulting call/put IV gap at the SAME strike and expiry:
        5 DTE   20-22 vol points
       12 DTE   12-13
       19 DTE    9-10
       26 DTE    7-8
       40 DTE      6

The decay is the fingerprint. A fixed dollar error divided by vega: short
expiries have little vega so the same error becomes an enormous vol error,
long expiries absorb it. Genuine put demand does not decay as 1/sqrt(T).

A 25-delta risk reversal computed off that data reads as extreme put skew —
panic pricing — when the true number is near zero. Same shape as the 0DTE
volume/OI artifact and the IV-explosion-at-expiry trap: arithmetically
correct, confidently wrong, and wrong every day between 16:00 and 20:00 ET.

The guard is self-contained and needs no extra data. Put-call parity on
deep-ITM pairs recovers the spot the options were actually quoted against:

    C - P = S - K*exp(-rT)   =>   S ~= C - P + K

Deep-ITM pairs are used because their parity relationship is nearly
insensitive to volatility, so the estimate is robust. On real data 522 pairs
gave a standard deviation of 2.51 on a $1,642 underlying.

`risk_reversal()` and `iv_skew_curve()` REFUSE to return a number when the
guard fails, rather than returning one with a warning attached. A warning next
to a plausible number gets read past; an absent number does not.

THE GUARD VALIDATES, IT DOES NOT REPAIR. Recovering the correct spot does not
correct the vendor's IV, which was already computed against the wrong one.
Always build the check from the PRINTED spot; building it from the recovered
one silences the guard while leaving the distortion in place, which is worse
than no guard at all because the output then looks checked.

This is enforced by API shape rather than by asking the caller to remember:
`risk_reversal` and `iv_skew_curve` take a `SpotCheck`, not a bare spot, and
read the underlying out of it. There is no argument through which a corrected
spot can be slipped past the guard by accident.

Units
=====
Schwab reports implied volatility in PERCENT (86.3). Alpaca reports it as a
DECIMAL (0.863). Silently mixing them is a 100x error in a field nobody
eyeballs. `ChainRow` normalises to DECIMAL and the adapters are the only
place the conversion happens.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

Side = Literal["CALL", "PUT"]

# Divergence beyond this fraction of spot marks IV and greeks untrustworthy.
# During regular hours option and equity quotes are seconds apart and the
# divergence is pennies; the SNDK weekend case was 0.88%. 0.25% sits clearly
# between the two rather than being tuned to either.
DEFAULT_SPOT_TOLERANCE = 0.0025

# Deep-ITM cutoff for the parity estimate: strikes below this fraction of spot
# for calls (and above its reciprocal for puts). Far enough that the extrinsic
# value is small and the parity relation barely depends on vol.
DEEP_ITM_FRACTION = 0.70


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One contract, normalised across vendors.

    `iv` is ALWAYS a decimal (0.863), never percent. `open_interest` is None
    when the source cannot supply it — Alpaca's market data API has no OI
    field at all, and None must not be read as zero.
    """

    symbol: str
    put_call: Side
    strike: float
    expiration: str
    dte: int
    bid: float | None
    ask: float | None
    volume: int
    open_interest: int | None
    iv: float | None
    delta: float | None
    multiplier: float
    quote_age_ms: float | None
    source: str

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def is_call(self) -> bool:
        return self.put_call == "CALL"

    @property
    def volume_oi_ratio(self) -> float | None:
        """None when OI is zero or absent — not zero, and not infinity.

        A contract with no prior open interest has no ratio. Returning 0.0
        would rank it bottom and returning inf would rank it top; both are
        assertions the data does not support.
        """
        if not self.open_interest:
            return None
        return self.volume / self.open_interest


def from_schwab(c) -> ChainRow:
    """Adapt data.schwab_chains.OptionContract. Divides IV by 100."""
    return ChainRow(
        symbol=c.symbol, put_call=c.put_call, strike=c.strike,
        expiration=c.expiration, dte=c.days_to_expiration,
        bid=c.bid or None, ask=c.ask or None,
        volume=c.volume, open_interest=c.open_interest,
        iv=(c.volatility / 100.0) if c.volatility is not None else None,
        delta=c.delta, multiplier=c.multiplier,
        quote_age_ms=None, source="schwab",
    )


def from_alpaca(q, *, dte: int = 0) -> ChainRow:
    """Adapt data.alpaca_rest.OptionQuote.

    `open_interest` is None, permanently: Alpaca's options market data API
    has no such field. See data/alpaca_rest.py.
    """
    return ChainRow(
        symbol=q.symbol, put_call=q.put_call, strike=q.strike,
        expiration=q.expiration, dte=dte,
        bid=q.bid, ask=q.ask, volume=0, open_interest=None,
        iv=q.implied_volatility, delta=q.delta, multiplier=100.0,
        quote_age_ms=q.quote_age_ms, source="alpaca",
    )


# ---------------------------------------------------------------- spot guard

@dataclass(frozen=True, slots=True)
class SpotCheck:
    """Result of comparing the printed underlying against parity-implied spot."""

    printed_spot: float | None
    implied_spot: float | None
    n_pairs: int
    stdev: float | None
    tolerance: float
    est_iv_error_vol_points: float | None
    front_dte: int | None

    @property
    def divergence(self) -> float | None:
        if self.printed_spot is None or self.implied_spot is None:
            return None
        return self.printed_spot - self.implied_spot

    @property
    def divergence_pct(self) -> float | None:
        d = self.divergence
        if d is None or not self.implied_spot:
            return None
        return d / self.implied_spot

    @property
    def trustworthy(self) -> bool:
        """False also when the estimate could not be made.

        Unknown is treated as untrustworthy deliberately. The alternative —
        defaulting to trustworthy when there is no evidence — is how a stale
        spot silently reaches a skew calculation.
        """
        pct = self.divergence_pct
        if pct is None:
            return False
        return abs(pct) <= self.tolerance

    def explain(self) -> str:
        if self.implied_spot is None:
            return (
                f"Spot consistency UNKNOWN: only {self.n_pairs} deep-ITM "
                f"put/call pairs available, need at least 3. IV and greeks "
                f"are NOT verified and are treated as untrustworthy. Usual "
                f"cause: the chain was fetched with a narrow strike window "
                f"around at-the-money, so it contains no strikes below "
                f"{DEEP_ITM_FRACTION:.0%} of spot for parity to work on. "
                f"Refetch without a strike_count limit, or widen it, if you "
                f"need the IV-derived numbers."
            )
        if self.trustworthy:
            return (f"Spot consistent: printed {self.printed_spot:.2f} vs "
                    f"parity-implied {self.implied_spot:.2f} "
                    f"({self.divergence:+.2f}). IV and greeks usable.")
        est = ""
        if self.est_iv_error_vol_points is not None:
            est = (f" Estimated IV distortion at the front expiry "
                   f"({self.front_dte}d): ~{self.est_iv_error_vol_points:.0f} "
                   f"vol points.")
        return (
            f"SPOT MISMATCH: printed underlying {self.printed_spot:.2f} but "
            f"options were quoted against {self.implied_spot:.2f} "
            f"({self.divergence:+.2f}, {self.divergence_pct * 100:+.2f}%, "
            f"from {self.n_pairs} pairs, stdev {self.stdev:.2f}). IV and "
            f"greeks are computed against the printed spot and are therefore "
            f"WRONG.{est} Usual cause: options stopped quoting at 16:00 ET "
            f"while the equity kept printing."
        )


def implied_spot_from_parity(
    rows: Sequence[ChainRow],
    *,
    deep_itm_fraction: float = DEEP_ITM_FRACTION,
    reference_spot: float | None = None,
) -> tuple[float | None, int, float | None]:
    """Recover the spot the options were quoted against, via put-call parity.

    Returns ``(implied_spot, n_pairs, stdev)``. ``implied_spot`` is None when
    fewer than 3 usable pairs exist — a median of one or two pairs is not an
    estimate, and returning one anyway would give the guard false confidence.

    Deep-ITM pairs only: their parity relation is nearly vol-independent, so
    a wrong vol assumption cannot bias the result. Discount factors are
    ignored; over the weeks-to-expiry horizon this is worth cents against a
    divergence measured in dollars.
    """
    pairs: dict[tuple[str, float], dict[str, ChainRow]] = {}
    for r in rows:
        pairs.setdefault((r.expiration, r.strike), {})[r.put_call] = r

    ref = reference_spot
    if ref is None:
        strikes = sorted({r.strike for r in rows})
        ref = statistics.median(strikes) if strikes else None
    if not ref:
        return None, 0, None

    estimates: list[float] = []
    for (_exp, strike), pair in pairs.items():
        call, put = pair.get("CALL"), pair.get("PUT")
        if call is None or put is None:
            continue
        if strike > ref * deep_itm_fraction:
            continue
        cm, pm = call.mid, put.mid
        if cm is None or pm is None:
            continue
        estimates.append(cm - pm + strike)

    if len(estimates) < 3:
        return None, len(estimates), None
    return (statistics.median(estimates), len(estimates),
            statistics.pstdev(estimates))


def _atm_vega(spot: float, dte: int) -> float | None:
    """Approximate ATM vega per 1.0 of vol (i.e. per 100 vol points).

    Black-Scholes ATM vega is S*sqrt(T)*phi(0). Used only to translate a
    dollar spot error into an approximate vol error for the operator's
    benefit, so an approximation is appropriate — the number is a magnitude,
    not a price.
    """
    if dte <= 0 or spot <= 0:
        return None
    t = dte / 365.0
    return spot * math.sqrt(t) * 0.3989422804014327


def check_spot_consistency(
    rows: Sequence[ChainRow],
    printed_spot: float | None,
    *,
    tolerance: float = DEFAULT_SPOT_TOLERANCE,
) -> SpotCheck:
    """Compare the printed underlying against the parity-implied one.

    Run this BEFORE trusting any IV or greek. Cheap: no extra network call,
    no extra data, arithmetic on the chain already in hand.
    """
    implied, n, sd = implied_spot_from_parity(
        rows, reference_spot=printed_spot)

    front_dte = None
    est_err = None
    live = [r.dte for r in rows if r.dte >= 1]
    if live:
        front_dte = min(live)
    if implied is not None and printed_spot is not None and front_dte:
        vega = _atm_vega(implied, front_dte)
        if vega:
            # Two-sided gap: a spot error of d moves the call's implied vol
            # down by delta*d/vega and the put's up by (1-delta)*d/vega, so
            # the observable call-vs-put gap is about d/vega in total.
            est_err = abs(printed_spot - implied) / vega * 100.0

    return SpotCheck(
        printed_spot=printed_spot, implied_spot=implied, n_pairs=n,
        stdev=sd, tolerance=tolerance,
        est_iv_error_vol_points=est_err, front_dte=front_dte,
    )


# ---------------------------------------------------------------- OI walls

@dataclass(frozen=True, slots=True)
class Wall:
    side: Side
    strike: float
    open_interest: int
    shares: float
    distance_pct: float | None

    @property
    def role(self) -> str:
        """Call walls act as resistance, put walls as support."""
        return "resistance" if self.side == "CALL" else "support"


def detect_oi_walls(
    rows: Sequence[ChainRow],
    spot: float | None = None,
    *,
    side: Side = "CALL",
    top: int = 8,
    max_distance_pct: float | None = None,
) -> list[Wall]:
    """Strikes carrying the most open interest, aggregated across expirations.

    Open interest is a settled, cleared figure — it cannot be spoofed the way
    displayed order-book size can. `analysis/futures_walls.py` carries a
    persistence/anti-spoofing layer for exactly that reason; deliberately NOT
    ported here, because it would guard against a failure mode that cannot
    occur.

    Contracts with no open interest are skipped rather than counted as zero:
    Alpaca cannot supply OI at all, and silently treating its entire chain as
    zero-OI would return an empty wall list that reads as "no walls."
    """
    totals: dict[float, int] = {}
    shares: dict[float, float] = {}
    for r in rows:
        if r.put_call != side or r.open_interest is None:
            continue
        if max_distance_pct is not None and spot:
            if abs(r.strike / spot - 1.0) > max_distance_pct:
                continue
        totals[r.strike] = totals.get(r.strike, 0) + r.open_interest
        shares[r.strike] = (shares.get(r.strike, 0.0)
                            + r.open_interest * r.multiplier)

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:top]
    return [
        Wall(side=side, strike=k, open_interest=oi, shares=shares[k],
             distance_pct=((k / spot - 1.0) * 100.0) if spot else None)
        for k, oi in ranked
    ]


def put_call_oi_ratio(rows: Sequence[ChainRow]) -> float | None:
    """Total put OI over total call OI. None when calls have no OI."""
    calls = sum(r.open_interest or 0 for r in rows if r.is_call)
    puts = sum(r.open_interest or 0 for r in rows if not r.is_call)
    if not calls:
        return None
    return puts / calls


# -------------------------------------------------------------------- flow

def flow_outliers(
    rows: Sequence[ChainRow],
    *,
    min_volume: int = 100,
    min_open_interest: int = 250,
    min_dte: int = 1,
    top: int = 8,
) -> list[ChainRow]:
    """Contracts whose volume is large against existing open interest.

    A ratio above 1 means more contracts changed hands today than existed at
    yesterday's close: new positioning rather than existing.

    The defaults are the guards learned the hard way. Open interest is a T+1
    figure, so a contract expiring today has OI near zero by construction and
    ANY volume yields an enormous ratio. The first flow table built without
    these ranked entirely on 0DTE churn, showed `v/OI 1882`, and read as
    massive put accumulation.
    """
    flow = [
        r for r in rows
        if r.volume_oi_ratio is not None
        and r.volume >= min_volume
        and (r.open_interest or 0) >= min_open_interest
        and r.dte >= min_dte
    ]
    flow.sort(key=lambda r: -(r.volume_oi_ratio or 0.0))
    return flow[:top]


def zero_dte_volume(rows: Sequence[ChainRow], *, min_volume: int = 100
                    ) -> tuple[int, int, list[ChainRow]]:
    """Same-day expiry volume, ranked on RAW VOLUME, never on v/OI.

    0DTE volume is genuinely informative; the ratio is not. Returned
    separately so it cannot contaminate `flow_outliers`.
    """
    zero = [r for r in rows if r.dte < 1 and r.volume >= min_volume]
    zero.sort(key=lambda r: -r.volume)
    calls = sum(r.volume for r in zero if r.is_call)
    puts = sum(r.volume for r in zero if not r.is_call)
    return calls, puts, zero


# -------------------------------------------------------------------- skew

@dataclass(frozen=True, slots=True)
class RiskReversal:
    expiration: str
    dte: int
    put_iv: float
    call_iv: float
    put_strike: float
    call_strike: float
    put_delta: float
    call_delta: float

    @property
    def value_vol_points(self) -> float:
        """Put IV minus call IV, in vol points. Positive = put skew."""
        return (self.put_iv - self.call_iv) * 100.0

    @property
    def direction(self) -> str:
        return "put skew" if self.value_vol_points > 0 else "call skew"


def risk_reversal(
    rows: Sequence[ChainRow],
    spot_check: SpotCheck,
    *,
    target_delta: float = 0.25,
) -> RiskReversal | None:
    """25-delta risk reversal at the nearest usable expiration.

    Returns None — not a flagged number — when the spot-consistency guard
    fails. A warning printed beside a plausible-looking figure gets read past;
    an absent figure cannot be. Equities normally carry put skew, so a call
    skew reading is a genuine signal and must not be manufacturable by a
    stale spot.

    Takes a `SpotCheck` rather than a bare spot on purpose, and reads the
    underlying out of it. Build that check from the PRINTED last price — the
    one the vendor used to compute `iv`. Building it from the parity-recovered
    spot silences the guard WITHOUT correcting anything, because the IV values
    in the rows were already computed against the printed spot and stay
    distorted. Demonstrated on real SNDK data 2026-08-16: feeding the
    recovered spot back in produced a confident +10.6 vol-point reading built
    entirely on IVs the guard had just rejected.

    Correcting the skew for real means re-implying volatility from the option
    prices against the recovered spot. This module does not do that, and the
    honest output until it does is None.

    Never uses the same-day expiry: implied vol diverges mechanically as time
    to expiry approaches zero, which produced a delta +0.975 call quoting IV
    245 the first time this was run.

    Out-of-the-money contracts only, the standard skew convention: ITM
    contracts carry the same IV by parity and merely duplicate the curve.
    """
    spot = spot_check.printed_spot
    if not spot or not spot_check.trustworthy:
        return None

    usable = [r for r in rows if r.dte >= 1 and r.iv is not None
              and r.delta is not None]
    if not usable:
        return None
    front_dte = min(r.dte for r in usable)
    near = [r for r in usable if r.dte == front_dte]

    puts = [r for r in near if not r.is_call and r.strike <= spot]
    calls = [r for r in near if r.is_call and r.strike >= spot]
    if not puts or not calls:
        return None

    p = min(puts, key=lambda r: abs(abs(r.delta) - target_delta))
    c = min(calls, key=lambda r: abs(abs(r.delta) - target_delta))
    return RiskReversal(
        expiration=p.expiration, dte=front_dte,
        put_iv=p.iv, call_iv=c.iv,
        put_strike=p.strike, call_strike=c.strike,
        put_delta=p.delta, call_delta=c.delta,
    )


def iv_skew_curve(
    rows: Sequence[ChainRow],
    spot_check: SpotCheck,
) -> list[ChainRow]:
    """OTM contracts at the nearest usable expiration, sorted by strike.

    Empty when the spot guard fails, for the same reason `risk_reversal`
    returns None. Takes a `SpotCheck` rather than a bare spot for the same
    reason too — see that function's note.
    """
    spot = spot_check.printed_spot
    if not spot or not spot_check.trustworthy:
        return []

    usable = [r for r in rows if r.dte >= 1 and r.iv is not None]
    if not usable:
        return []
    front_dte = min(r.dte for r in usable)
    rows_out = [
        r for r in usable
        if r.dte == front_dte
        and ((not r.is_call and r.strike <= spot)
             or (r.is_call and r.strike >= spot))
    ]
    return sorted(rows_out, key=lambda r: r.strike)
