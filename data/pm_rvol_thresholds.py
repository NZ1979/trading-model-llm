"""Per-ticker PM RVOL threshold loader (Phase C, 2026-05-06).

The original strategy used a single global threshold (UNUSUAL_RVOL_THRESHOLD=5.0)
for is_unusual_volume across every ticker. That's wrong: AAPL's typical PM volume
is enormously stable, so 5x is a rare event; small-cap tickers see 5x several
times a week from random news, so the same threshold produces noise.

Phase C computes a per-ticker threshold from each ticker's own historical
distribution of daily PM RVOLs (typically the 85th percentile, clipped to a
sane floor and ceiling). This file is the runtime loader: it reads the JSON
file produced by `scripts/build_pm_rvol_thresholds.py` and provides a fast
lookup function for the live signal engine.

JSON file shape:
    {
        "AAPL": 3.8,
        "MSFT": 4.2,
        "NVDA": 5.1,
        ...
        "_default": 5.0,
        "_metadata": {
            "computed_at": "2026-05-06T12:00:00Z",
            "lookback_days": 180,
            "percentile": 85,
            "floor": 2.0,
            "cap": 10.0,
            "n_tickers": 500
        }
    }

Keys starting with `_` are reserved for metadata and the default fallback.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard fallback if neither the file nor a per-ticker entry exists.
HARD_FALLBACK_THRESHOLD = 5.0


def load_thresholds(path: Path | str) -> dict[str, Any]:
    """Load the per-ticker PM RVOL threshold JSON file.

    Returns a dict suitable for passing to get_threshold(). Returns an empty
    dict if the file is missing or malformed; callers will then receive
    HARD_FALLBACK_THRESHOLD for every lookup, which keeps the platform
    operational with the pre-Phase-C global threshold.

    Args:
        path: filesystem path to the JSON file (typically
            'config/pm_rvol_thresholds.json').

    Returns:
        Dict mapping ticker -> threshold (float), plus optionally a
        '_default' key (float) and a '_metadata' key (dict).
    """
    p = Path(path)
    if not p.exists():
        logger.warning(
            "PM RVOL thresholds file not found at %s; using hard fallback "
            "(%.1f) for all tickers", p, HARD_FALLBACK_THRESHOLD,
        )
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("PM RVOL thresholds JSON malformed at %s: %s", p, e)
        return {}
    if not isinstance(data, dict):
        logger.error(
            "PM RVOL thresholds file should be a dict, got %s", type(data),
        )
        return {}
    n_tickers = sum(1 for k in data if not k.startswith("_"))
    logger.info(
        "Loaded PM RVOL thresholds for %d tickers from %s "
        "(default=%.2f, metadata=%s)",
        n_tickers, p,
        data.get("_default", HARD_FALLBACK_THRESHOLD),
        data.get("_metadata", {}),
    )
    return data


def get_threshold(thresholds: dict[str, Any], ticker: str) -> float:
    """Get the PM RVOL threshold for a specific ticker.

    Lookup order:
      1. Exact ticker match in thresholds dict
      2. '_default' key in thresholds dict
      3. HARD_FALLBACK_THRESHOLD (5.0)

    Args:
        thresholds: dict from load_thresholds().
        ticker: symbol to look up.

    Returns:
        The threshold for this ticker as a float.
    """
    if ticker in thresholds:
        val = thresholds[ticker]
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
    default = thresholds.get("_default", HARD_FALLBACK_THRESHOLD)
    if isinstance(default, (int, float)) and default > 0:
        return float(default)
    return HARD_FALLBACK_THRESHOLD
