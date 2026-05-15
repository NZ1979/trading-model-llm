"""FRED VIX loader — daily EOD CBOE Volatility Index from St. Louis Fed.

Series ID ``VIXCLS`` (CBOE Volatility Index: VIX) — daily close, published
T+1 by the Federal Reserve Bank of St. Louis. Authoritative source; no
schema-drift risk like the Yahoo Finance route.

Why FRED over Polygon Indices:

- Polygon Stocks Starter (current subscription) does NOT include
  indices. The ``I:VIX`` endpoint returns 403, verified 2026-05-13.
- Polygon Indices Starter is $29/month for data we only need at daily
  granularity. FRED gives the same data for free with a stable API
  contract and a 30-year track record.

The regime classifier (``analysis/regime.py``) uses ``vix_level`` against
a 60-day median. Daily EOD is the right granularity — VIX's medium-term
level (what the classifier cares about) does not move tick-to-tick.

Rule 22 trap: FRED puts ``api_key`` as a URL query parameter. The
``setup_logging`` block in ``main.py`` forcing ``httpx`` + ``httpcore``
to WARNING keeps it out of journalctl in production. This module
additionally scrubs URLs in exception messages, matching the pattern
in ``data/polygon_feed.py``. Callers that use this module OUTSIDE
``main.py`` (verify scripts, replay harness CLI) MUST set the httpx
logger level to WARNING themselves before instantiating an
``httpx.AsyncClient``.

Environment:
    FRED_API_KEY: required. Sign up at
        https://fred.stlouisfed.org/docs/api/api_key.html (free, instant).
        Set via PowerShell session env or via .env at repo root
        (.env is gitignored).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, timedelta

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

FRED_API_BASE = "https://api.stlouisfed.org/fred"
VIX_SERIES_ID = "VIXCLS"


# ---------------------------------------------------------------------------
# Rule 22 defense-in-depth: scrub api_key from exception messages
# ---------------------------------------------------------------------------

_APIKEY_RE = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def _scrub_apikey(s: str) -> str:
    """Replace any ``api_key=<value>`` in ``s`` with ``api_key=<redacted>``.

    Matches FRED's query-param name. Idempotent. Safe to call on
    already-clean strings. Used wherever we construct an error message
    that might include a FRED URL with credentials embedded.
    """
    return _APIKEY_RE.sub(r"\1<redacted>", s)


# ---------------------------------------------------------------------------
# Env-var loading
# ---------------------------------------------------------------------------


def _require_fred_key() -> str:
    """Read ``FRED_API_KEY`` from env; raise loud if missing (Rule 18).

    Read at call time, not at import time, so:

    - Tests can monkeypatch the env without re-importing the module.
    - A misconfigured deployment fails with a clear message at first
      use, not at import.
    """
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError(
            "FRED_API_KEY not set in environment. Sign up at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and either: "
            "(PowerShell session) $env:FRED_API_KEY = '<key>' OR "
            "(.env at repo root, gitignored) add FRED_API_KEY=<key>."
        )
    return key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_vix_history(
    start_date: date,
    end_date: date,
    *,
    timeout_s: float = 15.0,
    max_retries: int = 3,
) -> pd.DataFrame:
    """Fetch VIX daily close over ``[start_date, end_date]`` inclusive.

    Returns a DataFrame indexed by tz-aware UTC midnight date,
    sorted ascending, with a single column ``vix_close`` (float). Rows
    where FRED returns the sentinel ``"."`` (no observation — weekend,
    holiday, pre-1990 etc.) are skipped, NOT propagated as NaN. Empty
    result over a non-empty window is a fatal error per Rule 18.

    Args:
        start_date: inclusive (maps to FRED's ``observation_start``).
        end_date: inclusive (maps to FRED's ``observation_end``).
        timeout_s: per-request timeout.
        max_retries: retries on 5xx and transient network errors.
            Exponential backoff 1s, 2s, 4s.

    Raises:
        ValueError: ``end_date < start_date``.
        RuntimeError: missing FRED_API_KEY; 4xx from FRED (bad key,
            unknown series, malformed request); 5xx persisting after
            retries; transient network error persisting after retries;
            empty observations array; all observations were sentinels.
    """
    if end_date < start_date:
        raise ValueError(
            f"end_date {end_date} is before start_date {start_date}"
        )

    key = _require_fred_key()

    params = {
        "series_id": VIX_SERIES_ID,
        "api_key": key,
        "file_type": "json",
        "observation_start": start_date.isoformat(),
        "observation_end": end_date.isoformat(),
        "sort_order": "asc",
    }
    url = f"{FRED_API_BASE}/series/observations"

    last_err: Exception | None = None
    payload = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                break
        except httpx.HTTPStatusError as e:
            if 500 <= e.response.status_code < 600:
                last_err = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                # Fall through to RuntimeError after the loop.
                break
            # 4xx: don't retry; raise immediately with a scrubbed message.
            safe_url = _scrub_apikey(str(e.response.url))
            body_snippet = _scrub_apikey(e.response.text[:200])
            raise RuntimeError(
                f"FRED HTTP {e.response.status_code} for {safe_url}: "
                f"{body_snippet}"
            ) from None
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            last_err = e
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            break

    if payload is None:
        raise RuntimeError(
            f"FRED VIX fetch failed after {max_retries} retries: "
            f"{_scrub_apikey(str(last_err))}"
        ) from None

    observations = payload.get("observations", [])
    if not observations:
        raise RuntimeError(
            f"FRED returned 0 observations for {VIX_SERIES_ID} over "
            f"{start_date.isoformat()}..{end_date.isoformat()}. "
            f"FRED has VIX from 1990-01-02 onward; check the date range."
        )

    rows: list[tuple[pd.Timestamp, float]] = []
    for obs in observations:
        val_str = obs.get("value", ".")
        if val_str == ".":
            continue
        try:
            val = float(val_str)
        except (TypeError, ValueError):
            logger.warning(
                "FRED returned non-numeric VIX value %r on %s; skipping",
                val_str, obs.get("date"),
            )
            continue
        ts = pd.Timestamp(obs["date"], tz="UTC")
        rows.append((ts, val))

    if not rows:
        raise RuntimeError(
            f"FRED returned only sentinel '.' values over "
            f"{start_date.isoformat()}..{end_date.isoformat()}. "
            f"No usable VIX data in this window."
        )

    df = pd.DataFrame(rows, columns=["date", "vix_close"])
    df = df.set_index("date").sort_index()
    return df


async def get_vix_eod(
    target_date: date,
    *,
    lookback_days: int = 7,
) -> float | None:
    """Fetch VIX close for ``target_date`` (or the most recent prior trading day).

    Walks backward up to ``lookback_days`` calendar days when
    ``target_date`` falls on a weekend or US market holiday. Returns
    the close for the resolved trading day, or ``None`` if no
    observation found within the lookback window.

    Used by the live system to get "today's VIX" for the regime
    classifier. Typically resolves to ``target_date`` itself for
    weekdays after FRED's T+1 publication, or to ``target_date - 1``
    early in the day before FRED publishes.

    Args:
        target_date: the date to look up.
        lookback_days: max calendar days to walk back; default 7
            handles long holiday weekends comfortably.

    Returns:
        VIX close (float) or None.
    """
    start = target_date - timedelta(days=lookback_days)
    df = await get_vix_history(start, target_date)
    if df.empty:
        return None
    return float(df.iloc[-1]["vix_close"])
