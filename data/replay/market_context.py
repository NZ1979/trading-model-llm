"""SPY + VIX market context loader for the replay harness.

Populates the market-context fields of ``LLMContext``:
``spy_change_pct``, ``spy_rvol``, ``vix_level``. Also feeds the regime
classifier (``analysis/regime.py``) which produces
``market_regime_label``.

VIX source: ``data/fred_vix.py`` (FRED ``VIXCLS`` daily close). Polygon
Stocks Starter does not include indices (I:VIX returns 403, verified
2026-05-13); FRED publishes the same daily close one business day later,
free, with a stable 30-year API contract. Daily granularity is sufficient
for the regime classifier's 60-day median comparison.

Status: M2.2. VIX helper wired below; ``load_market_data`` still raises
``NotImplementedError`` until the SPY half lands in the next sub-task,
at which point the two halves combine into a single atomic
implementation (no half-built returns per Rule 18).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from data import fred_vix

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketContextBundle:
    """SPY + VIX data bundle for one replay run.

    Loaded once at run start, sliced per-tick by the replay loop. Shape
    parallels what the regime classifier's data layer expects so the
    same DataFrames can feed both the LLMContext builder and the
    regime classifier.
    """

    spy_5min: pd.DataFrame  # 5-min bars over the replay range
    spy_daily: pd.DataFrame  # daily bars, 300+ days back from start_date
    vix_daily: pd.DataFrame | None  # None when VIX source is unavailable


def load_market_data(start_date: date, end_date: date) -> MarketContextBundle:
    """Load SPY (5-min + daily) and VIX (daily) over a date range.

    SPY daily extends 300 trading days BEFORE ``start_date`` to give
    the regime classifier its required warmup window. Caller does not
    need to extend the range — this loader handles the prepad
    internally.

    VIX is best-effort: if Polygon returns 403 (or any other failure
    that isn't a transient retry-able error), the function logs a
    warning and returns ``vix_daily=None``. Callers handle None
    explicitly (regime classifier + LLMContext both have None-paths).

    Args:
        start_date: inclusive, the replay window's first date
        end_date: inclusive, the replay window's last date

    Returns:
        MarketContextBundle as described above.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
        RuntimeError: SPY load failed. SPY is required; we cannot
            replay without market-context for the LLM's regime field.
            Per Rule 18, this fails loud.
    """
    raise NotImplementedError(
        "load_market_data is M2.2 work; M2.1 declares the contract. "
        "VIX half wired via _load_vix_daily; SPY half pending the "
        "polygon_feed.fetch_aggs helper. Both land in the same commit."
    )


async def _load_vix_daily(
    start_date: date, end_date: date
) -> pd.DataFrame | None:
    """Best-effort VIX daily fetch over ``[start_date, end_date]`` inclusive.

    Wraps ``data.fred_vix.get_vix_history``. Per the ``load_market_data``
    contract, VIX is best-effort: any upstream FRED failure (missing key,
    4xx, retries exhausted, empty observations, all-sentinels) collapses
    to ``None`` with a WARNING log. Both the regime classifier and the
    LLMContext builder already handle ``vix_daily=None`` paths (verified
    during the regime classifier rollout, 2026-05-13).

    A ``ValueError`` from the underlying call (programming error, e.g.
    ``end_date < start_date``) is re-raised unchanged — that's a caller
    bug, not a best-effort failure mode, and silently returning None
    would hide it (Rule 18).

    Unexpected exception types also propagate; the only swallowed class
    is ``RuntimeError`` from ``fred_vix``, whose contract guarantees
    that path covers every legitimate FRED-side failure.

    Caller responsibilities:
      - Pre-pad ``start_date`` for whatever warmup window downstream
        consumers require (the regime classifier wants ~60 trading days
        of VIX history to compute its 60-day median).
      - Caller is sync or async; this helper is async, so a sync caller
        wraps it in ``asyncio.run`` once at the top of the replay load.

    Rule 22 note: ``fred_vix``'s ``RuntimeError`` messages are already
    scrubbed of the ``api_key=`` URL value before they reach this
    handler, so logging ``str(e)`` directly is safe. We rely on that
    contract rather than re-scrubbing here.

    Args:
        start_date: inclusive
        end_date: inclusive

    Returns:
        ``DataFrame`` indexed by tz-aware UTC midnight date with a single
        column ``vix_close`` (float), sorted ascending — i.e. exactly
        the shape ``fred_vix.get_vix_history`` returns. ``None`` if the
        FRED fetch failed.
    """
    try:
        return await fred_vix.get_vix_history(start_date, end_date)
    except RuntimeError as e:
        logger.warning(
            "VIX load failed; market_context.vix_daily=None: %s", e
        )
        return None
