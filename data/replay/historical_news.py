"""Point-in-time historical news loader for the replay harness.

Wraps Polygon News REST (and, where applicable, the locally-stored
news cache) to return per-ticker news items over a date range. The
replay loop applies the ``news_lag_seconds`` buffer at slice time so
publication-timestamp items are NOT considered "available to the LLM"
until ``published_ts + news_lag_seconds`` per design doc § News-data
caveats.

Status: M2.1 scaffolding stub. Implementation lands in M2.2.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class HistoricalNewsItem:
    """One news item as the replay harness ingests it.

    Mirrors the shape ``LLMContext.news_items`` expects: a tuple of
    dicts with ``ts``, ``headline``, ``sentiment_score``, ``source``.
    The dataclass version is what loaders return; conversion to the
    LLMContext dict shape happens in the context builder.
    """

    ts_et: datetime  # publication time, tz-aware America/New_York
    ticker: str
    headline: str
    source: str  # "polygon", "alpaca_news", etc.
    polygon_article_id: str | None = None


def load_historical_news(
    tickers: tuple[str, ...],
    start_date: date,
    end_date: date,
) -> dict[str, list[HistoricalNewsItem]]:
    """Load all news items for the given tickers + date range.

    Returns a dict mapping ticker -> list of items, sorted ascending by
    ``ts_et``. Tickers with no news in the window get an empty list (not
    omitted from the dict — callers iterate the requested ticker set and
    expect all keys present).

    Will use the existing ``data/polygon_news.py`` Polygon News client
    when implemented.

    News-availability gating (the 30s ingestion lag from design doc) is
    applied by the replay tick loop, not here. This loader returns
    publication timestamps as-is.

    Args:
        tickers: tuple of equity symbols
        start_date: inclusive
        end_date: inclusive

    Returns:
        dict[ticker, list of HistoricalNewsItem] sorted by ts_et.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "load_historical_news is M2.2 work; M2.1 scaffolding only "
        "declares the contract"
    )


def filter_visible_at(
    items: list[HistoricalNewsItem],
    *,
    as_of_et: datetime,
    lookback_hours: int,
    lag_seconds: int,
) -> list[HistoricalNewsItem]:
    """Return items visible to the LLM at ``as_of_et``.

    An item is visible iff:
      - ``item.ts_et + lag_seconds <= as_of_et`` (ingestion-lag buffer)
      - ``item.ts_et >= as_of_et - lookback_hours`` (recency window)

    Pure function, no I/O. Implemented in M2.1 (this is part of the
    point-in-time correctness contract and is small enough to land with
    the scaffolding so M2.2 can use it directly).

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.

    Note: Spec says this should be implemented in M2.1, but the
    implementation deliberately ships with the loaders in M2.2 so the
    filter and the loader can be tested together as one unit. The
    contract here is the binding statement.
    """
    raise NotImplementedError(
        "filter_visible_at lands in M2.2 alongside the loader; "
        "M2.1 declares the contract here"
    )


def items_to_context_dicts(
    items: list[HistoricalNewsItem],
    sentiment_lookup: dict[tuple[str, str], float] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Convert a list of HistoricalNewsItem to the LLMContext.news_items shape.

    The output dict shape is ``{ts, headline, sentiment_score, source}``
    per ``docs/LLM_SIGNAL_INTERFACE.md`` § Input context structure.

    If ``sentiment_lookup`` is provided, sentiment_score is filled from
    it keyed by (polygon_article_id, ticker). Items with no matching
    sentiment get ``sentiment_score=0.0`` (neutral) and a log warning
    one level up — never silently substituted (Rule 18).

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "items_to_context_dicts is M2.2 work; M2.1 declares the contract"
    )
