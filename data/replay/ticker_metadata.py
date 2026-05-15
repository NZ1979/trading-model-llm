"""Per-ticker slowly-changing metadata for the replay harness.

Provides ``sector``, ``market_cap_bucket``, and ``avg_daily_volume`` for
each replay ticker. These values feed ``LLMContext`` and the policy's
liquidity / bucket lookups.

Data sources:

- ``sector`` + ``market_cap_bucket``: Polygon Reference Tickers v3
  (``/v3/reference/tickers/{ticker}``). The endpoint returns ``sic_code``
  (4-digit US Census SIC) and ``market_cap`` (USD). We map SIC ranges to
  the 11 GICS-style sector labels and bucketize market cap to the 5
  thresholds policy.py expects.
- ``avg_daily_volume``: ``data.polygon_feed.fetch_aggs`` daily bars over
  the 45 calendar days ending the day before ``as_of``, then take the
  mean of the last 30 trading days' ``volume`` column.

Point-in-time correctness:

The replay harness's `M2_REPLAY_HARNESS_DESIGN.md` § Design principles #1
requires point-in-time correctness. The two slowly-changing fields
(sector, market_cap_bucket) are NOT strictly point-in-time — Polygon
Reference returns current snapshot only. For the M2.1-M2.4 30-day replay
window this is acceptable (sector reassignments are <1/yr; cap-bucket
transitions are quarterly cadence). Longer windows (M2.5+) need a
historical Reference source; tracked as a follow-up. The replay report
documents this approximation.

``avg_daily_volume`` IS computed point-in-time: the 30-trading-day window
ends the day before ``as_of``, so a 2026-03 replay sees the volume that
was actually available to traders on that date.

Failure semantics (Rule 18):

- Polygon Reference 404 (unknown / delisted ticker) and Polygon daily
  bars empty (no trading activity in the window): log WARNING, return
  ``"Unknown"`` / ``"unknown"`` / ``0``. Downstream consumers
  (``policy.py``, the LLM prompt) handle these sentinels cleanly. This
  is Rule 18 option (2) — visible degradation, not silent.
- Polygon 5xx after retries, 4xx other than 404, transient network
  failure after retries, missing API key, truncated results: raise
  ``RuntimeError`` (loud). These are operational bugs, not data gaps.

Cache format (``data/replay/fixtures/ticker_metadata.json``):

::

    {
      "AAPL": {
        "sector": "Information Technology",
        "market_cap_bucket": "mega",
        "avg_daily_volume": 52345678,
        "as_of": "2026-04-30",
        "fetched_at": "2026-05-15T12:57:43Z"
      }
    }

The two timestamps support independent invalidation:

- ``fetched_at`` ages the slow fields (sector, market_cap_bucket). After
  ``SLOW_FIELD_TTL_DAYS`` (7) we re-fetch from Polygon Reference.
- ``as_of`` keys the ADV. A replay window shift invalidates ADV without
  forcing a Reference re-fetch.

Status: M2.2 sub-task #5 — fully implemented.

Rule 22 note: errors raised from this module pass URL strings through
``_scrub_apikey`` before assembly; ``from None`` on re-raises suppresses
the httpx exception chain so its default leaky message can't propagate.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from data.polygon_feed import (
    POLYGON_REST_BASE,
    _require_polygon_key,
    _scrub_apikey,
    fetch_aggs,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_CACHE_PATH = Path("data/replay/fixtures/ticker_metadata.json")

# TTL on the slow-changing fields (sector, market_cap_bucket). Matches the
# 7-day TTL convention documented in the original M2.1 scaffolding stub.
SLOW_FIELD_TTL_DAYS = 7

# ADV is the mean of the last 30 trading days. We request 45 calendar days
# of daily bars to comfortably cover that even with extended-holiday
# weeks, then ``.tail(ADV_TRAILING_TRADING_DAYS)``.
ADV_TRAILING_TRADING_DAYS = 30
ADV_LOOKBACK_CALENDAR_DAYS = 45

# Default warm-up concurrency. 8 parallel Polygon calls is well below the
# Stocks Starter throughput cap and matches the convention in
# polygon_feed.backfill_daily_bars.
DEFAULT_WARMUP_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# Market-cap bucketization
# ---------------------------------------------------------------------------
# Boundaries match the 5-bucket enum used by strategy/llm/policy.py
# (the 5→2 collapse at line 714) and the LLMContext.market_cap_bucket
# field default in strategy/llm/types.py.

_MARKET_CAP_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (200_000_000_000.0, "mega"),    # ≥ $200B
    (10_000_000_000.0, "large"),    # $10B – $200B
    (2_000_000_000.0, "mid"),       # $2B – $10B
    (300_000_000.0, "small"),       # $300M – $2B
    (0.0, "micro"),                 # < $300M
)


def _market_cap_to_bucket(market_cap: float | int | None) -> str:
    """Map raw market cap (USD) to the 5-bucket label policy.py expects.

    None, missing, or non-positive market cap → ``"unknown"``. This is
    the sentinel that policy.py's bucket-collapse handles cleanly.
    """
    if market_cap is None:
        return "unknown"
    try:
        cap = float(market_cap)
    except (TypeError, ValueError):
        return "unknown"
    if cap <= 0:
        return "unknown"
    for threshold, bucket in _MARKET_CAP_THRESHOLDS:
        if cap >= threshold:
            return bucket
    return "unknown"  # unreachable given the 0.0 threshold above; defensive


# ---------------------------------------------------------------------------
# SIC code → GICS-style sector mapping
# ---------------------------------------------------------------------------
# Polygon Reference Tickers v3 returns ``sic_code`` (4-digit US Census
# SIC code). The LLMContext.sector field expects a GICS-style sector
# string. This table maps SIC ranges to the 11 GICS sectors. Coverage is
# ~80% of common stocks; unmapped ranges (rare in equities) fall through
# to "Unknown". A more rigorous GICS mapping requires a paid data source;
# the SIC→GICS approximation is documented as a known limitation in the
# replay report.

_SECTOR_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "Consumer Staples"),              # Agriculture
    (1000, 1499, "Materials"),                   # Mining
    (1500, 1799, "Industrials"),                 # Construction
    (2000, 2099, "Consumer Staples"),            # Food
    (2100, 2199, "Consumer Staples"),            # Tobacco
    (2200, 2399, "Consumer Discretionary"),      # Textiles, apparel
    (2400, 2499, "Materials"),                   # Lumber
    (2500, 2599, "Consumer Discretionary"),      # Furniture
    (2600, 2699, "Materials"),                   # Paper
    (2700, 2799, "Communication Services"),      # Publishing
    (2800, 2829, "Materials"),                   # Industrial chemicals
    (2830, 2839, "Health Care"),                 # Pharma
    (2840, 2899, "Materials"),                   # Other chemicals
    (2900, 2999, "Energy"),                      # Petroleum
    (3000, 3099, "Materials"),                   # Rubber, plastics
    (3100, 3199, "Consumer Discretionary"),      # Leather
    (3200, 3299, "Materials"),                   # Stone, glass
    (3300, 3399, "Materials"),                   # Primary metal
    (3400, 3499, "Industrials"),                 # Fabricated metal
    (3500, 3569, "Industrials"),                 # Industrial machinery
    (3570, 3579, "Information Technology"),      # Computer equipment
    (3580, 3599, "Industrials"),                 # Other machinery
    (3600, 3699, "Information Technology"),      # Electronic equipment
    (3700, 3799, "Consumer Discretionary"),      # Transportation eq (incl autos)
    (3800, 3829, "Industrials"),                 # Instruments
    (3830, 3849, "Health Care"),                 # Medical instruments
    (3850, 3899, "Industrials"),                 # Other instruments
    (3900, 3999, "Consumer Discretionary"),      # Misc manufacturing
    (4000, 4799, "Industrials"),                 # Transportation
    (4800, 4829, "Communication Services"),      # Communications
    (4830, 4899, "Communication Services"),      # Broadcasting
    (4900, 4999, "Utilities"),                   # Utilities
    (5000, 5199, "Consumer Discretionary"),      # Wholesale
    (5200, 5399, "Consumer Discretionary"),      # General retail
    (5400, 5499, "Consumer Staples"),            # Food retail
    (5500, 5799, "Consumer Discretionary"),      # Specialty retail
    (5800, 5899, "Consumer Discretionary"),      # Restaurants
    (5900, 5999, "Consumer Discretionary"),      # Misc retail
    (6000, 6199, "Financials"),                  # Depository institutions
    (6200, 6299, "Financials"),                  # Securities
    (6300, 6399, "Financials"),                  # Insurance carriers
    (6400, 6499, "Financials"),                  # Insurance agents
    (6500, 6599, "Real Estate"),                 # Real Estate
    (6700, 6799, "Financials"),                  # Holding & investment offices
    (7000, 7299, "Consumer Discretionary"),      # Hotels, personal services
    (7300, 7369, "Industrials"),                 # Business services
    (7370, 7379, "Information Technology"),      # Computer services
    (7380, 7399, "Industrials"),                 # Other business services
    (7800, 7899, "Communication Services"),      # Motion pictures
    (7900, 7999, "Consumer Discretionary"),      # Amusement
    (8000, 8099, "Health Care"),                 # Health services
    (8200, 8299, "Consumer Discretionary"),      # Education
)


def _sic_to_sector(sic_code: str | int | None) -> str:
    """Map a SIC code to a GICS-style sector label.

    Accepts string or int (Polygon returns string). Empty / None /
    unparseable → ``"Unknown"``. Unmapped numeric ranges (uncommon for
    listed equities) → ``"Unknown"``.
    """
    if sic_code is None:
        return "Unknown"
    try:
        n = int(sic_code)
    except (TypeError, ValueError):
        return "Unknown"
    for lo, hi, sector in _SECTOR_RANGES:
        if lo <= n <= hi:
            return sector
    return "Unknown"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TickerMetadata:
    """Slowly-changing per-ticker context fields.

    Field semantics pinned in ``docs/LLM_SIGNAL_INTERFACE.md`` § Input
    context structure. ``market_cap_bucket`` uses the same enum
    ``LLMContext.market_cap_bucket`` carries (matched at
    ``strategy/llm/types.py:65``).
    """

    ticker: str
    sector: str  # one of the 11 GICS labels, or "Unknown"
    market_cap_bucket: str  # "mega" | "large" | "mid" | "small" | "micro" | "unknown"
    avg_daily_volume: int  # 30-trading-day average daily volume in shares


# ---------------------------------------------------------------------------
# Cache file I/O
# ---------------------------------------------------------------------------


def _read_cache_file(cache_path: Path) -> dict[str, dict]:
    """Read the on-disk JSON cache. Returns ``{}`` if file missing / malformed.

    Malformed-cache handling logs WARNING and returns empty; we'd rather
    re-fetch a few rows than abort the replay over a corrupted cache.
    """
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "ticker_metadata cache %s read failed (%s); treating as empty",
            cache_path, e,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "ticker_metadata cache %s root is not a dict; ignoring",
            cache_path,
        )
        return {}
    return data


def _write_cache_file(cache_path: Path, rows: dict[str, dict]) -> None:
    """Atomically write the cache.

    Writes to ``<path>.tmp`` then ``.replace()`` so a SIGKILL mid-write
    doesn't leave a half-truncated JSON file the next run can't parse.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, sort_keys=True)
    tmp.replace(cache_path)


