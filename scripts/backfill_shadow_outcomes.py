"""Backfill the shadow_outcomes table for historical decisions.

For every Buy/Sell decision in trading.db that does not yet have a
shadow_outcomes row, this script:
  1. Joins decisions to orders to recover limit_price + stop_price
  2. Computes a target_price using the same TP/stop ratio that Layer 1
     would have applied (configurable; default tp_atr=2.0, stop_atr=1.5)
  3. Fetches 1-min bars from Polygon for the decision's date
  4. Calls strategy.llm.metrics.compute_outcome to produce a ShadowOutcome
  5. Persists to the shadow_outcomes table

Hold decisions are skipped in this version (no order row, no price). They
can be added later by joining the decision timestamp to the bar feed at
that timestamp.

Idempotent: re-running skips decisions that already have outcomes.
Self-contained: creates the shadow_outcomes table if it doesn't exist
(so this works even on a VPS where main.py hasn't been restarted with
the v2 schema migration).

Usage:
    cd "C:\\trading\\LLM model"
    $env:PYTHONPATH = "."
    $env:POLYGON_API_KEY = '<your-key>'
    python scripts/backfill_shadow_outcomes.py
    python scripts/backfill_shadow_outcomes.py --db-path /opt/trader/app/trading.db --since 2026-04-01
    python scripts/backfill_shadow_outcomes.py --limit 50 --dry-run

On the VPS:
    set -a; source /etc/trading-platform/env; set +a
    cd /opt/trader/app
    /opt/trader/.venv/bin/python scripts/backfill_shadow_outcomes.py --db-path /opt/trader/app/trading.db
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import yaml

from data.polygon_feed import PolygonRESTClient
from strategy.llm.metrics import Bar, ShadowOutcome, compute_outcome

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--db-path", default=None,
        help="Path to trading.db. Default: read from config/settings.yaml",
    )
    p.add_argument(
        "--config", default="config/settings.yaml",
        help="Path to settings.yaml. Default: config/settings.yaml",
    )
    p.add_argument(
        "--since", default=None,
        help="Backfill only decisions on or after this date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Max decisions to process. Useful for smoke-testing.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute outcomes but do not write to shadow_outcomes table.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log INFO-level messages.",
    )
    return p.parse_args()


def _load_config(path: str) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def _read_decisions_to_backfill(
    db_path: Path, since: str | None, limit: int | None,
) -> list[dict]:
    """Pull (decision_id, ticker, ts, action, limit_price, stop_price) tuples
    for every Buy/Sell decision lacking a shadow_outcomes row.

    Joins decisions to orders on decision_id; only rows where the order
    has a limit_price are returnable (we need an entry price to compute
    forward returns against).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = [
        "d.action IN ('Buy', 'Sell')",
        "o.limit_price IS NOT NULL",
        "o.stop_price IS NOT NULL",
        "so.decision_id IS NULL",
    ]
    if since:
        where.append("d.ts >= ?")
        params: list = [datetime.strptime(since, "%Y-%m-%d").timestamp()]
    else:
        params = []
    sql = f"""
        SELECT d.id AS decision_id,
               d.ticker, d.ts, d.action, d.setup,
               o.limit_price, o.stop_price, o.qty, o.side
        FROM decisions d
        JOIN orders o ON o.decision_id = d.id
        LEFT JOIN shadow_outcomes so ON so.decision_id = d.id
        WHERE {' AND '.join(where)}
        ORDER BY d.ts ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def _group_by_ticker_date(decisions: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group decisions by (ticker, YYYY-MM-DD in ET) so we fetch one bar set per group."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in decisions:
        et_dt = datetime.fromtimestamp(d["ts"], tz=timezone.utc).astimezone(ET)
        key = (d["ticker"], et_dt.strftime("%Y-%m-%d"))
        grouped[key].append(d)
    return grouped


def _polygon_to_bar(raw: dict) -> Bar:
    """Convert a Polygon /v2/aggs bar (t in ms) to our metrics.Bar (ts in s)."""
    return Bar(
        ts=raw["t"] / 1000.0,
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=int(raw.get("v", 0)),
    )


def _eod_ts_for(date_str: str) -> float:
    """Return epoch seconds for 15:55 ET on the given YYYY-MM-DD."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET)
    eod = d.replace(hour=15, minute=55, second=0, microsecond=0)
    return eod.timestamp()


