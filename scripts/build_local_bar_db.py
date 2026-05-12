"""Build a local minute-bar database from Polygon historical aggregates.

Output: partitioned Parquet files at
    <base_dir>/bars/1min/ticker=<TICKER>/year=<YYYY>.parquet

Queryable via DuckDB:
    SELECT * FROM read_parquet('D:/trading_data/bars/1min/**/*.parquet')
    WHERE ticker = 'AAPL' AND ts >= '2025-01-01';

Idempotent. Existing ticker-year Parquet files are skipped (delete the
file to force a re-pull). Smoke runs write to bars/1min_smoke/ so they
cannot pollute production partitions.

Per CLAUDE_PREFLIGHT.md Rule 22, the Polygon API key is passed as a URL
query parameter. This script uses aiohttp (safe at INFO by default) and
also forces aiohttp/urllib3/httpx loggers to WARNING regardless of
--verbose, so the credential never lands in INFO-level URL logs.

Usage (PowerShell):
    # Smoke: 5 mega-caps, last 30 days, writes to bars/1min_smoke/
    python scripts/build_local_bar_db.py --base-dir D:/trading_data --smoke

    # Watchlist (config/watchlist_dynamic.json), N years back
    python scripts/build_local_bar_db.py --base-dir D:/trading_data --universe watchlist --years 1

S&P 500 and Russell 3000 universes are not wired in this v1; extend
_load_universe() with a snapshot at config/<universe>.json when ready.

Polygon Stocks Starter subscription required for 5y history.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

POLYGON_AGGS_URL = (
    "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{from_}/{to}"
)
POLYGON_LIMIT = 50000
SMOKE_TICKERS = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-dir", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--smoke", action="store_true",
                   help="5 mega-caps, last 30 days, separate smoke dir")
    g.add_argument("--universe", choices=["watchlist", "sp500", "russell3000"],
                   help="Production ticker universe (watchlist, sp500, or russell3000)")
    p.add_argument("--years", type=int, default=1,
                   help="Years of history for --universe runs (default 1)")
    p.add_argument("--concurrency", type=int, default=8,
                   help="Max concurrent Polygon requests (default 8)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    for noisy in ("aiohttp", "aiohttp.access", "aiohttp.client",
                  "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _require_api_key() -> str:
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        sys.exit(
            "ERROR: POLYGON_API_KEY not set. Run in PowerShell:\n"
            "    $env:POLYGON_API_KEY = 'your-key'"
        )
    return key


def _load_universe(universe: str) -> list[str]:
    if universe == "watchlist":
        candidates = [
            Path("config/watchlist_dynamic.json"),
            Path("config/watchlist.json"),
        ]
        for wl_path in candidates:
            if wl_path.exists():
                with open(wl_path) as f:
                    data = json.load(f)
                # Production watchlist_dynamic.json uses key 'watchlist'
                # (see data/watchlist_builder.py). Accept 'tickers' too,
                # or a bare list, for hand-edited files.
                if isinstance(data, dict):
                    tickers = data.get("watchlist") or data.get("tickers")
                elif isinstance(data, list):
                    tickers = data
                else:
                    tickers = None
                if not isinstance(tickers, list) or not tickers:
                    sys.exit(
                        f"ERROR: {wl_path} has unexpected shape "
                        f"(expected list, dict with 'watchlist' key, "
                        f"or dict with 'tickers' key)"
                    )
                return sorted(set(str(t).upper() for t in tickers))
        sys.exit("ERROR: no watchlist file at config/watchlist_dynamic.json "
                 "or config/watchlist.json")
    if universe == "sp500":
        sp_path = Path("config/sp500.json")
        if not sp_path.exists():
            sys.exit("ERROR: config/sp500.json not found. "
                     "Run: python scripts/fetch_sp500.py")
        with open(sp_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            tickers = data.get("tickers")
        elif isinstance(data, list):
            tickers = data
        else:
            tickers = None
        if not isinstance(tickers, list) or not tickers:
            sys.exit(f"ERROR: {sp_path} has unexpected shape")
        return sorted(set(str(t).upper() for t in tickers))
    if universe == "russell3000":
        rs_path = Path("config/russell3000.json")
        if not rs_path.exists():
            sys.exit("ERROR: config/russell3000.json not found. "
                     "Run: python scripts/fetch_russell3000.py")
        with open(rs_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            tickers = data.get("tickers")
        elif isinstance(data, list):
            tickers = data
        else:
            tickers = None
        if not isinstance(tickers, list) or not tickers:
            sys.exit(f"ERROR: {rs_path} has unexpected shape")
        return sorted(set(str(t).upper() for t in tickers))
    sys.exit(f"ERROR: universe {universe!r} not wired in v1 script")


def _year_range(years_back: int) -> list[int]:
    today = date.today()
    return list(range(today.year - years_back + 1, today.year + 1))


def _smoke_window() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=30)).isoformat(), today.isoformat()


def _output_root(base_dir: Path, smoke: bool) -> Path:
    return base_dir / "bars" / ("1min_smoke" if smoke else "1min")


def _ticker_year_path(out_root: Path, ticker: str, year: int) -> Path:
    return out_root / f"ticker={ticker}" / f"year={year}.parquet"


def _build_log_path(base_dir: Path) -> Path:
    return base_dir / "meta" / "build_log.json"


def _load_build_log(base_dir: Path) -> dict[str, Any]:
    p = _build_log_path(base_dir)
    if not p.exists():
        return {"entries": {}, "runs": []}
    with open(p) as f:
        return json.load(f)


def _save_build_log(base_dir: Path, log: dict[str, Any]) -> None:
    p = _build_log_path(base_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(log, f, indent=2, sort_keys=True)
    tmp.replace(p)


async def _fetch_paginated(
    session: aiohttp.ClientSession,
    url: str,
    api_key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    current_url = url
    current_params = dict(params or {})
    current_params["apiKey"] = api_key
    while True:
        async with session.get(
            current_url,
            params=current_params,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
            data = await resp.json()
        results.extend(data.get("results") or [])
        next_url = data.get("next_url")
        if not next_url:
            return results
        current_url = next_url
        current_params = {"apiKey": api_key}


async def _fetch_ticker_window(
    session: aiohttp.ClientSession,
    ticker: str,
    from_date: str,
    to_date: str,
    api_key: str,
) -> list[dict[str, Any]]:
    url = POLYGON_AGGS_URL.format(ticker=ticker, from_=from_date, to=to_date)
    params = {"adjusted": "true", "sort": "asc", "limit": POLYGON_LIMIT}
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await _fetch_paginated(session, url, api_key, params)
        except Exception as exc:
            last_exc = exc
            if attempt >= RETRY_ATTEMPTS:
                break
            backoff = RETRY_BACKOFF_BASE ** attempt
            logger.warning("Retry %d for %s %s-%s after %ds: %s",
                           attempt, ticker, from_date, to_date, backoff, exc)
            await asyncio.sleep(backoff)
    assert last_exc is not None
    raise last_exc


def _bars_to_dataframe(ticker: str, raw_bars: list[dict[str, Any]]) -> pd.DataFrame:
    if not raw_bars:
        return pd.DataFrame()
    df = pd.DataFrame(raw_bars)
    df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={
        "o": "open", "h": "high", "l": "low", "c": "close",
        "v": "volume", "vw": "vwap", "n": "n_trades",
    })
    df["ticker"] = ticker
    cols = ["ticker", "ts", "open", "high", "low", "close", "vwap", "volume", "n_trades"]
    return df[[c for c in cols if c in df.columns]]


def _write_parquet(df: pd.DataFrame, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, engine="pyarrow", compression="snappy", index=False)
    return path.stat().st_size


async def _process(
    semaphore: asyncio.Semaphore,
    session: aiohttp.ClientSession,
    ticker: str,
    year: int,
    out_root: Path,
    from_date: str,
    to_date: str,
    api_key: str,
) -> dict[str, Any]:
    key = f"{ticker}/{year}/{from_date}_to_{to_date}"
    out_path = _ticker_year_path(out_root, ticker, year)
    if out_path.exists():
        return {"key": key, "status": "skipped_exists", "rows": 0,
                "bytes": out_path.stat().st_size}
    async with semaphore:
        t0 = time.monotonic()
        try:
            raw = await _fetch_ticker_window(session, ticker, from_date, to_date, api_key)
        except Exception as exc:
            return {"key": key, "status": "http_error", "error": str(exc)[:200]}
    df = _bars_to_dataframe(ticker, raw)
    if df.empty:
        return {"key": key, "status": "empty", "rows": 0, "bytes": 0,
                "from": from_date, "to": to_date}
    try:
        n_bytes = _write_parquet(df, out_path)
    except Exception as exc:
        return {"key": key, "status": "write_error", "error": str(exc)[:200]}
    return {
        "key": key,
        "status": "ok",
        "rows": len(df),
        "bytes": n_bytes,
        "from": from_date,
        "to": to_date,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


async def main() -> int:
    args = _parse_args()
    _setup_logging(args.verbose)
    api_key = _require_api_key()
    base_dir: Path = args.base_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    out_root = _output_root(base_dir, args.smoke)

    if args.smoke:
        tickers = SMOKE_TICKERS
        smoke_from, smoke_to = _smoke_window()
        jobs = [(t, date.today().year, smoke_from, smoke_to) for t in tickers]
        print(f"SMOKE: {len(tickers)} tickers ({tickers}), window {smoke_from} -> {smoke_to}")
    else:
        tickers = _load_universe(args.universe)
        today = date.today()
        years = _year_range(args.years)
        jobs = []
        for t in tickers:
            for y in years:
                from_d = f"{y}-01-01"
                to_d = today.isoformat() if y == today.year else f"{y}-12-31"
                jobs.append((t, y, from_d, to_d))
        print(f"BUILD: {len(tickers)} tickers x {len(years)} years = {len(jobs)} jobs")

    semaphore = asyncio.Semaphore(args.concurrency)
    counter: Counter[str] = Counter()
    total_rows = 0
    total_bytes = 0
    run_t0 = time.monotonic()
    build_log = _load_build_log(base_dir)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        coros = [
            _process(semaphore, session, t, y, out_root, fd, td, api_key)
            for (t, y, fd, td) in jobs
        ]
        completed = 0
        for fut in asyncio.as_completed(coros):
            r = await fut
            counter[r["status"]] += 1
            total_rows += r.get("rows", 0) or 0
            total_bytes += r.get("bytes", 0) or 0
            build_log["entries"][r["key"]] = r
            completed += 1
            if completed % 25 == 0 or completed == len(jobs):
                elapsed = time.monotonic() - run_t0
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"  [{completed}/{len(jobs)}] {dict(counter)} "
                      f"rows={total_rows:,} bytes={total_bytes/1e9:.2f}GB "
                      f"rate={rate:.1f}/s")

    build_log["runs"].append({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "summary": dict(counter),
        "total_rows": total_rows,
        "total_bytes": total_bytes,
        "elapsed_s": round(time.monotonic() - run_t0, 2),
    })
    build_log["runs"] = build_log["runs"][-20:]
    _save_build_log(base_dir, build_log)

    elapsed = time.monotonic() - run_t0
    print()
    print(f"DONE in {elapsed:.1f}s")
    print(f"  Status counts : {dict(counter)}")
    print(f"  Total rows    : {total_rows:,}")
    print(f"  Total bytes   : {total_bytes/1e9:.2f} GB")
    print(f"  Output root   : {out_root}")
    print(f"  Build log     : {_build_log_path(base_dir)}")

    errors = counter.get("http_error", 0) + counter.get("write_error", 0)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
