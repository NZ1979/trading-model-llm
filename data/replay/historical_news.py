"""Point-in-time historical news loader for the replay harness.

Wraps ``data.polygon_news.fetch_news_range`` (the shared httpx-based
Polygon News range helper) to return per-ticker news items over an
ET-aligned date range. The replay loop applies the
``news_lag_seconds`` buffer at slice time so publication-timestamp
items are NOT considered "available to the LLM" until
``published_ts + news_lag_seconds`` per design doc § News-data caveats.

Status: M2.2 sub-task #6 -- fully implemented.

Three public functions:

- ``load_historical_news(tickers, start_date, end_date)``: async I/O.
  Per-ticker JSON cache at ``data/replay/fixtures/news/<TICKER>.json``;
  cache hits when the requested window is a subset of the cached
  coverage. Bounded concurrency across tickers (default 8). Per-ticker
  failures propagate loud (Rule 18) -- silently dropping a ticker's
  news would corrupt the LLMContext.
- ``filter_visible_at(items, *, as_of_et, lookback_hours, lag_seconds)``:
  pure point-in-time gate. Applies the 30s ingestion-lag buffer and the
  recency lookback.
- ``items_to_context_dicts(items, sentiment_lookup)``: pure adapter to
  the ``LLMContext.news_items`` tuple-of-dicts shape. Sentiment lookup
  keyed by ``(polygon_article_id, ticker) -> float``; missing matches
  log WARNING and default to 0.0 (Rule 18 option 2 -- visible degradation).

Date semantics:

The public ``start_date`` / ``end_date`` are interpreted as **America/
New_York calendar dates** so news from the same trading session lands
in the same requested range regardless of DST. Internal cache files
record the resolved UTC bounds so cache-hit checks don't repeat the
timezone math.

Rule 22 note: errors raised through this module pass through
``fetch_news_range`` which scrubs apiKey query params from URL strings
and re-raises with ``from None`` to suppress httpx's leaky exception
chain.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Inputs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from data.polygon_news import fetch_news_range

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_CACHE_DIR = Path("data/replay/fixtures/news")

# Default fan-out concurrency for load_historical_news. Mirrors
# ticker_metadata.DEFAULT_WARMUP_CONCURRENCY (8) -- well below Polygon
# Stocks Starter's throughput cap.
DEFAULT_LOAD_CONCURRENCY = 8

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Cache-file schema version. Bump on any structural change to the on-
# disk JSON so stale caches deserialize as miss instead of producing
# silently-wrong items.
CACHE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalNewsItem:
    """One news item as the replay harness ingests it.

    Mirrors the shape ``LLMContext.news_items`` expects: a tuple of
    dicts with ``ts``, ``headline``, ``sentiment_score``, ``source``.
    The dataclass version is what loaders return; conversion to the
    LLMContext dict shape happens via ``items_to_context_dicts``.

    ``article_url`` is carried for debugging / spot-checking point-in-
    time correctness (per backtest credibility checklist line "manually
    verify LLMContext doesn't include data published after the
    timestamp"). It does not flow into the LLMContext dict shape.
    """

    ts_et: datetime  # publication time, tz-aware America/New_York
    ticker: str
    headline: str
    source: str  # "polygon" (this loader); other sources reserved for future feeds
    polygon_article_id: str | None = None
    article_url: str | None = None


# ---------------------------------------------------------------------------
# Date / timezone helpers
# ---------------------------------------------------------------------------


def _et_date_to_utc_bounds(
    start_date: date, end_date: date
) -> tuple[datetime, datetime]:
    """Convert ET calendar dates to inclusive UTC datetime bounds.

    ``start_date 00:00 America/New_York`` -> UTC for ``.gte``.
    ``end_date 23:59:59 America/New_York`` -> UTC for ``.lte``.

    DST-aware via zoneinfo so EDT (-04:00) and EST (-05:00) days both
    land on the right UTC instant without manual offset math.

    Raises:
        ValueError: ``end_date < start_date``.
    """
    if end_date < start_date:
        raise ValueError(
            f"end_date {end_date} is before start_date {start_date}"
        )
    start_et = datetime.combine(start_date, datetime.min.time(), tzinfo=ET)
    # 23:59:59 inclusive upper bound. Polygon's published_utc.lte is
    # inclusive at second granularity, which is the resolution Polygon
    # records publication timestamps at.
    end_et = datetime.combine(
        end_date,
        datetime.max.time().replace(microsecond=0),
        tzinfo=ET,
    )
    return start_et.astimezone(UTC), end_et.astimezone(UTC)


def _utc_now_iso() -> str:
    """UTC now formatted as the cache's ``fetched_at`` field."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc_iso(s: str) -> datetime:
    """Parse a Polygon ``published_utc`` (or our cached ``ts_utc``) string.

    Polygon publishes ``YYYY-MM-DDTHH:MM:SSZ`` or with a numeric offset
    (rare; mostly historical). ``fromisoformat`` since Python 3.11
    accepts the trailing ``Z`` directly; on 3.10 we substitute manually.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Defensive: an unannotated Polygon timestamp is treated as UTC,
        # not local. Polygon documents these as UTC.
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Cache file I/O (per-ticker JSON)
# ---------------------------------------------------------------------------


def _cache_path_for(cache_dir: Path, ticker: str) -> Path:
    """Cache file path for one ticker.

    Polygon tickers are alphanumeric + ``.`` for class shares (BRK.A);
    sanitize ``.`` to ``_`` so the filesystem path is portable across
    Windows / Linux without escaping rules biting us.
    """
    safe = ticker.replace(".", "_").replace("/", "_").replace("\\", "_")
    return cache_dir / f"{safe}.json"


def _read_cache_file(path: Path) -> dict[str, Any] | None:
    """Read a per-ticker cache file. Returns None on missing / malformed.

    Malformed cache files log WARNING and return None -- we'd rather
    re-fetch than abort the replay over a corrupted cache row.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "historical_news cache %s read failed (%s); treating as miss",
            path, e,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "historical_news cache %s root is not a dict; ignoring",
            path,
        )
        return None
    if data.get("schema_version") != CACHE_SCHEMA_VERSION:
        logger.warning(
            "historical_news cache %s schema_version mismatch "
            "(got %r, want %r); treating as miss",
            path, data.get("schema_version"), CACHE_SCHEMA_VERSION,
        )
        return None
    return data