def _utc_now_iso() -> str:
    """UTC now in the cache's fetched_at format (``YYYY-MM-DDTHH:MM:SSZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_slow_fields_fresh(row: dict, *, now: datetime | None = None) -> bool:
    """True iff ``row["fetched_at"]`` is within ``SLOW_FIELD_TTL_DAYS``."""
    fetched = row.get("fetched_at")
    if not fetched:
        return False
    try:
        ts = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return False
    reference = now if now is not None else datetime.now(timezone.utc)
    return reference - ts <= timedelta(days=SLOW_FIELD_TTL_DAYS)


# ---------------------------------------------------------------------------
# Polygon Reference Tickers v3 fetch
# ---------------------------------------------------------------------------


async def _fetch_polygon_reference(
    ticker: str,
    api_key: str,
    *,
    timeout_s: float = 30.0,
    max_retries: int = 3,
) -> dict | None:
    """Fetch ``/v3/reference/tickers/{ticker}`` and return the ``results`` dict.

    Returns ``None`` on:
      - HTTP 404 (Polygon does not know this ticker; delisted, brand new,
        or a typo). This is a normal data-gap condition.
      - 200 with no ``results`` key or a non-dict ``results``. Same
        rationale.

    Raises ``RuntimeError`` on:
      - 4xx other than 404 (bad key, malformed request, plan tier
        mismatch).
      - 5xx persisting after ``max_retries`` retries.
      - 429 persisting after retries.
      - Transient network error (ConnectError, ReadTimeout, WriteTimeout)
        persisting after retries.

    Rule 22: URL strings in raised messages pass through
    ``_scrub_apikey``. ``from None`` on re-raises suppresses the original
    httpx exception chain so its default URL-containing message can't
    propagate via ``__cause__``.
    """
    url = f"{POLYGON_REST_BASE}/v3/reference/tickers/{ticker}"
    params = {"apiKey": api_key}
    backoff = 1.0
    last_err: Exception | None = None

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429:
                    last_err = RuntimeError(
                        f"Polygon HTTP 429 (rate limited) for "
                        f"{_scrub_apikey(str(resp.url))}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    break
                resp.raise_for_status()
                payload = resp.json()
                results = payload.get("results")
                if not isinstance(results, dict):
                    return None
                return results
            except httpx.HTTPStatusError as e:
                if 500 <= e.response.status_code < 600:
                    last_err = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    break
                # 4xx other than 429/404: don't retry; raise with scrubbed URL.
                safe_url = _scrub_apikey(str(e.response.url))
                body = _scrub_apikey(e.response.text[:200])
                raise RuntimeError(
                    f"Polygon Reference HTTP {e.response.status_code} for "
                    f"{safe_url}: {body}"
                ) from None
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
            ) as e:
                last_err = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                break

    raise RuntimeError(
        f"Polygon Reference fetch failed after {max_retries} retries for "
        f"{ticker}: {_scrub_apikey(str(last_err))}"
    ) from None


# ---------------------------------------------------------------------------
# Average daily volume
# ---------------------------------------------------------------------------


# fetch_aggs raises ``RuntimeError`` with this exact message prefix on
# the empty-results path. We string-match here to distinguish "ticker
# exists but had no trades in the window" (soft-fail to ADV=0) from
# other RuntimeError causes (bad key, 5xx, retries exhausted,
# truncation) which propagate loud per Rule 18. If fetch_aggs's empty
# message changes, this match becomes a false negative and ADV=0 paths
# start raising loud — that's a strictly safer failure direction (Rule
# 18 option 3 > option 4) and the test suite catches it.
_EMPTY_BARS_MSG_PREFIX = "Polygon returned 0 bars"


async def _fetch_avg_daily_volume(ticker: str, as_of: date) -> int:
    """Trailing 30-trading-day mean daily volume ending the day before ``as_of``.

    Returns ``0`` with a WARNING log if Polygon returned 0 daily bars in
    the window (delisted, halted, or unknown ticker — symmetric to the
    Polygon Reference 404 path). Any other ``RuntimeError`` from
    ``fetch_aggs`` propagates loud (Rule 18).
    """
    end = as_of - timedelta(days=1)
    start = as_of - timedelta(days=ADV_LOOKBACK_CALENDAR_DAYS)
    try:
        df = await fetch_aggs(ticker, 1, "day", start, end)
    except RuntimeError as e:
        if str(e).startswith(_EMPTY_BARS_MSG_PREFIX):
            logger.warning(
                "ticker_metadata: %s has 0 daily bars %s..%s; ADV=0",
                ticker, start, end,
            )
            return 0
        raise
    if df.empty:
        # Defensive: fetch_aggs's empty-results path raises (caught
        # above), so this branch only fires if fetch_aggs is mocked /
        # contract changes. Same soft-fail semantics.
        return 0
    tail = df.tail(ADV_TRAILING_TRADING_DAYS)
    return int(round(float(tail["volume"].mean())))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_ticker_metadata(
    ticker: str,
    as_of: date,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> TickerMetadata:
    """Return metadata for ``ticker`` as of ``as_of``, fetching and caching as needed.

    Decision tree:

    1. Read the cache file. If ``ticker`` has a row with ``fetched_at``
       within ``SLOW_FIELD_TTL_DAYS`` (7), reuse its ``sector`` and
       ``market_cap_bucket``. Otherwise fetch ``/v3/reference/tickers/{ticker}``
       from Polygon and bucketize.
    2. If the same row's ``as_of`` matches the requested ``as_of``, reuse
       its ``avg_daily_volume``. Otherwise fetch daily bars over the
       last 45 calendar days ending the day before ``as_of`` and compute
       the mean of the last 30 trading days' volume.
    3. Write the (possibly updated) row back to the cache, atomically.

    Args:
        ticker: equity symbol; case-sensitive on Polygon's side.
        as_of: the replay window's start date. Determines the ADV
            lookback window (last 30 trading days ending ``as_of - 1d``)
            and the cache row's ``as_of`` invalidation key.
        cache_path: cache file location. Defaults to
            ``data/replay/fixtures/ticker_metadata.json``; tests pass a
            tmp_path.

    Returns:
        ``TickerMetadata``. Always returns a value: Polygon Reference
        404 → ``sector="Unknown"``, ``market_cap_bucket="unknown"``;
        empty Polygon daily bars → ``avg_daily_volume=0``. Both log
        WARNING per Rule 18.

    Raises:
        RuntimeError: ``POLYGON_API_KEY`` missing; Polygon 4xx other
            than 404; 5xx after retries; transient network failure
            after retries; fetch_aggs truncation. Operational bugs,
            not data gaps.
    """
    cache = _read_cache_file(cache_path)
    row = cache.get(ticker)

    # ---- Slow fields (sector, market_cap_bucket) ----
    if row is not None and _is_slow_fields_fresh(row):
        sector = str(row.get("sector", "Unknown"))
        bucket = str(row.get("market_cap_bucket", "unknown"))
        fetched_at = str(row["fetched_at"])
    else:
        api_key = _require_polygon_key()
        ref = await _fetch_polygon_reference(ticker, api_key)
        if ref is None:
            logger.warning(
                "ticker_metadata: Polygon Reference returned no row for %s; "
                "sector=Unknown, market_cap_bucket=unknown",
                ticker,
            )
            sector = "Unknown"
            bucket = "unknown"
        else:
            sector = _sic_to_sector(ref.get("sic_code"))
            bucket = _market_cap_to_bucket(ref.get("market_cap"))
        fetched_at = _utc_now_iso()

    # ---- ADV (as_of-keyed) ----
    cached_as_of = (row or {}).get("as_of")
    if row is not None and cached_as_of == as_of.isoformat():
        adv = int(row.get("avg_daily_volume", 0))
    else:
        adv = await _fetch_avg_daily_volume(ticker, as_of)

    cache[ticker] = {
        "sector": sector,
        "market_cap_bucket": bucket,
        "avg_daily_volume": adv,
        "as_of": as_of.isoformat(),
        "fetched_at": fetched_at,
    }
    _write_cache_file(cache_path, cache)

    return TickerMetadata(
        ticker=ticker,
        sector=sector,
        market_cap_bucket=bucket,
        avg_daily_volume=adv,
    )


async def warm_metadata_cache(
    tickers: tuple[str, ...],
    as_of: date,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    concurrency: int = DEFAULT_WARMUP_CONCURRENCY,
) -> None:
    """Pre-fetch metadata for ``tickers`` and write to cache.

    Called once at replay start to avoid first-tick latency from
    per-symbol cache misses. Idempotent — tickers whose slow fields AND
    ADV are both fresh skip both fetches.

    Per-ticker errors do NOT abort the whole warmup. Each ticker's
    failure logs a WARNING and the other tickers proceed; the use-site
    ``get_ticker_metadata`` call later will retry that ticker and raise
    if Polygon is still down. This matches "warm best-effort, use-site
    strict" — Rule 18 option (2) for warmup, option (3) for the actual
    replay path.

    Args:
        tickers: watchlist for the replay run.
        as_of: replay window start date (ADV invalidation key).
        cache_path: same as ``get_ticker_metadata``.
        concurrency: max in-flight Polygon requests. 8 is conservative
            relative to Stocks Starter's throughput.

    Raises:
        RuntimeError: ``POLYGON_API_KEY`` missing (raised by the first
            fetch attempt). All other per-ticker failures are logged
            and swallowed at this layer.
    """
    if not tickers:
        return

    cache = _read_cache_file(cache_path)
    sem = asyncio.Semaphore(concurrency)
    # Read the key once so we don't repeat the env lookup for every
    # ticker. _require_polygon_key raises loud on the first call,
    # which is the correct behavior — no point fetching 100 tickers
    # if the key is missing.
    api_key = _require_polygon_key()

    async def fetch_one(ticker: str) -> tuple[str, dict] | Exception | None:
        async with sem:
            existing = cache.get(ticker)
            fresh_slow = existing is not None and _is_slow_fields_fresh(existing)
            fresh_adv = (
                existing is not None
                and existing.get("as_of") == as_of.isoformat()
            )
            if fresh_slow and fresh_adv:
                return None  # idempotent skip

            try:
                if fresh_slow:
                    assert existing is not None  # for type-checkers
                    sector = str(existing["sector"])
                    bucket = str(existing["market_cap_bucket"])
                    fetched_at = str(existing["fetched_at"])
                else:
                    ref = await _fetch_polygon_reference(ticker, api_key)
                    if ref is None:
                        logger.warning(
                            "warm_metadata_cache: Polygon Reference returned "
                            "no row for %s; sector=Unknown, "
                            "market_cap_bucket=unknown",
                            ticker,
                        )
                        sector = "Unknown"
                        bucket = "unknown"
                    else:
                        sector = _sic_to_sector(ref.get("sic_code"))
                        bucket = _market_cap_to_bucket(ref.get("market_cap"))
                    fetched_at = _utc_now_iso()

                if fresh_adv:
                    assert existing is not None
                    adv = int(existing["avg_daily_volume"])
                else:
                    adv = await _fetch_avg_daily_volume(ticker, as_of)

                return ticker, {
                    "sector": sector,
                    "market_cap_bucket": bucket,
                    "avg_daily_volume": adv,
                    "as_of": as_of.isoformat(),
                    "fetched_at": fetched_at,
                }
            except Exception as exc:  # pragma: no cover - logged below
                logger.warning(
                    "warm_metadata_cache: %s failed (%s); leaving cache row "
                    "unchanged",
                    ticker, exc,
                )
                return exc

    results = await asyncio.gather(*(fetch_one(t) for t in tickers))

    written = 0
    for r in results:
        if r is None or isinstance(r, BaseException):
            continue
        ticker, new_row = r
        cache[ticker] = new_row
        written += 1

    if written:
        _write_cache_file(cache_path, cache)
        logger.info(
            "warm_metadata_cache: wrote %d new/updated row(s) to %s",
            written, cache_path,
        )
    else:
        logger.info(
            "warm_metadata_cache: no rows needed updating (all fresh); %s "
            "untouched",
            cache_path,
        )
