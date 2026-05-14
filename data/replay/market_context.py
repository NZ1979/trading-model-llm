"""SPY + VIX market context loader for the replay harness.

Populates the market-context fields of ``LLMContext``:
``spy_change_pct``, ``spy_rvol``, ``vix_level``. Also feeds the regime
classifier (``analysis/regime.py``) which produces
``market_regime_label``.

VIX caveat: Polygon Stocks Starter returns 403 on the ``I:VIX``
ticker (verified 2026-05-13). Until a working VIX source is wired up,
``vix_level`` is ``None`` and the regime classifier handles the gap
gracefully (already verified during regime classifier rollout).

Status: M2.1 scaffolding stub. Implementation lands in M2.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


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
        "load_market_data is M2.2 work; M2.1 declares the contract"
    )
