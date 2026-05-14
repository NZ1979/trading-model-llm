"""Per-ticker slowly-changing metadata for the replay harness.

Provides ``sector``, ``market_cap_bucket``, ``avg_daily_volume`` for
each replay ticker. These values feed ``LLMContext`` and the policy's
liquidity / bucket lookups. They change slowly (sector reassignments
are rare; market-cap-bucket shifts occur on quarterly fundamentals
updates), so we cache on disk with a configurable TTL and re-fetch
only when stale.

Status: M2.1 scaffolding stub. Implementation lands in M2.2.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TickerMetadata:
    """Slowly-changing per-ticker context fields.

    Field semantics pinned in ``docs/LLM_SIGNAL_INTERFACE.md`` § Input
    context structure. ``market_cap_bucket`` uses the same enum the
    live ``LLMContext`` carries.
    """

    ticker: str
    sector: str  # "Technology", "Healthcare", ... or "Unknown"
    market_cap_bucket: str  # "mega" | "large" | "mid" | "small" | "micro" | "unknown"
    avg_daily_volume: int  # 30-day average daily volume in shares


def get_ticker_metadata(
    ticker: str,
    cache_path: Path = Path("data/replay/fixtures/ticker_metadata.json"),
) -> TickerMetadata:
    """Return metadata for ``ticker``, fetching + caching as needed.

    Read path:
      1. If ``cache_path`` exists and has a fresh entry for ``ticker``,
         return it.
      2. Otherwise fetch from Polygon Reference (or Finnhub as
         fallback), write the row into the cache file, return it.

    "Fresh" means written within the last 7 days. Older entries are
    re-fetched. This is the same TTL policy ``data/watchlist_builder.py``
    already uses for its sector/cap lookups, so behavior is consistent
    across the codebase.

    Failures from the upstream API (404 unknown ticker, network error)
    return a ``TickerMetadata`` with ``sector="Unknown"`` and
    ``market_cap_bucket="unknown"`` but log a warning. This is a
    deliberate exception to strict fail-loud — slowly-changing
    metadata absence is a normal condition for newly-listed tickers,
    and downstream code (policy.py liquidity gate etc.) already
    handles "unknown" cleanly.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "get_ticker_metadata is M2.2 work; M2.1 declares the contract"
    )


def warm_metadata_cache(
    tickers: tuple[str, ...],
    cache_path: Path = Path("data/replay/fixtures/ticker_metadata.json"),
) -> None:
    """Pre-fetch metadata for ``tickers`` and write to cache.

    Called once at replay start to avoid first-tick latency from
    per-symbol cache misses. Idempotent: skips tickers already fresh.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "warm_metadata_cache is M2.2 work; M2.1 declares the contract"
    )
