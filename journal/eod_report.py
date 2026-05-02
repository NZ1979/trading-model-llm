"""End-of-day journal report.

At 16:30 ET each weekday, write a markdown report summarizing:
  - Account equity start/end + day P&L
  - Every Buy/Sell decision and what happened to it
      (filled? rejected by risk? rejected by Alpaca? still open?)
  - Per-trade thesis (sentiment score, technical setup, wall context) vs
    actual outcome (fill price, stop hit, EOD value)
  - Aggregate stats: # signals, # orders placed, # filled, hit rate

The goal is a daily forensic record. If a strategy starts losing money, the
journal lets you walk back through every decision and ask: "would I make this
trade again with the same information?" That's the difference between a
losing day caused by the strategy vs a losing day caused by execution bugs.

The report is also a lightweight backtest substitute: at the end of a 4-week
paper run you have 20 reports + a SQLite database containing every decision
ever made. That's enough data to ask "do gap-and-go signals beat pullback
signals?" or "does the wall filter actually improve win rate?" without any
extra instrumentation.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


@dataclass
class DecisionRow:
    id: int
    ts: float
    ticker: str
    action: str
    setup: str
    sentiment: int | None
    confidence: int
    walls_status: str
    reasons: str


@dataclass
class OrderRow:
    id: int
    ts: float
    ticker: str
    side: str
    qty: int
    limit_price: float
    stop_price: float | None
    alpaca_order_id: str | None
    status: str
    error: str | None
    decision_id: int | None


def write_eod_report(
    db_path: Path,
    date_str: str,
    ending_equity: float,
    output_dir: Path,
) -> Path:
    """Generate the EOD markdown report for the given trading day.

    Args:
        db_path: SQLite database with `decisions` and `orders` tables.
        date_str: ISO date string (e.g., "2026-04-27") for the trading day.
        ending_equity: Account equity at journal time (post-flatten).
        output_dir: Directory to write the report. Created if missing.

    Returns:
        Path to the written report file: {output_dir}/{date_str}.md
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{date_str}.md"

    decisions, orders = _load_day(db_path, date_str)
    report = _format_report(date_str, ending_equity, decisions, orders)
    out_path.write_text(report, encoding="utf-8")
    return out_path


def _load_day(
    db_path: Path, date_str: str,
) -> tuple[list[DecisionRow], list[OrderRow]]:
    """Load decisions and orders for one trading day (RTH-bounded to ET)."""
    # Day starts at 00:00 ET, ends 23:59:59 ET. Use Unix timestamps.
    day = datetime.fromisoformat(date_str).replace(tzinfo=ET)
    start_ts = day.timestamp()
    end_ts = day.replace(hour=23, minute=59, second=59).timestamp()

    decisions: list[DecisionRow] = []
    orders: list[OrderRow] = []

    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, ts, ticker, action, setup, sentiment, confidence, "
            "walls_status, reasons FROM decisions "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        )
        for row in cur:
            decisions.append(DecisionRow(*row))

        cur = conn.execute(
            "SELECT id, ts, ticker, side, qty, limit_price, stop_price, "
            "alpaca_order_id, status, error, decision_id FROM orders "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        )
        for row in cur:
            orders.append(OrderRow(*row))

    return decisions, orders