def _compute_target_price(
    side: str, limit_price: float, stop_price: float,
    tp_atr: float, stop_atr: float,
) -> float:
    """Compute the would-have-been target price using the configured TP/stop ratio.

    target_distance / stop_distance = tp_atr / stop_atr. So:
        target = entry +/- (tp_atr / stop_atr) * stop_distance
    where stop_distance = abs(entry - stop) and the sign matches the trade direction.
    """
    stop_dist = abs(limit_price - stop_price)
    target_dist = (tp_atr / stop_atr) * stop_dist
    if side == "buy":
        return limit_price + target_dist
    return limit_price - target_dist


def _ensure_schema(db_path: Path) -> None:
    """Create shadow_outcomes table if it doesn't already exist.

    Self-contained so this script can run on a VPS where main.py hasn't been
    restarted with the v2 _init_db migration. Idempotent.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS shadow_outcomes ("
            "decision_id INTEGER PRIMARY KEY, "
            "return_5m_pct REAL, return_15m_pct REAL, return_30m_pct REAL, "
            "return_60m_pct REAL, return_eod_pct REAL, "
            "mae_pct REAL, mfe_pct REAL, "
            "mae_at_minutes INTEGER, mfe_at_minutes INTEGER, "
            "stop_would_hit INTEGER, stop_hit_at_minutes INTEGER, "
            "target_would_hit INTEGER, target_hit_at_minutes INTEGER, "
            "first_touch TEXT, "
            "avg_spread_bps REAL, estimated_slippage_bps REAL, "
            "populated_at REAL NOT NULL, horizon_complete TEXT NOT NULL, "
            "FOREIGN KEY(decision_id) REFERENCES decisions(id))"
        )
        conn.commit()
    finally:
        conn.close()


def _persist_outcome(db_path: Path, outcome: ShadowOutcome) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO shadow_outcomes (
                decision_id,
                return_5m_pct, return_15m_pct, return_30m_pct, return_60m_pct, return_eod_pct,
                mae_pct, mfe_pct, mae_at_minutes, mfe_at_minutes,
                stop_would_hit, stop_hit_at_minutes,
                target_would_hit, target_hit_at_minutes,
                first_touch,
                avg_spread_bps, estimated_slippage_bps,
                populated_at, horizon_complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.decision_id,
                outcome.return_5m_pct, outcome.return_15m_pct, outcome.return_30m_pct,
                outcome.return_60m_pct, outcome.return_eod_pct,
                outcome.mae_pct, outcome.mfe_pct,
                outcome.mae_at_minutes, outcome.mfe_at_minutes,
                int(outcome.stop_would_hit), outcome.stop_hit_at_minutes,
                int(outcome.target_would_hit), outcome.target_hit_at_minutes,
                outcome.first_touch,
                outcome.avg_spread_bps, outcome.estimated_slippage_bps,
                time.time(), outcome.horizon_complete,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def run_backfill(
    db_path: Path,
    polygon_key: str,
    tp_atr: float,
    stop_atr: float,
    *,
    since: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    log_via_print: bool = True,
) -> dict[str, int]:
    """Programmatic entry point. Used by both the CLI ``main()`` and
    ``main.py``'s daily routine follower.

    Returns a dict with keys ``candidates``, ``success``, ``skipped``,
    ``failed`` so callers can log a one-line summary without scraping
    print output.

    ``log_via_print``: when True (CLI), per-row progress goes through
    ``print()``. When False (daily-routine follower), per-row progress
    is suppressed and only the summary numbers are surfaced via the
    return value. The CLI keeps its existing tone; the daemon stays
    quiet on a healthy day and lets the summary line speak for itself.
    """
    _ensure_schema(db_path)
    decisions = _read_decisions_to_backfill(db_path, since, limit)
    if not decisions:
        if log_via_print:
            print(
                "No Buy/Sell decisions to backfill (table empty, all already "
                "populated, or filter excluded all rows)."
            )
        return {"candidates": 0, "success": 0, "skipped": 0, "failed": 0}

    if log_via_print:
        print(f"Found {len(decisions)} decisions to backfill")
    grouped = _group_by_ticker_date(decisions)
    if log_via_print:
        print(f"Grouped into {len(grouped)} (ticker, date) fetches")

    success = 0
    skipped = 0
    failed = 0

    polygon = PolygonRESTClient(api_key=polygon_key)
    try:
        for (ticker, date_str), day_decisions in sorted(grouped.items()):
            try:
                raw_bars = await polygon.get_minute_aggregates(
                    ticker, date_str, date_str
                )
            except Exception as exc:
                logger.exception(
                    "Polygon fetch failed for %s on %s", ticker, date_str
                )
                if log_via_print:
                    print(f"  FAIL fetch {ticker} {date_str}: {exc}")
                failed += len(day_decisions)
                continue

            if not raw_bars:
                if log_via_print:
                    print(f"  SKIP {ticker} {date_str}: Polygon returned 0 bars")
                skipped += len(day_decisions)
                continue

            bars = [_polygon_to_bar(b) for b in raw_bars]
            eod_ts = _eod_ts_for(date_str)

            for d in day_decisions:
                relevant = [b for b in bars if b.ts >= d["ts"]]
                if not relevant:
                    if log_via_print:
                        print(
                            f"  SKIP decision {d['decision_id']} ({ticker} @ "
                            f"{date_str}): no bars after decision_ts"
                        )
                    skipped += 1
                    continue

                target_price = _compute_target_price(
                    d["side"], d["limit_price"], d["stop_price"],
                    tp_atr, stop_atr,
                )

                outcome = compute_outcome(
                    decision_id=d["decision_id"],
                    decision_ts=d["ts"],
                    decision_price=d["limit_price"],
                    side=d["side"],
                    stop_price=d["stop_price"],
                    target_price=target_price,
                    bars=relevant,
                    eod_ts=eod_ts,
                )

                if dry_run:
                    if log_via_print:
                        print(
                            f"  DRY decision {d['decision_id']} ({ticker} "
                            f"{d['side']} @ ${d['limit_price']:.2f}): "
                            f"first_touch={outcome.first_touch} "
                            f"5m={_fmt(outcome.return_5m_pct)} "
                            f"60m={_fmt(outcome.return_60m_pct)} "
                            f"eod={_fmt(outcome.return_eod_pct)} "
                            f"mae={_fmt(outcome.mae_pct)} "
                            f"mfe={_fmt(outcome.mfe_pct)}"
                        )
                else:
                    _persist_outcome(db_path, outcome)
                success += 1
    finally:
        await polygon.aclose()

    return {
        "candidates": len(decisions),
        "success": success,
        "skipped": skipped,
        "failed": failed,
    }


async def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    config = _load_config(args.config)
    db_path = Path(args.db_path or config["storage"]["db_path"])
    if not db_path.exists():
        print(f"ERROR: trading.db not found at {db_path}")
        return 1

    risk_cfg = config.get("risk", {})
    tp_atr = float(risk_cfg.get("take_profit_atr_multiple", 2.0))
    stop_atr = float(risk_cfg.get("stop_atr_multiplier", 1.5))
    print(f"Using tp_atr={tp_atr}, stop_atr={stop_atr} (R/R = {tp_atr/stop_atr:.2f}:1)")

    polygon_key = os.environ.get("POLYGON_API_KEY")
    if not polygon_key:
        print("ERROR: POLYGON_API_KEY not set in environment")
        return 1

    summary = await run_backfill(
        db_path=db_path,
        polygon_key=polygon_key,
        tp_atr=tp_atr,
        stop_atr=stop_atr,
        since=args.since,
        limit=args.limit,
        dry_run=args.dry_run,
        log_via_print=True,
    )

    print()
    print(
        f"Backfill summary: success={summary['success']} "
        f"skipped={summary['skipped']} failed={summary['failed']}"
    )
    if args.dry_run:
        print("(Dry run: no rows written to shadow_outcomes)")
    return 0 if summary["failed"] == 0 else 2


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
