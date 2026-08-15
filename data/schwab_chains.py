"""Schwab option chain fetch and parse. Spec: docs/FEED_SPEC_V4.md.

Delivers three of the four options capabilities from one endpoint:

  - OI walls          openInterest per strike
  - flow              totalVolume vs openInterest per strike
  - IV / skew         volatility, delta per strike

Only tick-level intraday gamma needs LEVELONE_OPTIONS streaming, which is a
separate component and hits Schwab's undocumented symbol cap.

Response shape
--------------
Schwab nests contracts three deep:

    callExpDateMap["2026-08-15:1"]["1660.0"] -> contract(s)

The expiration key is `YYYY-MM-DD:daysToExpiration`. The leaf has historically
been a LIST of contracts (multiple roots can share a strike after a corporate
action) but the portal's OpenAPI schema shows an object. Both are handled;
guessing one and being wrong would silently drop contracts.

Filtering
---------
`isNonStandard` and `isMini` contracts are excluded by default. Adjusted
contracts from splits and mergers sit at odd strikes with non-100 multipliers,
and mini options represent 10 shares rather than 100. Folding either into an
open-interest wall silently corrupts it: 1000 contracts of a mini option is
10,000 shares of exposure, not 100,000.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OptionContract:
    """One option contract at one strike and expiration."""

    symbol: str                 # OCC symbol, e.g. "SNDK  260815C01660000"
    underlying: str
    put_call: str               # "CALL" | "PUT"
    strike: float
    expiration: str             # YYYY-MM-DD
    days_to_expiration: int

    bid: float
    ask: float
    last: float
    mark: float
    bid_size: int
    ask_size: int

    volume: int                 # contracts traded today
    open_interest: int          # contracts outstanding, T+1 figure

    volatility: float | None    # implied vol, percent
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None

    in_the_money: bool
    intrinsic_value: float | None
    time_value: float | None
    multiplier: float
    is_penny_pilot: bool
    is_mini: bool
    is_non_standard: bool
    option_root: str

    @property
    def notional_oi_shares(self) -> float:
        """Open interest expressed in underlying shares.

        This is the number that matters for a wall. `multiplier` is normally
        100 but is NOT for mini or adjusted contracts, which is exactly why
        those are filtered by default rather than counted in contracts.
        """
        return self.open_interest * self.multiplier

    @property
    def volume_oi_ratio(self) -> float | None:
        """Today's volume over existing open interest.

        > 1 means more contracts changed hands today than were outstanding at
        the open — new positioning rather than existing. This is the flow
        signal. None when OI is zero, which is not the same as zero ratio.
        """
        if not self.open_interest:
            return None
        return self.volume / self.open_interest


@dataclass
class OptionChain:
    underlying: str
    underlying_price: float | None
    fetched_at: datetime
    contracts: list[OptionContract] = field(default_factory=list)
    is_delayed: bool | None = None
    status: str = ""

    def calls(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.put_call == "CALL"]

    def puts(self) -> list[OptionContract]:
        return [c for c in self.contracts if c.put_call == "PUT"]

    def expirations(self) -> list[str]:
        return sorted({c.expiration for c in self.contracts})


def _f(d: dict, key: str) -> float | None:
    """Float or None. Schwab uses NaN and large sentinels for 'no value'."""
    v = d.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Schwab returns -999.0 for greeks on contracts it cannot price, and NaN
    # shows up on illiquid strikes. Both must become None rather than being
    # averaged into a skew calculation.
    if f != f or f <= -999.0:
        return None
    return f


def _iter_leaf_contracts(node: Any) -> Iterator[dict]:
    """Yield contract dicts from a strike node.

    The leaf is a list in the live API and an object in the OpenAPI schema.
    Handle both; guessing wrong silently drops contracts.
    """
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                yield item
    elif isinstance(node, dict):
        # Either a single contract, or a map of contracts.
        if "putCall" in node or "symbol" in node:
            yield node
        else:
            for item in node.values():
                if isinstance(item, dict):
                    yield item
                elif isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, dict):
                            yield sub


def _parse_expiration_key(key: str) -> tuple[str, int]:
    """'2026-08-15:1' -> ('2026-08-15', 1)."""
    exp, _, dte = key.partition(":")
    try:
        return exp, int(dte)
    except ValueError:
        return exp, -1


def parse_chain(
    payload: dict,
    *,
    include_non_standard: bool = False,
    include_mini: bool = False,
    fetched_at: datetime | None = None,
) -> OptionChain:
    """Flatten a Schwab /chains response into OptionContract records."""
    underlying_block = payload.get("underlying") or {}
    chain = OptionChain(
        underlying=payload.get("symbol") or underlying_block.get("symbol") or "",
        underlying_price=_f(payload, "underlyingPrice")
        or _f(underlying_block, "last"),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        is_delayed=payload.get("isDelayed", underlying_block.get("delayed")),
        status=payload.get("status", ""),
    )

    skipped = {"mini": 0, "non_standard": 0, "unparseable": 0}

    for map_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = payload.get(map_key) or {}
        if not isinstance(exp_map, dict):
            continue
        for exp_key, strike_map in exp_map.items():
            expiration, dte = _parse_expiration_key(str(exp_key))
            if not isinstance(strike_map, dict):
                continue
            for strike_key, node in strike_map.items():
                for raw in _iter_leaf_contracts(node):
                    is_mini = bool(raw.get("isMini"))
                    is_ns = bool(raw.get("isNonStandard"))
                    if is_mini and not include_mini:
                        skipped["mini"] += 1
                        continue
                    if is_ns and not include_non_standard:
                        skipped["non_standard"] += 1
                        continue
                    try:
                        strike = _f(raw, "strikePrice")
                        if strike is None:
                            strike = float(strike_key)
                        chain.contracts.append(OptionContract(
                            symbol=raw.get("symbol", ""),
                            underlying=chain.underlying,
                            put_call=raw.get("putCall", ""),
                            strike=strike,
                            expiration=raw.get("expirationDate", expiration)[:10],
                            days_to_expiration=int(
                                raw.get("daysToExpiration", dte) or dte),
                            bid=_f(raw, "bidPrice") or 0.0,
                            ask=_f(raw, "askPrice") or 0.0,
                            last=_f(raw, "lastPrice") or 0.0,
                            mark=_f(raw, "markPrice") or 0.0,
                            bid_size=int(raw.get("bidSize") or 0),
                            ask_size=int(raw.get("askSize") or 0),
                            volume=int(raw.get("totalVolume") or 0),
                            open_interest=int(raw.get("openInterest") or 0),
                            volatility=_f(raw, "volatility"),
                            delta=_f(raw, "delta"),
                            gamma=_f(raw, "gamma"),
                            theta=_f(raw, "theta"),
                            vega=_f(raw, "vega"),
                            rho=_f(raw, "rho"),
                            in_the_money=bool(raw.get("isInTheMoney")),
                            intrinsic_value=_f(raw, "intrinsicValue"),
                            time_value=_f(raw, "timeValue"),
                            multiplier=_f(raw, "multiplier") or 100.0,
                            is_penny_pilot=bool(raw.get("isPennyPilot")),
                            is_mini=is_mini,
                            is_non_standard=is_ns,
                            option_root=raw.get("optionRoot", ""),
                        ))
                    except (TypeError, ValueError, KeyError):
                        skipped["unparseable"] += 1
                        logger.exception(
                            "Unparseable option contract at %s %s",
                            exp_key, strike_key)

    if any(skipped.values()):
        logger.info("Chain %s: parsed %d contracts, skipped %s",
                    chain.underlying, len(chain.contracts), skipped)
    return chain


def fetch_chain(
    client,
    symbol: str,
    *,
    strike_count: int | None = 40,
    contract_type: str = "ALL",
    from_date: date | None = None,
    to_date: date | None = None,
    include_underlying_quote: bool = True,
    include_non_standard: bool = False,
    include_mini: bool = False,
) -> OptionChain:
    """Fetch and parse one option chain.

    `strike_count` returns N strikes above AND below at-the-money. Defaulting
    to 40 rather than the whole chain matters: walls live near the money, and
    pulling every strike on a liquid name is thousands of contracts per call
    against an undocumented rate limit.

    Raises RuntimeError on a non-200, with the response body included — Schwab
    returns a structured error object that names the offending parameter, and
    swallowing it makes debugging blind (Rule 18).
    """
    kwargs: dict[str, Any] = {}
    if strike_count is not None:
        kwargs["strike_count"] = strike_count
    if contract_type and contract_type != "ALL":
        kwargs["contract_type"] = contract_type
    if from_date is not None:
        kwargs["from_date"] = from_date
    if to_date is not None:
        kwargs["to_date"] = to_date
    if include_underlying_quote is not None:
        kwargs["include_underlying_quote"] = include_underlying_quote

    resp = client.get_option_chain(symbol, **kwargs)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Schwab /chains returned {resp.status_code} for {symbol}: "
            f"{resp.text[:500]}"
        )

    payload = resp.json()
    status = payload.get("status", "")
    if status and status.upper() not in ("SUCCESS", "OK"):
        # A 200 with a non-success status is Schwab telling you the symbol is
        # not optionable, or the filters matched nothing.
        logger.warning("Chain %s returned status=%s", symbol, status)

    chain = parse_chain(
        payload,
        include_non_standard=include_non_standard,
        include_mini=include_mini,
    )
    if chain.is_delayed:
        logger.warning(
            "Chain %s is DELAYED (isDelayed=true). Open interest is a T+1 "
            "figure so walls are still valid, but volume, greeks and IV are "
            "not current.", symbol)
    return chain