def _write_cache_file(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write a per-ticker cache file.

    Writes to ``<path>.tmp`` then ``.replace()`` so a SIGKILL mid-write
    doesn't leave a half-truncated JSON file the next run can't parse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _coverage_covers(
    cached: dict[str, Any], start_utc: datetime, end_utc: datetime
) -> bool:
    """True iff the cached coverage window is a superset of [start_utc, end_utc].

    Subset semantics intentionally: a window outside the cached
    coverage triggers a full re-fetch of the requested window. We do
    not splice partial overlaps -- replay windows are stable and the
    extra simplicity wins.
    """
    cov = cached.get("coverage") or {}
    cov_start_raw = cov.get("start_utc")
    cov_end_raw = cov.get("end_utc")
    if not cov_start_raw or not cov_end_raw:
        return False
    try:
        cov_start = _parse_utc_iso(cov_start_raw)
        cov_end = _parse_utc_iso(cov_end_raw)
    except ValueError:
        return False
    return cov_start <= start_utc and cov_end >= end_utc


# ---------------------------------------------------------------------------
# Polygon article -> HistoricalNewsItem
# ---------------------------------------------------------------------------


def _article_to_item(
    article: dict[str, Any], ticker: str
) -> HistoricalNewsItem | None:
    """Convert one Polygon News article dict to a HistoricalNewsItem.

    Returns ``None`` (with a WARNING) if the article is missing the
    publication timestamp or headline -- there's nothing usable for the
    LLM in such a row. Other malformed fields default to empty / None
    rather than dropping the row.
    """
    published_raw = article.get("published_utc")
    title = article.get("title")
    if not published_raw or not title:
        logger.warning(
            "historical_news: dropping article id=%r ticker=%s; "
            "missing published_utc or title",
            article.get("id"), ticker,
        )
        return None
    try:
        ts_utc = _parse_utc_iso(published_raw)
    except ValueError:
        logger.warning(
            "historical_news: dropping article id=%r ticker=%s; "
            "unparseable published_utc=%r",
            article.get("id"), ticker, published_raw,
        )
        return None
    return HistoricalNewsItem(
        ts_et=ts_utc.astimezone(ET),
        ticker=ticker,
        headline=str(title)[:500],  # cap defensively; matches polygon_news.py
        source="polygon",
        polygon_article_id=str(article.get("id")) if article.get("id") else None,
        article_url=(
            str(article["article_url"]) if article.get("article_url") else None
        ),
    )


def _item_to_cache_row(item: HistoricalNewsItem) -> dict[str, Any]:
    """Serialize a HistoricalNewsItem to the on-disk cache shape."""
    return {
        "ts_utc": item.ts_et.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticker": item.ticker,
        "headline": item.headline,
        "source": item.source,
        "polygon_article_id": item.polygon_article_id,
        "article_url": item.article_url,
    }


def _cache_row_to_item(row: dict[str, Any]) -> HistoricalNewsItem | None:
    """Deserialize one cache row. Returns None on malformed."""
    ts_raw = row.get("ts_utc")
    ticker = row.get("ticker")
    headline = row.get("headline")
    if not ts_raw or not ticker or headline is None:
        return None
    try:
        ts_utc = _parse_utc_iso(ts_raw)
    except ValueError:
        return None
    return HistoricalNewsItem(
        ts_et=ts_utc.astimezone(ET),
        ticker=str(ticker),
        headline=str(headline),
        source=str(row.get("source") or "polygon"),
        polygon_article_id=(
            str(row["polygon_article_id"])
            if row.get("polygon_article_id")
            else None
        ),
        article_url=(
            str(row["article_url"]) if row.get("article_url") else None
        ),
    )


# ---------------------------------------------------------------------------
# Polygon credential resolution
# ---------------------------------------------------------------------------


def _require_polygon_key() -> str:
    """Read the Polygon API key from POLYGON_API_KEY env var or raise.

    Matches data.polygon_feed._require_polygon_key semantics but is
    locally re-implemented to avoid a leaky abstraction dependency
    (polygon_feed's helper is private). The error message intentionally
    does NOT include any partial-key fingerprint -- Rule 21.
    """
    key = os.environ.get("POLYGON_API_KEY", "")
    if not key:
        raise RuntimeError(
            "POLYGON_API_KEY is not set; historical_news cannot fetch "
            "from Polygon News REST"
        )
    return key


# ---------------------------------------------------------------------------
# Per-ticker fetch + cache
# ---------------------------------------------------------------------------


async def _load_one_ticker(
    ticker: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    api_key: str,
    cache_dir: Path,
) -> list[HistoricalNewsItem]:
    """Load one ticker's news, using cache if covered, else fetching and caching.

    Raises loud on Polygon-side failures (per Rule 18, news is a
    direct LLMContext input -- silent zero-substitution would lie to
    the model).
    """
    path = _cache_path_for(cache_dir, ticker)
    cached = _read_cache_file(path)
    if cached is not None and _coverage_covers(cached, start_utc, end_utc):
        items: list[HistoricalNewsItem] = []
        for row in cached.get("items", []):
            it = _cache_row_to_item(row)
            if it is None:
                continue
            ts_utc = it.ts_et.astimezone(UTC)
            if start_utc <= ts_utc <= end_utc:
                items.append(it)
        items.sort(key=lambda x: x.ts_et)
        return items

    # Cache miss (or non-superset coverage). Fetch the full requested
    # window and overwrite the file. Coverage on disk is set to the
    # requested window so subsequent calls in the same range serve from
    # cache. If a future call asks for a wider window we re-fetch then,
    # not now.
    articles = await fetch_news_range(
        ticker=ticker,
        start_utc=start_utc,
        end_utc=end_utc,
        api_key=api_key,
    )

    items = []
    for art in articles:
        it = _article_to_item(art, ticker)
        if it is not None:
            items.append(it)
    items.sort(key=lambda x: x.ts_et)

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "ticker": ticker,
        "coverage": {
            "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "fetched_at": _utc_now_iso(),
        "items": [_item_to_cache_row(it) for it in items],
    }
    _write_cache_file(path, payload)
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def load_historical_news(
    tickers: tuple[str, ...],
    start_date: date,
    end_date: date,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    concurrency: int = DEFAULT_LOAD_CONCURRENCY,
) -> dict[str, list[HistoricalNewsItem]]:
    """Load all news items for ``tickers`` over ``[start_date, end_date]``.

    Args:
        tickers: tuple of equity symbols. Case-sensitive on Polygon's
            side; this function does not upper-case.
        start_date: inclusive, interpreted as an **ET calendar date**.
        end_date: inclusive, interpreted as an **ET calendar date**.
        cache_dir: per-ticker JSON cache directory. Defaults to
            ``data/replay/fixtures/news``; tests pass a tmp_path.
        concurrency: max in-flight Polygon News requests.

    Returns:
        ``dict[ticker, list[HistoricalNewsItem]]``. Every requested
        ticker appears as a key (even with empty news). Per-ticker
        lists are sorted ascending by ``ts_et``.

    Raises:
        ValueError: ``end_date < start_date``; empty ``tickers``;
            ``concurrency < 1``.
        RuntimeError: ``POLYGON_API_KEY`` missing; any per-ticker
            Polygon-side failure (4xx other than 404, 5xx after
            retries, transient network failure after retries,
            pagination cap exceeded). Per-ticker failures abort the
            whole batch -- silently dropping a ticker's news from
            LLMContext is a worse failure mode than aborting the
            replay (Rule 18 option 3 > option 2 for use-site loaders).
    """
    if not tickers:
        raise ValueError("load_historical_news requires at least one ticker")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    start_utc, end_utc = _et_date_to_utc_bounds(start_date, end_date)
    api_key = _require_polygon_key()

    sem = asyncio.Semaphore(concurrency)

    async def fetch_one(ticker: str) -> tuple[str, list[HistoricalNewsItem]]:
        async with sem:
            items = await _load_one_ticker(
                ticker,
                start_utc,
                end_utc,
                api_key=api_key,
                cache_dir=cache_dir,
            )
            return ticker, items

    # return_exceptions=False so any per-ticker RuntimeError aborts the
    # whole gather and surfaces to the caller (Rule 18 fail-loud).
    results = await asyncio.gather(*(fetch_one(t) for t in tickers))
    return {ticker: items for ticker, items in results}


def filter_visible_at(
    items: list[HistoricalNewsItem],
    *,
    as_of_et: datetime,
    lookback_hours: int,
    lag_seconds: int,
) -> list[HistoricalNewsItem]:
    """Return items visible to the LLM at ``as_of_et``.

    An item is visible iff::

        item.ts_et + timedelta(seconds=lag_seconds) <= as_of_et
        AND item.ts_et >= as_of_et - timedelta(hours=lookback_hours)

    Pure function, no I/O. Returns a new list (does not mutate input).
    The 30-second buffer is the design doc's ingestion-lag
    approximation; the caller passes
    ``ReplayConfig.news_lag_seconds``.

    Args:
        items: list of HistoricalNewsItem, in any order.
        as_of_et: replay tick timestamp, tz-aware America/New_York.
        lookback_hours: recency window in hours. Items older than this
            are not visible (matches the live news_lookback contract).
        lag_seconds: ingestion-lag buffer in seconds. Items whose
            publication time plus this many seconds is in the future
            relative to ``as_of_et`` are not yet visible.

    Raises:
        ValueError: ``as_of_et`` naive; ``lookback_hours < 0``;
            ``lag_seconds < 0``.
    """
    if as_of_et.tzinfo is None:
        raise ValueError(
            "filter_visible_at requires tz-aware as_of_et; got naive"
        )
    if lookback_hours < 0:
        raise ValueError(
            f"lookback_hours must be >= 0, got {lookback_hours}"
        )
    if lag_seconds < 0:
        raise ValueError(
            f"lag_seconds must be >= 0, got {lag_seconds}"
        )

    lag = timedelta(seconds=lag_seconds)
    earliest = as_of_et - timedelta(hours=lookback_hours)
    return [
        it for it in items
        if it.ts_et + lag <= as_of_et and it.ts_et >= earliest
    ]


def items_to_context_dicts(
    items: list[HistoricalNewsItem],
    sentiment_lookup: dict[tuple[str, str], float] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Convert HistoricalNewsItems to the LLMContext.news_items tuple shape.

    Output dict shape per ``docs/LLM_SIGNAL_INTERFACE.md`` § Input
    context structure::

        {"ts": <ISO8601 ET>, "headline": str,
         "sentiment_score": float, "source": str}

    Args:
        items: list of HistoricalNewsItem.
        sentiment_lookup: optional mapping
            ``(polygon_article_id, ticker) -> float`` resolving each
            item's sentiment score. If ``None``, every item gets
            ``sentiment_score=0.0`` (no warnings -- the caller
            explicitly opted out of sentiment).

    Returns:
        Tuple of dicts, in the same order as ``items``.

    Sentiment-miss semantics (Rule 18 option 2 -- visible degradation):
        If ``sentiment_lookup`` is provided but an item has no
        ``polygon_article_id`` or the (id, ticker) key isn't present,
        ``sentiment_score`` defaults to 0.0 AND a WARNING is logged
        from this function. The substitution is never silent.
    """
    out: list[dict[str, Any]] = []
    for it in items:
        if sentiment_lookup is None:
            score = 0.0
        elif it.polygon_article_id is None:
            logger.warning(
                "items_to_context_dicts: item ticker=%s ts=%s has no "
                "polygon_article_id; sentiment_score defaulting to 0.0",
                it.ticker, it.ts_et.isoformat(),
            )
            score = 0.0
        else:
            key = (it.polygon_article_id, it.ticker)
            if key in sentiment_lookup:
                score = float(sentiment_lookup[key])
            else:
                logger.warning(
                    "items_to_context_dicts: no sentiment row for "
                    "(article_id=%s, ticker=%s); sentiment_score defaulting "
                    "to 0.0",
                    it.polygon_article_id, it.ticker,
                )
                score = 0.0
        out.append({
            "ts": it.ts_et.isoformat(),
            "headline": it.headline,
            "sentiment_score": score,
            "source": it.source,
        })
    return tuple(out)
