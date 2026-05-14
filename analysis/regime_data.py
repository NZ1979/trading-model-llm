"""Polygon data fetching for the regime classifier.

Separate file from ``analysis/regime.py`` to keep the classifier pure and
I/O-free. The classifier is unit-testable without network; this module is
the thin async adapter that produces its inputs from real Polygon data.

Three responsibilities:

1. Pull ~70 trading days of SPY daily bars (covers both the 20-day return
   and the 50-day SMA used as the breadth proxy).
2. Pull ~70 trading days of VIX daily bars from Polygon's index endpoint.
   Polygon serves indices under the ``I:`` prefix, e.g. ``I:VIX``. VIX
   may not be available on the Stocks Starter plan; on any failure we
   return ``vix_level=None`` and ``vix_60d_median=None`` and the
   classifier degrades gracefully per its design.
3. Compute the three scalars the classifier consumes and return a
   ``RegimeInputs`` instance.

Fail-loud priority per Rule 18:

- SPY fetch failure → raise. SPY is core; without it there is no regime
  signal at all and we should NOT silently fall back to ``"unknown"``.
- VIX fetch failure → log warning, return ``None`` for VIX fields. The
  classifier already handles VIX-None and degrades to a 4-bucket
  classification (no ``crash`` reachable, but ``trending_up``,
  ``trending_down``, and ``choppy`` all still produce real labels).
- Insufficient SPY history (fewer than 50 bars) → raise. The 50-day
  SMA is required for the breadth proxy; if we can't compute it we
  cannot honestly produce a regime.

Point-in-time correctness (per ``M2_REPLAY_HARNESS_DESIGN.md`` § 1):
the ``as_of`` parameter is the last day whose close we are allowed to
see. The function pulls bars with ``to_date = as_of`` and computes
everything against bars strictly through that date. The M2 harness uses
this to walk historical days without leaking future information into a
regime label.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from analysis.regime import RegimeInputs

if TYPE_CHECKING:  # avoid runtime import cycle / heavy imports on test paths
    from data.polygon_feed import PolygonRESTClient

logger = logging.getLogger(__name__)


# How many calendar days back to ask Polygon for, given a trading-day
# requirement. 5 trading days ≈ 7 calendar days; we add a generous buffer
# for long weekends + market holidays. Over-fetching is cheap; under-
# fetching means the median or SMA quietly uses a too-short window.
_TRADING_DAYS_PER_WEEK = 5
_CALENDAR_BUFFER_DAYS = 14   # absorbs ~2 weeks of holidays in any window
_REGIME_TRADING_DAYS_NEEDED = 60   # max(20-day return, 50-day SMA, 60-day median)


def _calendar_days_for(trading_days: int) -> int:
    """Convert a trading-day count to a safe calendar-day lookback."""
    return int(trading_days * 7 / _TRADING_DAYS_PER_WEEK) + _CALENDAR_BUFFER_DAYS


def _bars_to_closes(bars: list[dict]) -> list[float]:
    """Extract close prices in chronological order. Polygon returns
    ``sort=asc`` so this is just a column projection."""
    return [float(b["c"]) for b in bars if "c" in b]


def _median(xs: list[float]) -> float | None:
    """Plain median without numpy. Returns None for empty input.

    Used instead of statistics.median to keep behavior explicit on the
    even-count tie-break (average of the two middle values, which is
    statistics.median's default and what we want)."""
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


@dataclass(frozen=True, slots=True)
class _SPYStats:
    """Internal: scalars we derive from the SPY daily-bar series."""
    last_close: float
    return_20d: float
    sma_50: float


def _compute_spy_stats(closes: list[float]) -> _SPYStats:
    """Compute the three SPY-derived scalars from a chronological close
    series. Raises ValueError when there's insufficient history.

    The series is expected to end at the as-of date's close (closes[-1]).
    The 20-day return is closes[-1] / closes[-21] - 1; the 50-day SMA
    is the simple mean of the trailing 50 closes including today."""
    if len(closes) < 50:
        raise ValueError(
            f"need ≥50 SPY daily closes for the 50-day SMA; got {len(closes)}"
        )
    if len(closes) < 21:
        # Defensive: 50 ≥ 21, but the check makes the dependency
        # explicit and survives any future refactor that splits the
        # 50-day and 20-day fetches.
        raise ValueError(
            f"need ≥21 SPY daily closes for the 20-day return; got {len(closes)}"
        )
    last_close = closes[-1]
    close_20d_ago = closes[-21]
    return_20d = (last_close / close_20d_ago) - 1.0
    sma_50 = sum(closes[-50:]) / 50.0
    return _SPYStats(last_close=last_close, return_20d=return_20d, sma_50=sma_50)


async def _fetch_spy_closes(
    client: "PolygonRESTClient",
    as_of: date,
    trading_days_needed: int,
) -> list[float]:
    """Fetch SPY daily closes through as_of, oldest-first."""
    end = as_of
    start = end - timedelta(days=_calendar_days_for(trading_days_needed))
    bars = await client.get_daily_aggregates(
        symbol="SPY",
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        adjusted=True,
    )
    if not bars:
        raise RuntimeError(
            f"Polygon returned no SPY bars for {start.isoformat()}..{end.isoformat()}"
        )
    return _bars_to_closes(bars)


async def _fetch_vix_closes(
    client: "PolygonRESTClient",
    as_of: date,
    trading_days_needed: int,
) -> list[float] | None:
    """Fetch VIX daily closes through as_of, oldest-first. Returns
    ``None`` on any failure — VIX is optional, the classifier degrades
    gracefully."""
    end = as_of
    start = end - timedelta(days=_calendar_days_for(trading_days_needed))
    try:
        bars = await client.get_daily_aggregates(
            symbol="I:VIX",
            from_date=start.isoformat(),
            to_date=end.isoformat(),
            adjusted=True,
        )
    except Exception as exc:
        logger.warning(
            "VIX fetch failed (%s); classifier will run in VIX-less degraded mode",
            exc,
        )
        return None
    if not bars:
        logger.warning(
            "VIX fetch returned no bars for %s..%s; classifier will run in "
            "VIX-less degraded mode (likely a plan-tier coverage gap)",
            start.isoformat(),
            end.isoformat(),
        )
        return None
    return _bars_to_closes(bars)


async def fetch_regime_inputs(
    client: "PolygonRESTClient",
    as_of: date | None = None,
) -> RegimeInputs:
    """Build a ``RegimeInputs`` for the regime classifier as of a given date.

    Parameters
    ----------
    client
        A live ``PolygonRESTClient`` (created with a valid
        ``POLYGON_API_KEY``). The function awaits ``client.get_daily_aggregates``
        twice — once for SPY, once for ``I:VIX``.
    as_of
        The reference date for the regime label. All inputs are computed
        from bars ≤ ``as_of``. Defaults to today (UTC) when omitted; the
        M2 replay harness passes a historical date here.

    Returns
    -------
    RegimeInputs
        Ready to pass to ``classify_regime``. ``vix_level`` and
        ``vix_60d_median`` are ``None`` when VIX is unavailable; the
        SPY-derived fields are always populated or the function raises.

    Raises
    ------
    RuntimeError
        SPY data unavailable from Polygon.
    ValueError
        Insufficient SPY history (fewer than 50 daily bars in the
        requested window). This generally indicates a vendor-side gap
        rather than a logic error; the caller should surface the date
        and retry rather than silently degrading.

    Notes
    -----
    ``fetch_regime_inputs`` does NOT cache. Callers that need to hit
    this many times (e.g., the M2 replay walking 250 trading days)
    should layer a date-keyed dict in front of it. The cache key is
    just ``as_of`` because Polygon's daily bars are immutable once the
    session has closed.
    """
    if as_of is None:
        as_of = datetime.utcnow().date()

    spy_closes = await _fetch_spy_closes(
        client, as_of, _REGIME_TRADING_DAYS_NEEDED
    )
    vix_closes = await _fetch_vix_closes(
        client, as_of, _REGIME_TRADING_DAYS_NEEDED
    )

    spy = _compute_spy_stats(spy_closes)
    breadth_proxy = (spy.last_close / spy.sma_50) - 1.0

    if vix_closes is None or len(vix_closes) < 30:
        # Either Polygon returned no VIX coverage or fewer than 30 obs
        # in the lookback window. 30 is the minimum for a median to
        # mean anything; below that we degrade to no-VIX rather than
        # report a meaninglessly noisy ratio.
        vix_level: float | None = None
        vix_median: float | None = None
        if vix_closes is not None:
            logger.warning(
                "VIX series has only %d obs (< 30); running in VIX-less mode",
                len(vix_closes),
            )
    else:
        vix_level = vix_closes[-1]
        # Use the trailing 60 obs (or whatever we have, if fewer); we
        # already guarded against < 30 above.
        vix_median = _median(vix_closes[-60:])

    return RegimeInputs(
        spy_return_20d=spy.return_20d,
        vix_level=vix_level,
        vix_60d_median=vix_median,
        breadth_proxy=breadth_proxy,
    )


__all__ = ["fetch_regime_inputs"]
