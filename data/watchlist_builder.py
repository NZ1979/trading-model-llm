"""Dynamic watchlist builder (Phase B).

Sources S&P 500 + NASDAQ-100 + DJIA constituents from Wikipedia, computes
30-day average dollar volume per symbol via Polygon's grouped daily
aggregates, and returns the top N (default 500) by ADV.

Wired into main.py as a daily 08:30 ET refresh that writes
`config/watchlist_dynamic.json`. At service boot, main.py loads
watchlist_dynamic.json if recent (<7 days), else falls back to
`settings.yaml.watchlist`.

Wikipedia table layouts can shift over time. Each constituent fetcher uses
defensive column-name detection plus row-count sanity bounds rather than
positional table indexing.

Symbol normalization: outputs use the dot form (e.g. "BRK.B"), which matches
both Polygon and Alpaca conventions.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

WIKI_HEADERS = {
    "User-Agent": "trader-platform/1.0 (https://github.com/Trading.Base.1)"
}

POLYGON_BASE = "https://api.polygon.io"
HTTP_TIMEOUT_SEC = 30


def _read_html(url: str) -> list[pd.DataFrame]:
    """Fetch a Wikipedia page and parse its tables.

    Wikipedia rate-limits unidentified User-Agents; we set a descriptive UA.

    Wraps the HTML body in `io.StringIO` for pd.read_html — pandas 2.2+
    removed support for passing literal HTML strings directly; the
    StringIO wrap is required.
    """
    req = Request(url, headers=WIKI_HEADERS)
    with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return pd.read_html(io.StringIO(html))


def _find_symbol_column(df: pd.DataFrame) -> str | None:
    """Find the column that holds tickers — defensively, by name."""
    for col in df.columns:
        col_str = str(col).strip().lower()
        if col_str in ("symbol", "ticker", "ticker symbol", "code"):
            return col
    return None


def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol to Polygon/Alpaca format.

    Wikipedia sometimes uses "BRK.B" and sometimes "BRK-B" depending on the
    page. Polygon and Alpaca both use the dot form. Convert hyphens to dots.
    """
    s = str(symbol).strip().upper()
    s = s.replace("-", ".")
    return s