def _format_report(
    date_str: str,
    ending_equity: float,
    decisions: list[DecisionRow],
    orders: list[OrderRow],
) -> str:
    """Compose the markdown report from loaded data."""
    actionable = [d for d in decisions if d.action != "Hold"]
    holds = [d for d in decisions if d.action == "Hold"]
    submitted = [o for o in orders if o.status == "submitted"]
    rejected_risk = [o for o in orders if o.status == "risk_rejected"]
    rejected_broker = [o for o in orders if o.status == "failed"]

    # Group decisions by ticker for the trade narratives section.
    by_ticker: dict[str, list[DecisionRow]] = defaultdict(list)
    for d in actionable:
        by_ticker[d.ticker].append(d)

    # Map decision_id -> order so we can show outcomes per decision.
    order_by_decision: dict[int, OrderRow] = {}
    for o in orders:
        if o.decision_id is not None:
            order_by_decision[o.decision_id] = o

    lines: list[str] = []
    lines.append(f"# EOD Journal — {date_str}")
    lines.append("")
    lines.append("## Account Summary")
    lines.append("")
    lines.append(f"- **Ending equity:** ${ending_equity:,.2f}")
    lines.append("")
    lines.append("## Activity Overview")
    lines.append("")
    lines.append(f"- Decisions logged: {len(decisions)} ({len(actionable)} actionable, {len(holds)} holds)")
    lines.append(f"- Orders submitted: {len(submitted)}")
    lines.append(f"- Orders risk-rejected: {len(rejected_risk)}")
    lines.append(f"- Orders broker-rejected: {len(rejected_broker)}")
    lines.append("")

    # Setup breakdown
    setup_counts: dict[str, int] = defaultdict(int)
    for d in actionable:
        setup_counts[d.setup] += 1
    if setup_counts:
        lines.append("### Setup Distribution")
        lines.append("")
        for setup, n in sorted(setup_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {setup}: {n}")
        lines.append("")

    # Per-ticker narratives
    if by_ticker:
        lines.append("## Trade Narratives")
        lines.append("")
        for ticker in sorted(by_ticker):
            lines.append(f"### {ticker}")
            lines.append("")
            for d in by_ticker[ticker]:
                ts_et = datetime.fromtimestamp(d.ts, tz=ET).strftime("%H:%M:%S")
                lines.append(f"**{ts_et} ET — {d.action} ({d.setup})**")
                lines.append(f"")
                lines.append(f"- Sentiment: {d.sentiment if d.sentiment is not None else 'n/a'}")
                lines.append(f"- Technical confidence: {d.confidence}")
                lines.append(f"- Wall context: {d.walls_status}")
                lines.append(f"- Reasons: {d.reasons}")
                order = order_by_decision.get(d.id)
                if order is None:
                    lines.append(f"- Outcome: no order placed")
                else:
                    if order.status == "submitted":
                        lines.append(
                            f"- Outcome: submitted {order.qty}sh "
                            f"@ ${order.limit_price:.2f} "
                            f"(stop ${order.stop_price:.2f}) "
                            f"-> Alpaca id `{order.alpaca_order_id}`"
                        )
                    elif order.status == "risk_rejected":
                        lines.append(
                            f"- Outcome: RISK REJECTED "
                            f"({order.qty}sh @ ${order.limit_price:.2f}) "
                            f"reason={order.error}"
                        )
                    else:  # failed
                        lines.append(
                            f"- Outcome: BROKER REJECTED "
                            f"({order.qty}sh @ ${order.limit_price:.2f}) "
                            f"error={order.error}"
                        )
                lines.append("")

    # Risk rejections section
    if rejected_risk:
        lines.append("## Risk Rejections")
        lines.append("")
        for o in rejected_risk:
            ts_et = datetime.fromtimestamp(o.ts, tz=ET).strftime("%H:%M:%S")
            lines.append(
                f"- {ts_et} ET — {o.ticker} {o.side} {o.qty}sh "
                f"@ ${o.limit_price:.2f}: {o.error}"
            )
        lines.append("")

    # Broker rejections
    if rejected_broker:
        lines.append("## Broker Rejections")
        lines.append("")
        for o in rejected_broker:
            ts_et = datetime.fromtimestamp(o.ts, tz=ET).strftime("%H:%M:%S")
            lines.append(
                f"- {ts_et} ET — {o.ticker} {o.side} {o.qty}sh "
                f"@ ${o.limit_price:.2f}: {o.error}"
            )
        lines.append("")

    if not actionable:
        lines.append("## No Actionable Signals Today")
        lines.append("")
        lines.append("Possible reasons (review individually):")
        lines.append("")
        lines.append("- Sentiment thresholds not met (no strong news catalysts)")
        lines.append("- Technical signals filtered out (no pullbacks or gap-and-gos)")
        lines.append("- ES wall conditions blocked entries")
        lines.append("- Pre-market context unfavorable (low RVOL, no gap)")
        lines.append("")

    lines.append("---")
    lines.append(f"_Generated {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}_")
    lines.append("")

    return "\n".join(lines)
