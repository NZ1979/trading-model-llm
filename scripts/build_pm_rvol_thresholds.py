"""Phase C sandbox sweep: build per-ticker PM RVOL thresholds.

For each ticker in the watchlist:
  1. Fetch the last `lookback_days` of 1-minute bars (pre-market + RTH) from Polygon.
  2. Filter to pre-market hours (4:00-9:30 ET).
  3. Sum PM volume per trading day.
  4. Starting from day 21, compute a rolling 20-day PM volume baseline.
  5. PM RVOL per day = today's PM volume / baseline.
  6. Take the `percentile` of the resulting distribution per ticker as that
     ticker's PM RVOL threshold.
  7. Clip to [floor, cap] to avoid degenerate extremes.

Output: `config/pm_rvol_thresholds.json` (the file consumed by
`data/pm_rvol_thresholds.py` at runtime).

Usage (manual):
    python scripts/build_pm_rvol_thresholds.py

Or scheduled (called from main.py daily routine; see _run_pm_rvol_threshold_refresh).

Design notes:
  - We use the 85th percentile (P85) by default. Reasoning: P75 fires too
    often on tickers with steady PM activity; P90 misses real news days on
    less-liquid names. P85 is a balance.
  - Floor at 2.0: no ticker should fire on less than 2x normal — anything
    below that is noise.
  - Cap at 10.0: thresholds higher than 10x rarely fire and miss actual
    news days even on the most stable mega-caps.
  - We fetch 1-minute bars and aggregate to daily PM totals locally rather
    than asking Polygon for pre-market aggregates, because Polygon's
    pre-market support is inconsistent across endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import numpy as np

# Allow direct invocation: python scripts/build_pm_rvol_thresholds.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"

# Defaults; can be overridden by build_thresholds() callers.
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_PERCENTILE = 85
DEFAULT_FLOOR = 2.0
DEFAULT_CAP = 10.0
DEFAULT_BASELINE_WINDOW = 20

# Polygon allows 5 concurrent requests on the typical Stocks Starter plan.
DEFAULT_CONCURRENCY = 5


async def _fetch_minute_bars(
    session: aiohttp.ClientSession,
    api_key: str,
    ticker: str,
    start_date: str,
    end_date: str,
    timeout: float = 30.0,
) -> list[dict] | None:
    """Fetch 1-minute aggregates for a ticker. Returns list of bar dicts or None."""
    url = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/"
        f"{start_date}/{end_date}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                logger.warning("Polygon %s for %s: HTTP %s", ticker, ticker, r.status)
                return None
            data = await r.json()
            return data.get("results", []) or []
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Polygon fetch error for %s: %s", ticker, e)
        return None


def _is_premarket_minute(ts_ms: int) -> bool:
    """Return True if a millisecond UTC timestamp falls in 4:00-9:30 ET."""
    dt_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    # America/New_York is UTC-5 in winter, UTC-4 in summer. Handle both.
    # Trading-platform deployment is in UTC so ZoneInfo via stdlib works.
    from zoneinfo import ZoneInfo
    et = dt_utc.astimezone(ZoneInfo("America/New_York"))
    if et.hour < 4:
        return False
    if et.hour > 9:
        return False
    if et.hour == 9 and et.minute >= 30:
        return False
    return True


def _aggregate_to_daily_pm_volume(
    bars: list[dict],
) -> list[tuple[str, int]]:
    """Aggregate 1-minute bars into daily PM totals.

    Returns list of (date_str, pm_volume) tuples sorted by date ascending.
    """
    from zoneinfo import ZoneInfo
    et_zone = ZoneInfo("America/New_York")
    daily: dict[str, int] = {}
    for bar in bars:
        ts_ms = bar.get("t", 0)
        vol = int(bar.get("v", 0))
        if vol <= 0:
            continue
        if not _is_premarket_minute(ts_ms):
            continue
        et = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(et_zone)
        day = et.date().isoformat()
        daily[day] = daily.get(day, 0) + vol
    return sorted(daily.items())


def _compute_threshold(
    daily_pm_volumes: list[tuple[str, int]],
    percentile: int,
    baseline_window: int,
    floor: float,
    cap: float,
) -> float | None:
    """Compute the per-ticker PM RVOL threshold from a daily series.

    Args:
        daily_pm_volumes: list of (date, pm_volume) tuples sorted ascending.
        percentile: which percentile of the historical RVOL distribution
            to set the threshold at.
        baseline_window: rolling-baseline length (typically 20 trading days).
        floor: minimum threshold value (clipped if computed below).
        cap: maximum threshold value (clipped if computed above).

    Returns:
        The computed threshold as a float in [floor, cap], or None if
        there's not enough data.
    """
    n = len(daily_pm_volumes)
    if n < baseline_window + 5:
        # Need at least baseline_window days plus a few more for stable percentile
        return None

    rvols: list[float] = []
    vols = [v for _, v in daily_pm_volumes]
    for i in range(baseline_window, n):
        baseline_slice = vols[i - baseline_window : i]
        baseline = float(np.mean([v for v in baseline_slice if v > 0]))
        if baseline <= 0:
            continue
        rvol = vols[i] / baseline
        if rvol > 0:
            rvols.append(rvol)

    if len(rvols) < 5:
        return None

    raw_threshold = float(np.percentile(rvols, percentile))
    return max(floor, min(cap, raw_threshold))


async def build_thresholds(
    api_key: str,
    tickers: list[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    percentile: int = DEFAULT_PERCENTILE,
    floor: float = DEFAULT_FLOOR,
    cap: float = DEFAULT_CAP,
    baseline_window: int = DEFAULT_BASELINE_WINDOW,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict[str, float]:
    """Compute per-ticker PM RVOL thresholds for a list of tickers.

    Returns a dict mapping ticker -> threshold. Tickers with insufficient
    data are skipped.
    """
    end = datetime.utcnow().date()
    start = end - timedelta(days=lookback_days * 2)  # double lookback to absorb non-trading days
    start_str = start.isoformat()
    end_str = end.isoformat()

    sem = asyncio.Semaphore(concurrency)
    thresholds: dict[str, float] = {}

    async with aiohttp.ClientSession() as session:
        async def process(ticker: str) -> None:
            async with sem:
                bars = await _fetch_minute_bars(
                    session, api_key, ticker, start_str, end_str,
                )
            if not bars:
                logger.info("No bars for %s", ticker)
                return
            daily = _aggregate_to_daily_pm_volume(bars)
            t = _compute_threshold(
                daily, percentile, baseline_window, floor, cap,
            )
            if t is None:
                logger.info("Insufficient PM history for %s", ticker)
                return
            thresholds[ticker] = round(t, 2)

        await asyncio.gather(*[process(t) for t in tickers])

    return thresholds


def write_thresholds_file(
    thresholds: dict[str, float],
    output_path: Path,
    percentile: int = DEFAULT_PERCENTILE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    floor: float = DEFAULT_FLOOR,
    cap: float = DEFAULT_CAP,
    default_threshold: float = 5.0,
) -> None:
    """Write the thresholds JSON file in the format expected by data/pm_rvol_thresholds.py."""
    output: dict = dict(thresholds)
    output["_default"] = default_threshold
    output["_metadata"] = {
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "lookback_days": lookback_days,
        "percentile": percentile,
        "floor": floor,
        "cap": cap,
        "n_tickers": len(thresholds),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, sort_keys=False)
    logger.info(
        "Wrote PM RVOL thresholds for %d tickers to %s",
        len(thresholds), output_path,
    )


async def refresh_pm_rvol_thresholds(
    api_key: str,
    watchlist: list[str],
    output_path: Path,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    percentile: int = DEFAULT_PERCENTILE,
    floor: float = DEFAULT_FLOOR,
    cap: float = DEFAULT_CAP,
) -> None:
    """High-level entry point: build and write thresholds in one call.

    Used by both the scheduled refresh in main.py and the manual script
    invocation below.
    """
    thresholds = await build_thresholds(
        api_key=api_key,
        tickers=watchlist,
        lookback_days=lookback_days,
        percentile=percentile,
        floor=floor,
        cap=cap,
    )
    write_thresholds_file(
        thresholds, output_path,
        percentile=percentile, lookback_days=lookback_days,
        floor=floor, cap=cap,
    )


def _main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    )

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print("ERROR: POLYGON_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    # Read watchlist
    watchlist_path = Path("config/watchlist_dynamic.json")
    if not watchlist_path.exists():
        print(f"ERROR: {watchlist_path} not found. Run watchlist refresh first.",
              file=sys.stderr)
        sys.exit(1)
    with open(watchlist_path) as f:
        wl_data = json.load(f)
    tickers = wl_data["watchlist"] if isinstance(wl_data, dict) else wl_data

    output_path = Path("config/pm_rvol_thresholds.json")

    asyncio.run(refresh_pm_rvol_thresholds(
        api_key=api_key,
        watchlist=tickers,
        output_path=output_path,
    ))


if __name__ == "__main__":
    _main()
