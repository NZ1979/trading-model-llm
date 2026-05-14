"""Verify the regime classifier over recent market history.

Pulls SPY and VIX daily bars from Polygon for the last ~370 calendar days
(roughly 250 trading days + a 50-bar buffer to seed the SMA), then walks
the trailing 252 trading days computing ``classify_regime`` on each as-of
date as if it were live that day. Prints:

  - count + percentage of each label
  - day-to-day transition matrix (how often each label flowed to each
    other label across the window)
  - the 10 most recent labelled days, for eyeball sanity

This is the Rule-14 verification gate for ``analysis/regime.py``: we do
not declare the classifier deployed until the distribution looks sane on
known history. If trending_up is 95% of the window or crash never
appears even during known stress periods, the thresholds need tuning
before this lands in any live code path.

Run on Godzilla (the LLM-model workstation), NOT on the VPS — Rule 26
keeps LLM-model work off the gap-and-go infrastructure.

Usage (PowerShell, on Godzilla):

    cd "C:\\trading\\LLM model"
    .\\.venv\\Scripts\\Activate.ps1
    $env:POLYGON_API_KEY = '<your-polygon-key>'
    python scripts/verify_regime.py

The script makes exactly two Polygon REST calls (one SPY, one VIX). On
Stocks Starter that's a few seconds round-trip. POLYGON_API_KEY travels
via env var only; the script never echoes the key per Rule 21.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Defense-in-depth Rule-22 scrubber. data/polygon_feed.py's _get_with_retry
# already redacts apiKey from RuntimeError messages it raises, but any
# OTHER library (httpx itself in an unexpected code path, a future
# dependency, asyncio's exception formatting) might still surface the
# URL. We wrap the entire script entry point in a try/except that runs
# the final traceback through this scrubber before it hits stderr.
_APIKEY_TRACEBACK_RE = re.compile(r"(apiKey=)[^&\s'\"]+", re.IGNORECASE)


def _scrub_traceback(s: str) -> str:
    return _APIKEY_TRACEBACK_RE.sub(r"\1<redacted>", s)

# Make `from analysis...` and `from data...` work when running as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.regime import (  # noqa: E402
    MarketRegime,
    RegimeInputs,
    classify_regime,
)
from data.polygon_feed import PolygonRESTClient  # noqa: E402

# Walk this many trading days backward from the most recent fetched bar.
WALK_TRADING_DAYS = 252

# History we need to seed the longest indicator (60-day VIX median or
# 50-day SMA) before we start labelling. Set higher than 60 to leave a
# small buffer for missing days.
WARMUP_TRADING_DAYS = 65

# Total trading days we need to fetch = walk + warmup.
TOTAL_TRADING_DAYS = WALK_TRADING_DAYS + WARMUP_TRADING_DAYS

# Convert trading days to calendar days for the Polygon date range.
def _calendar_days_for(trading_days: int) -> int:
    return int(trading_days * 7 / 5) + 14  # 14-day holiday buffer


def _bars_to_dated_closes(bars: list[dict]) -> list[tuple[date, float]]:
    """Polygon daily bars → (date, close) tuples, oldest first.

    Polygon's ``t`` field on daily bars is epoch_ms of the trading day
    at midnight UTC. We convert via UTC to keep this deterministic
    regardless of where the verifier runs (Godzilla MDT vs CI UTC vs
    anywhere else)."""
    out: list[tuple[date, float]] = []
    for b in bars:
        ts_utc = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc)
        out.append((ts_utc.date(), float(b["c"])))
    return out


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def _build_inputs_at_index(
    spy_closes: list[float],
    vix_closes_by_date: dict[date, float],
    spy_dates: list[date],
    i: int,
) -> RegimeInputs:
    """Build a RegimeInputs as if the as-of date were spy_dates[i].

    Uses only spy_closes[:i+1] and VIX closes dated ≤ spy_dates[i].
    Mirrors the math in regime_data._compute_spy_stats and the VIX
    median computation in regime_data.fetch_regime_inputs."""
    last_close = spy_closes[i]
    return_20d = (last_close / spy_closes[i - 20]) - 1.0
    sma_50 = sum(spy_closes[i - 49: i + 1]) / 50.0
    breadth_proxy = (last_close / sma_50) - 1.0

    as_of = spy_dates[i]
    vix_history = [v for d, v in sorted(vix_closes_by_date.items()) if d <= as_of]
    if len(vix_history) < 30:
        vix_level: float | None = None
        vix_median: float | None = None
    else:
        vix_level = vix_history[-1]
        vix_median = _median(vix_history[-60:])

    return RegimeInputs(
        spy_return_20d=return_20d,
        vix_level=vix_level,
        vix_60d_median=vix_median,
        breadth_proxy=breadth_proxy,
    )


def _print_distribution(labels: list[tuple[date, MarketRegime]]) -> None:
    counts = Counter(label for _, label in labels)
    total = len(labels)
    print("Regime distribution over %d trading days:" % total)
    for regime in ("trending_up", "trending_down", "choppy", "crash", "unknown"):
        n = counts.get(regime, 0)
        pct = (100.0 * n / total) if total else 0.0
        bar = "█" * int(round(pct / 2))   # 0–50 chars
        print(f"  {regime:<14} {n:>4}  {pct:>5.1f}%  {bar}")
    print()


def _print_transitions(labels: list[tuple[date, MarketRegime]]) -> None:
    if len(labels) < 2:
        return
    transitions: dict[tuple[MarketRegime, MarketRegime], int] = defaultdict(int)
    for (_, a), (_, b) in zip(labels[:-1], labels[1:]):
        transitions[(a, b)] += 1
    regimes_seen = sorted({r for pair in transitions for r in pair})
    if not regimes_seen:
        return
    print("Transition matrix (rows = from, cols = to):")
    header = "  from \\ to  " + " ".join(f"{r[:8]:>10}" for r in regimes_seen)
    print(header)
    for r_from in regimes_seen:
        row = f"  {r_from[:10]:<11} " + " ".join(
            f"{transitions.get((r_from, r_to), 0):>10}" for r_to in regimes_seen
        )
        print(row)
    print()


def _print_recent(labels: list[tuple[date, MarketRegime]], n: int = 10) -> None:
    print(f"Most recent {n} labelled days:")
    for d, label in labels[-n:]:
        print(f"  {d.isoformat()}  {label}")
    print()


async def main() -> int:
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        print(
            "POLYGON_API_KEY not set. Export it in the shell before running. "
            "Per Rule 21 we do not echo the value back; the script just bails.",
            file=sys.stderr,
        )
        return 1

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=_calendar_days_for(TOTAL_TRADING_DAYS))
    print(f"Fetching SPY + VIX daily bars from {start} to {end}...")

    async with PolygonRESTClient(api_key=api_key) as client:
        spy_bars = await client.get_daily_aggregates(
            symbol="SPY",
            from_date=start.isoformat(),
            to_date=end.isoformat(),
            adjusted=True,
        )
        try:
            vix_bars = await client.get_daily_aggregates(
                symbol="I:VIX",
                from_date=start.isoformat(),
                to_date=end.isoformat(),
                adjusted=True,
            )
        except Exception as exc:
            print(
                f"VIX fetch failed ({exc!r}); verifier will run in VIX-less "
                "degraded mode and crash labels will not be reachable.",
                file=sys.stderr,
            )
            vix_bars = []

    if not spy_bars:
        print("SPY fetch returned no bars — cannot verify.", file=sys.stderr)
        return 2

    spy_dated = _bars_to_dated_closes(spy_bars)
    vix_dated = _bars_to_dated_closes(vix_bars)
    spy_dates = [d for d, _ in spy_dated]
    spy_closes = [c for _, c in spy_dated]
    vix_by_date = {d: c for d, c in vix_dated}

    print(
        f"Got {len(spy_dated)} SPY bars (range {spy_dates[0]} .. {spy_dates[-1]}), "
        f"{len(vix_dated)} VIX bars."
    )
    print()

    # The earliest day we can label is the first day we have 50 SPY
    # closes through (and ≥ 20 for the return). i = 49 is the first
    # eligible index.
    first_i = 49
    if first_i >= len(spy_closes):
        print(
            f"Only {len(spy_closes)} SPY bars; need at least 50 to label "
            "any day. Widen the lookback or check the date range.",
            file=sys.stderr,
        )
        return 3

    labels: list[tuple[date, MarketRegime]] = []
    for i in range(first_i, len(spy_closes)):
        inputs = _build_inputs_at_index(spy_closes, vix_by_date, spy_dates, i)
        labels.append((spy_dates[i], classify_regime(inputs)))

    print(
        f"Labelled {len(labels)} trading days "
        f"({labels[0][0]} .. {labels[-1][0]})."
    )
    print()

    _print_distribution(labels)
    _print_transitions(labels)
    _print_recent(labels)

    # Sanity gate: classifier must never emit 'unknown' on real data
    # (it cannot per the function's design, but verify here so a future
    # refactor can't silently break the invariant).
    if any(label == "unknown" for _, label in labels):
        print(
            "FAIL: classifier produced 'unknown' on real data. "
            "This breaks the module's central invariant.",
            file=sys.stderr,
        )
        return 4

    print("OK: classifier never returned 'unknown' across the window.")
    return 0


if __name__ == "__main__":
    # Rule-22 defense-in-depth: scrub apiKey from any traceback we might
    # emit before it reaches stderr. The expected raise paths already
    # use _scrub_apikey in data/polygon_feed.py, but a surprise leak
    # path (a new dependency, an asyncio internal) would not, so this
    # final scrubber catches anything else.
    try:
        rc = asyncio.run(main())
    except SystemExit:
        raise
    except BaseException:
        sys.stderr.write(_scrub_traceback(traceback.format_exc()))
        sys.exit(5)
    sys.exit(rc)