def get_sp500_symbols() -> set[str]:
    """Fetch current S&P 500 constituents from Wikipedia.

    Returns a set of ~503 normalized symbols (some companies have multiple
    share classes, e.g., GOOG/GOOGL). Returns an empty set on parse failure.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        tables = _read_html(url)
    except Exception:
        logger.exception("Failed to fetch S&P 500 page from Wikipedia")
        return set()
    for df in tables:
        col = _find_symbol_column(df)
        if col is None:
            continue
        if not (400 <= len(df) <= 600):
            continue
        symbols = {
            _normalize_symbol(s) for s in df[col].tolist()
            if isinstance(s, str) and s.strip()
        }
        if 400 <= len(symbols) <= 600:
            logger.info("S&P 500: parsed %d symbols from Wikipedia", len(symbols))
            return symbols
    logger.error("Could not find S&P 500 constituent table in Wikipedia HTML")
    return set()


def get_nasdaq100_symbols() -> set[str]:
    """Fetch current NASDAQ-100 constituents from Wikipedia.

    Returns ~100 normalized symbols. Returns empty set on parse failure.
    """
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    try:
        tables = _read_html(url)
    except Exception:
        logger.exception("Failed to fetch NASDAQ-100 page from Wikipedia")
        return set()
    for df in tables:
        col = _find_symbol_column(df)
        if col is None:
            continue
        if not (90 <= len(df) <= 110):
            continue
        symbols = {
            _normalize_symbol(s) for s in df[col].tolist()
            if isinstance(s, str) and s.strip()
        }
        if 90 <= len(symbols) <= 110:
            logger.info("NASDAQ-100: parsed %d symbols from Wikipedia", len(symbols))
            return symbols
    logger.error("Could not find NASDAQ-100 constituent table in Wikipedia HTML")
    return set()


def get_djia_symbols() -> set[str]:
    """Fetch current DJIA constituents from Wikipedia.

    Returns exactly 30 normalized symbols. Returns empty set on parse
    failure.
    """
    url = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
    try:
        tables = _read_html(url)
    except Exception:
        logger.exception("Failed to fetch DJIA page from Wikipedia")
        return set()
    for df in tables:
        col = _find_symbol_column(df)
        if col is None:
            continue
        if len(df) != 30:
            continue
        symbols = {
            _normalize_symbol(s) for s in df[col].tolist()
            if isinstance(s, str) and s.strip()
        }
        if len(symbols) == 30:
            logger.info("DJIA: parsed %d symbols from Wikipedia", len(symbols))
            return symbols
    logger.error(
        "Could not find DJIA constituent table in Wikipedia HTML "
        "(expected exactly 30 rows)"
    )
    return set()


async def _fetch_grouped_daily(
    session: aiohttp.ClientSession,
    polygon_key: str,
    target_date: str,
) -> dict[str, tuple[float, int]]:
    """Fetch one day's grouped daily aggregates from Polygon.

    Returns {ticker: (close_price, volume)} for all tickers that traded.
    Returns {} on weekends/holidays/errors. The grouped endpoint is
    free-tier on Polygon Stocks Starter and returns ~10,000 tickers in a
    single response, which makes it far cheaper than per-ticker queries
    for our union universe.
    """
    url = (
        f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/"
        f"{target_date}?apiKey={polygon_key}"
    )
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(
                    "Polygon grouped daily failed for %s: HTTP %d body=%s",
                    target_date, resp.status, body[:300],
                )
                return {}
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error("Polygon grouped daily network error for %s: %s",
                     target_date, e)
        return {}

    results = data.get("results") or []
    out: dict[str, tuple[float, int]] = {}
    for row in results:
        ticker = row.get("T")
        close = row.get("c")
        volume = row.get("v")
        if not ticker or close is None or volume is None:
            continue
        out[ticker] = (float(close), int(volume))
    return out


async def fetch_30day_adv(
    symbols: set[str],
    polygon_key: str,
    *,
    days_lookback: int = 45,
    target_trading_days: int = 30,
) -> dict[str, float]:
    """Fetch 30-day average dollar volume for the given symbols.

    Strategy: query Polygon's grouped daily aggregates for the last
    `days_lookback` calendar days (45 is enough to cover 30 trading days).
    Skip empty days (weekends/holidays return empty). Take the most recent
    `target_trading_days` non-empty days. Sum dollar volume per symbol,
    divide by the actual trading-day count.

    Returns {symbol: avg_dollar_volume}. Symbols absent from Polygon's data
    (delisted, ADRs not in coverage, etc.) are absent from the result.
    """
    today = date.today()
    candidate_dates = [
        (today - timedelta(days=i)).isoformat()
        for i in range(1, days_lookback + 1)
    ]

    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_grouped_daily(session, polygon_key, d)
            for d in candidate_dates
        ]
        results = await asyncio.gather(*tasks)

    # Filter to non-empty (= actual trading days), take most recent N
    non_empty = [(d, r) for d, r in zip(candidate_dates, results) if r]
    if not non_empty:
        logger.error(
            "Polygon returned no trading-day data in last %d calendar days",
            days_lookback,
        )
        return {}
    trading_days = non_empty[:target_trading_days]
    actual_count = len(trading_days)
    if actual_count < target_trading_days:
        logger.warning(
            "Only %d trading days available (asked for %d); using what we have",
            actual_count, target_trading_days,
        )

    # Accumulate dollar volume per symbol, restricted to our universe
    totals: dict[str, float] = {}
    for _, day_data in trading_days:
        for ticker, (close, volume) in day_data.items():
            if ticker not in symbols:
                continue
            totals[ticker] = totals.get(ticker, 0.0) + close * volume

    return {sym: total / actual_count for sym, total in totals.items()}


async def build_dynamic_watchlist(
    polygon_key: str,
    *,
    top_n: int = 500,
) -> tuple[list[str], dict[str, Any]]:
    """Build the dynamic watchlist: union of S&P 500 + NASDAQ-100 + DJIA,
    top N by 30-day average dollar volume.

    Returns (watchlist_symbols, metadata) where metadata has source counts,
    union size, last-updated timestamp, and the count of symbols that had
    Polygon ADV data. Returns ([], {}) on any source-side failure that
    would produce an unsafe partial result.
    """
    sp500 = get_sp500_symbols()
    ndx = get_nasdaq100_symbols()
    djia = get_djia_symbols()

    if len(sp500) < 400:
        logger.error("S&P 500 source returned %d symbols; aborting refresh",
                     len(sp500))
        return [], {}
    if len(ndx) < 90:
        logger.error("NASDAQ-100 source returned %d symbols; aborting refresh",
                     len(ndx))
        return [], {}
    if len(djia) != 30:
        logger.error(
            "DJIA source returned %d symbols (expected 30); aborting refresh",
            len(djia),
        )
        return [], {}

    union = sp500 | ndx | djia
    logger.info(
        "Universe: SP500=%d, NDX=%d, DJIA=%d, union=%d",
        len(sp500), len(ndx), len(djia), len(union),
    )

    advs = await fetch_30day_adv(union, polygon_key)
    if not advs:
        logger.error("ADV computation returned empty; aborting refresh")
        return [], {}

    # Sort by ADV descending; symbols missing from advs go to the end
    sorted_symbols = sorted(union, key=lambda s: advs.get(s, 0.0), reverse=True)
    top = sorted_symbols[:top_n]
    advs_count = sum(1 for s in top if s in advs)

    metadata = {
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_counts": {
            "sp500": len(sp500),
            "nasdaq100": len(ndx),
            "djia": len(djia),
        },
        "union_size": len(union),
        "top_n": top_n,
        "with_adv_data": advs_count,
    }
    logger.info(
        "Dynamic watchlist built: top %d by 30D ADV (%d had ADV data)",
        len(top), advs_count,
    )
    return top, metadata


def write_watchlist_file(
    watchlist: list[str],
    metadata: dict[str, Any],
    output_path: Path,
) -> None:
    """Write watchlist + metadata atomically to a JSON file.

    Uses write-to-temp-then-rename to avoid leaving a half-written file
    if the process is killed mid-write.
    """
    payload = {**metadata, "watchlist": watchlist}
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    tmp_path.replace(output_path)
    logger.info(
        "Wrote dynamic watchlist to %s (%d symbols)",
        output_path, len(watchlist),
    )


def read_watchlist_file(
    path: Path,
    *,
    max_age_days: int = 7,
) -> list[str] | None:
    """Read dynamic watchlist if present and recent.

    Returns the list of symbols if valid; None if the file doesn't exist,
    is malformed, or is older than `max_age_days`.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
    except Exception:
        logger.exception("Failed to read dynamic watchlist from %s", path)
        return None

    last_updated_str = payload.get("last_updated")
    if not last_updated_str:
        logger.warning("Dynamic watchlist at %s missing last_updated field", path)
        return None

    try:
        last_updated = datetime.fromisoformat(
            last_updated_str.replace("Z", "+00:00")
        )
        age_seconds = (datetime.now(timezone.utc) - last_updated).total_seconds()
    except Exception:
        logger.exception("Could not parse last_updated %r", last_updated_str)
        return None

    if age_seconds > max_age_days * 86400:
        logger.warning(
            "Dynamic watchlist at %s is %.1f days old (max %d); ignoring",
            path, age_seconds / 86400, max_age_days,
        )
        return None

    watchlist = payload.get("watchlist") or []
    if not isinstance(watchlist, list) or not watchlist:
        logger.warning("Dynamic watchlist at %s has empty watchlist field", path)
        return None

    return watchlist


async def refresh_dynamic_watchlist(
    polygon_key: str,
    output_path: Path,
    *,
    top_n: int = 500,
) -> bool:
    """Build and persist the dynamic watchlist. Returns True on success.

    On failure, the existing file (if any) is left untouched so the next
    service boot still has a usable watchlist.
    """
    watchlist, metadata = await build_dynamic_watchlist(
        polygon_key, top_n=top_n
    )
    if not watchlist:
        logger.error(
            "Dynamic watchlist refresh failed; leaving previous file untouched"
        )
        return False
    write_watchlist_file(watchlist, metadata, output_path)
    return True
