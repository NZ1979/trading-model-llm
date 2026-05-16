"""Markdown comparison report for the M2 replay harness.

Reads one replay run's rows from ``replay_results.db`` (written by
``data/replay/persistence.py``) and emits a single markdown file with
seven sections per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Comparison
report format.

Sections that ship today (M2.2 #18 baseline + #21 tier-agreement +
#22 regime-stratified):

1. Run metadata -- date range, prompt version, tier config from
   replay_runs.config_json. Cache hits/misses and per-tier wall-clock
   time are deferred (not yet persisted).
2. Decision summary -- counts by action × source (live_merged vs base
   vs t3_only).
3. Portfolio performance -- starting/ending equity, total realized
   P&L, win rate, drawdown, rough Sharpe ratio for each portfolio.
4. Divergence analysis -- (ticker, tick_et) pairs where live_merged
   and base picked different actions; bucketed by disagreement type
   with realized-P&L outcomes attached when available.
5. LLM-specific quality metrics -- confidence histogram, setup label
   frequency, reasoning length distribution. Restricted to
   live_merged rows.
5b. Regime-stratified performance (M2.2 sub-task #22) -- per-(regime,
   source) trade count, win rate, total + avg realized P&L.
5d. Tier agreement & escalation analysis (M2.2 sub-task #21) --
   T1↔T3 agreement rate, 3×3 confusion matrix, disagreement
   profitability, confidence-band breakdown, tier_provenance counts.
6. Failure modes -- replay_rejections grouped by reason; LLM tier
   failure setup_label patterns (schema_invalid_t1, api_failure_t1,
   t1_unexpected).
7. Top decisions worth manual review -- highest-confidence wins,
   highest-confidence losses, biggest divergences.

Section 5c (crash-period replay) remains stubbed -- it is a
separate-run analysis, not a section of the per-run report.

Failure semantics (Rule 18):
- Missing db_path: FileNotFoundError, named loud.
- Missing run_id: ValueError naming the missing id.
- Empty run (no decisions/fills/equity): every section renders a
  "no data" placeholder; no crashes.
- Open run (completed_at IS NULL): section 1 surfaces "run still
  open" rather than failing.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_report(
    *,
    db_path: Path,
    run_id: int,
    output_path: Path | None = None,
) -> Path:
    """Generate the markdown comparison report for one replay run.

    Args:
        db_path: path to ``replay_results.db`` (typically
            ``ReplayConfig.replay_db_path``).
        run_id: which run to report on; from ``persistence.start_run``.
        output_path: optional override. Default is
            ``docs/reports/replay_<start_date>_run<run_id>.md``
            derived from the run's ``config_json`` start_date.
            Parent directory is created if missing.

    Returns:
        The Path actually written.

    Raises:
        FileNotFoundError: ``db_path`` does not exist.
        ValueError: ``run_id`` not present in ``replay_runs``.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"replay_results.db not found at {db_path}. "
            "Run a replay first via run_replay() with "
            "persistence_conn + run_id set."
        )

    conn = sqlite3.connect(db_path)
    try:
        run_row = conn.execute(
            "SELECT started_at, completed_at, config_json, repo_sha, "
            "summary_json FROM replay_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise ValueError(
                f"run_id={run_id} not found in {db_path}. "
                "Available run_ids: "
                + str([
                    r[0] for r in conn.execute(
                        "SELECT run_id FROM replay_runs ORDER BY run_id"
                    )
                ])
            )
        started_at, completed_at, config_json_str, repo_sha, summary_json = run_row
        config = json.loads(config_json_str)

        # Compose all sections.
        lines: list[str] = []
        lines.append(f"# Replay Comparison Report — run {run_id}")
        lines.append("")
        lines.extend(_section_1_metadata(
            run_id=run_id, started_at=started_at,
            completed_at=completed_at, config=config,
            repo_sha=repo_sha, summary_json=summary_json,
        ))
        lines.append("")
        lines.extend(_section_2_decision_summary(conn, run_id))
        lines.append("")
        lines.extend(_section_3_portfolio_performance(conn, run_id, config))
        lines.append("")
        lines.extend(_section_4_divergence(conn, run_id))
        lines.append("")
        lines.extend(_section_5_llm_quality(conn, run_id))
        lines.append("")
        lines.extend(_section_5b_regime_stratified(conn, run_id))
        lines.append("")
        lines.extend(_section_5c_deferred_stub())
        lines.append("")
        lines.extend(_section_5d_tier_agreement(conn, run_id))
        lines.append("")
        lines.extend(_section_6_failure_modes(conn, run_id))
        lines.append("")
        lines.extend(_section_7_top_decisions(conn, run_id))
        lines.append("")

        # Resolve output path.
        if output_path is None:
            start_date = config.get("start_date", "unknown")
            output_path = Path("docs/reports") / (
                f"replay_{start_date}_run{run_id}.md"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(
            "comparison.generate_report: wrote %s (%d lines)",
            output_path, len(lines),
        )
        return output_path
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Section 1: Run metadata
# ---------------------------------------------------------------------------


def _section_1_metadata(
    *,
    run_id: int,
    started_at: str,
    completed_at: str | None,
    config: dict,
    repo_sha: str | None,
    summary_json: str | None,
) -> list[str]:
    lines = ["## 1. Run metadata", ""]
    lines.append(f"- **Run id:** {run_id}")
    lines.append(f"- **Started at (UTC):** {started_at}")
    if completed_at is None:
        lines.append("- **Completed at:** *(run still open — completed_at IS NULL)*")
    else:
        lines.append(f"- **Completed at (UTC):** {completed_at}")
    lines.append(
        f"- **Date range:** {config.get('start_date', '?')} → "
        f"{config.get('end_date', '?')}"
    )
    tickers = config.get("tickers", "?")
    if isinstance(tickers, list):
        lines.append(f"- **Tickers:** {len(tickers)} ({', '.join(tickers[:10])}{'...' if len(tickers) > 10 else ''})")
    else:
        lines.append(f"- **Tickers:** {tickers}")
    lines.append(f"- **Prompt version:** {config.get('llm_prompt_version', '?')}")
    lines.append("")
    lines.append("### Tier configuration")
    lines.append("")
    lines.append(f"- **T1:** {config.get('t1_backend', '?')} / {config.get('t1_model_id', '?')}")
    t2_enabled = config.get("t2_enabled", False)
    lines.append(
        f"- **T2:** {'enabled' if t2_enabled else 'disabled'}"
        + (f" / {config.get('t2_model_id', '?')}, max_per_day={config.get('t2_max_per_day', '?')}"
           if t2_enabled else "")
    )
    t3_enabled = config.get("t3_enabled", False)
    lines.append(
        f"- **T3:** {'enabled' if t3_enabled else 'disabled'}"
        + (f" / {config.get('t3_model_id', '?')}, sample_rate={config.get('t3_sample_rate', '?')}"
           if t3_enabled else "")
    )
    lines.append("")
    lines.append("### Provenance")
    lines.append("")
    lines.append(f"- **repo_sha:** {repo_sha if repo_sha else '*(not recorded)*'}")
    lines.append("")

    # Cache + timing sub-blocks (M2.2 sub-task #23). Parse summary_json
    # defensively: it can be None (run never completed with summary),
    # empty (legacy summary call), or malformed (unlikely but defended
    # against per Rule 18). On any parse failure render explicit
    # placeholders rather than crashing the report.
    summary_payload: dict | None = None
    if summary_json:
        try:
            parsed = json.loads(summary_json)
            if isinstance(parsed, dict):
                summary_payload = parsed
            else:
                logger.warning(
                    "_section_1_metadata: summary_json parsed to "
                    "non-dict %s; ignoring", type(parsed).__name__,
                )
        except json.JSONDecodeError as exc:
            logger.warning(
                "_section_1_metadata: summary_json malformed (%s); "
                "rendering placeholders", exc,
            )

    lines.extend(_render_cache_stats(summary_payload))
    lines.append("")
    lines.extend(_render_phase_timing(summary_payload))
    return lines


def _render_cache_stats(payload: dict | None) -> list[str]:
    """Render the cache hits/misses table from summary_json.

    Emits a `### Cache stats` header followed by a markdown table with
    one row per wrapped tier. If neither tier has cache fields in the
    payload (e.g. legacy runs or runs without ``CachedLLMClient``
    wrapping), emits a placeholder line instead.
    """
    lines = ["### Cache stats", ""]
    if payload is None:
        lines.append("*Cache stats not recorded for this run.*")
        return lines

    rows: list[tuple[str, int, int]] = []
    for tier in ("t1", "t2"):
        hits_key = f"cache_{tier}_hits"
        misses_key = f"cache_{tier}_misses"
        if hits_key in payload or misses_key in payload:
            hits = int(payload.get(hits_key, 0))
            misses = int(payload.get(misses_key, 0))
            rows.append((tier.upper(), hits, misses))

    if not rows:
        lines.append("*No cached tiers in this run.*")
        return lines

    lines.append("| Tier | Hits | Misses | Hit rate |")
    lines.append("|---|---|---|---|")
    for tier, hits, misses in rows:
        total = hits + misses
        if total == 0:
            rate_str = "*(no calls)*"
        else:
            rate_str = f"{(hits / total) * 100:.1f}%"
        lines.append(f"| {tier} | {hits} | {misses} | {rate_str} |")
    return lines


def _render_phase_timing(payload: dict | None) -> list[str]:
    """Render the per-phase wall-clock timing breakdown.

    Emits a `### Phase timing` header followed by one bullet per phase
    that has data, plus a `Total wall-clock` bullet when the run-level
    total is present. Phases missing from the payload are silently
    skipped (e.g. T3 disabled). Empty payload renders a placeholder.
    """
    lines = ["### Phase timing", ""]
    if payload is None:
        lines.append("*Phase timing not recorded for this run.*")
        return lines

    # Run-level wall-clock first when available.
    total_ms = payload.get("total_wall_clock_ms")
    if total_ms is not None:
        lines.append(f"- **Total wall-clock:** {_format_ms(total_ms)}")

    # Per-phase rows in a fixed display order.
    phase_order: tuple[tuple[str, str], ...] = (
        ("data_prep", "Data prep"),
        ("tick_loop", "Tick loop"),
        ("fill_sim_llm", "Fill sim (LLM)"),
        ("t3_labeling", "T3 labeling"),
        ("base_pass", "Base pass"),
    )
    rendered_any = False
    for key, label in phase_order:
        mean_key = f"phase_{key}_mean_ms"
        n_key = f"phase_{key}_n_days"
        if mean_key in payload:
            mean = float(payload[mean_key])
            n = int(payload.get(n_key, 0))
            lines.append(
                f"- **{label}:** {_format_ms(mean)} mean/day "
                f"({n} day{'s' if n != 1 else ''})"
            )
            rendered_any = True

    if total_ms is None and not rendered_any:
        lines.append(
            "*No phase timing keys present in summary_json.*"
        )
    return lines


def _format_ms(ms: float) -> str:
    """Render a millisecond duration as a human-readable string.

    Sub-second values render as `123 ms`; sub-minute as `4.2 s`;
    longer durations as `3m 14s`.
    """
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}m {remainder:.0f}s"


# ---------------------------------------------------------------------------
# Section 2: Decision summary
# ---------------------------------------------------------------------------


def _section_2_decision_summary(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 2. Decision summary", ""]
    rows = conn.execute(
        "SELECT decision_source, action, COUNT(*) "
        "FROM replay_decisions WHERE run_id = ? "
        "GROUP BY decision_source, action ORDER BY decision_source, action",
        (run_id,),
    ).fetchall()
    if not rows:
        lines.append("*No decisions recorded for this run.*")
        return lines

    # Build a pivot: action -> {source: count}
    pivot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sources_seen: set[str] = set()
    for src, action, n in rows:
        pivot[action][src] = n
        sources_seen.add(src)

    sources = sorted(sources_seen)
    actions = ["Buy", "Sell", "Hold"]
    # Markdown table
    header = "| Action | " + " | ".join(sources) + " |"
    sep = "|" + "|".join(["---"] * (len(sources) + 1)) + "|"
    lines.append(header)
    lines.append(sep)
    for action in actions:
        row = f"| {action} | " + " | ".join(
            str(pivot.get(action, {}).get(src, 0)) for src in sources
        ) + " |"
        lines.append(row)
    lines.append("")
    if "live_merged" not in sources_seen:
        lines.append("*No live_merged decisions — LLM path was not run.*")
    if "base" not in sources_seen:
        lines.append("*No base decisions — run did not include base_portfolio.*")
    lines.append("")
    lines.append(
        "*Per-tier rows (T1, T2, T3) are not yet persisted — deferred "
        "to the Tier-3 persistence sub-task.*"
    )
    return lines


# ---------------------------------------------------------------------------
# Section 3: Portfolio performance
# ---------------------------------------------------------------------------


def _section_3_portfolio_performance(
    conn: sqlite3.Connection, run_id: int, config: dict,
) -> list[str]:
    lines = ["## 3. Portfolio performance", ""]
    starting_cash = float(config.get("starting_cash", 0.0))

    # Determine which portfolios have data.
    portfolio_names = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT portfolio_name FROM replay_equity_curve "
            "WHERE run_id = ? ORDER BY portfolio_name", (run_id,)
        )
    ]
    if not portfolio_names:
        lines.append("*No equity curve points recorded — no portfolio was active.*")
        return lines

    for pname in portfolio_names:
        lines.extend(_portfolio_section(conn, run_id, pname, starting_cash))
        lines.append("")
    return lines


def _portfolio_section(
    conn: sqlite3.Connection,
    run_id: int,
    portfolio_name: str,
    starting_cash: float,
) -> list[str]:
    lines = [f"### {portfolio_name} portfolio", ""]

    # Ending equity = last equity_curve point by id (chronological).
    end_row = conn.execute(
        "SELECT equity, cash, n_open_positions FROM replay_equity_curve "
        "WHERE run_id = ? AND portfolio_name = ? "
        "ORDER BY id DESC LIMIT 1",
        (run_id, portfolio_name),
    ).fetchone()
    ending_equity = end_row[0] if end_row else starting_cash
    total_return = ending_equity - starting_cash
    total_return_pct = (
        (total_return / starting_cash * 100.0) if starting_cash else 0.0
    )
    lines.append(f"- **Starting cash:** ${starting_cash:,.2f}")
    lines.append(f"- **Ending equity:** ${ending_equity:,.2f}")
    lines.append(
        f"- **Total return:** ${total_return:,.2f} "
        f"({total_return_pct:+.2f}%)"
    )

    # Trade stats from replay_fills (decision_source filter via JOIN).
    decision_source = "live_merged" if portfolio_name == "llm" else portfolio_name
    fills = conn.execute(
        "SELECT f.realized_pl FROM replay_fills f "
        "JOIN replay_decisions d ON f.decision_id = d.id "
        "WHERE f.run_id = ? AND d.decision_source = ? "
        "AND f.realized_pl IS NOT NULL",
        (run_id, decision_source),
    ).fetchall()
    pls = [row[0] for row in fills]
    total_trades = len(pls)
    if total_trades == 0:
        lines.append("- **Trades:** 0 *(no closed positions)*")
    else:
        winners = [p for p in pls if p > 0]
        losers = [p for p in pls if p < 0]
        flats = [p for p in pls if p == 0]
        win_rate = (len(winners) / total_trades * 100.0)
        avg_win = (sum(winners) / len(winners)) if winners else 0.0
        avg_loss = (sum(losers) / len(losers)) if losers else 0.0
        max_win = max(pls) if pls else 0.0
        max_loss = min(pls) if pls else 0.0
        total_realized = sum(pls)
        lines.append(f"- **Total realized P&L:** ${total_realized:,.2f}")
        lines.append(
            f"- **Trades:** {total_trades} "
            f"({len(winners)} winners / {len(losers)} losers / {len(flats)} flat)"
        )
        lines.append(f"- **Win rate:** {win_rate:.1f}%")
        lines.append(f"- **Average win:** ${avg_win:,.2f}")
        lines.append(f"- **Average loss:** ${avg_loss:,.2f}")
        lines.append(f"- **Largest single win:** ${max_win:,.2f}")
        lines.append(f"- **Largest single loss:** ${max_loss:,.2f}")

    # Max drawdown over the equity curve.
    curve = conn.execute(
        "SELECT bar_et, equity FROM replay_equity_curve "
        "WHERE run_id = ? AND portfolio_name = ? ORDER BY id",
        (run_id, portfolio_name),
    ).fetchall()
    max_dd, max_dd_at = _compute_max_drawdown(curve)
    if max_dd > 0:
        lines.append(
            f"- **Max drawdown:** ${max_dd:,.2f} "
            f"(at {max_dd_at})"
        )
    else:
        lines.append("- **Max drawdown:** $0.00 *(curve never dropped from peak)*")

    # Rough Sharpe ratio from daily returns.
    sharpe = _rough_sharpe(curve)
    if sharpe is None:
        lines.append("- **Sharpe (annualized, rough):** *(need ≥ 2 trading days)*")
    else:
        lines.append(
            f"- **Sharpe (annualized, rough):** {sharpe:.2f} "
            "*(short replay window; treat as directional only)*"
        )

    return lines


def _compute_max_drawdown(
    curve: list[tuple[str, float]],
) -> tuple[float, str | None]:
    """Walk the equity curve maintaining a running peak; return (max_dd, bar_et)."""
    peak = None
    max_dd = 0.0
    max_dd_at: str | None = None
    for bar_et, eq in curve:
        if peak is None or eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_at = bar_et
    return max_dd, max_dd_at


def _rough_sharpe(curve: list[tuple[str, float]]) -> float | None:
    """Annualized Sharpe from end-of-day equity points.

    Compresses the (potentially 78+ per day) curve to one closing point
    per trading_date (the last bar_et with that date prefix), then
    computes daily returns and the standard
    ``mean(returns) / stdev(returns) * sqrt(252)``. Returns None when
    fewer than 2 distinct trading days are present (stdev undefined).
    """
    by_date: dict[str, float] = {}
    for bar_et, eq in curve:
        # bar_et is ISO 8601; the date prefix is the first 10 chars.
        date_str = bar_et[:10]
        by_date[date_str] = eq  # later writes overwrite -> last-of-day wins
    if len(by_date) < 2:
        return None
    dates = sorted(by_date.keys())
    equities = [by_date[d] for d in dates]
    returns: list[float] = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        cur = equities[i]
        if prev == 0:
            continue
        returns.append((cur / prev) - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return None
    return (mean / stdev) * math.sqrt(252)


# ---------------------------------------------------------------------------
# Section 4: Divergence analysis
# ---------------------------------------------------------------------------


def _section_4_divergence(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 4. Divergence analysis", ""]
    # Quick check: any base decisions?
    n_base = conn.execute(
        "SELECT COUNT(*) FROM replay_decisions "
        "WHERE run_id = ? AND decision_source = 'base'",
        (run_id,),
    ).fetchone()[0]
    n_llm = conn.execute(
        "SELECT COUNT(*) FROM replay_decisions "
        "WHERE run_id = ? AND decision_source = 'live_merged'",
        (run_id,),
    ).fetchone()[0]
    if n_base == 0 or n_llm == 0:
        lines.append(
            "*No comparable data — this run did not include both "
            "an LLM (live_merged) and a base portfolio. Rerun with "
            "both `portfolio` and `base_portfolio` set on `run_replay` "
            "to populate this section.*"
        )
        return lines

    # JOIN the two decision streams on (ticker, tick_et). Disagreements only.
    rows = conn.execute(
        "SELECT llm.ticker, llm.tick_et, "
        "       llm.action AS llm_action, base.action AS base_action, "
        "       llm.id AS llm_decision_id, base.id AS base_decision_id "
        "FROM replay_decisions llm "
        "JOIN replay_decisions base "
        "  ON llm.ticker = base.ticker AND llm.tick_et = base.tick_et "
        "WHERE llm.run_id = ? AND llm.decision_source = 'live_merged' "
        "  AND base.decision_source = 'base' "
        "  AND llm.action != base.action",
        (run_id,),
    ).fetchall()

    if not rows:
        lines.append(
            "*All LLM and base actions agreed — no divergences recorded.*"
        )
        return lines

    # Bucket disagreements
    buckets: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        buckets[(r[2], r[3])].append(r)

    lines.append(f"**Total disagreement count:** {len(rows)}")
    lines.append("")
    bucket_names = {
        ("Buy", "Hold"): "LLM extra Buys (LLM=Buy, base=Hold)",
        ("Sell", "Hold"): "LLM extra Sells (LLM=Sell, base=Hold)",
        ("Hold", "Buy"): "LLM missed Buys (LLM=Hold, base=Buy)",
        ("Hold", "Sell"): "LLM missed Sells (LLM=Hold, base=Sell)",
        ("Buy", "Sell"): "Opposite (LLM=Buy, base=Sell)",
        ("Sell", "Buy"): "Opposite (LLM=Sell, base=Buy)",
    }
    for key in [("Buy", "Hold"), ("Sell", "Hold"),
                ("Hold", "Buy"), ("Hold", "Sell"),
                ("Buy", "Sell"), ("Sell", "Buy")]:
        rows_b = buckets.get(key, [])
        if not rows_b:
            continue
        name = bucket_names[key]
        lines.append(f"### {name} ({len(rows_b)})")
        lines.append("")
        # Attach realized_pl outcome where available.
        lines.append("| Ticker | Tick | LLM P&L | Base P&L |")
        lines.append("|---|---|---|---|")
        for r in rows_b[:20]:  # cap at 20 per bucket
            ticker, tick_et, _, _, llm_decision_id, base_decision_id = r
            llm_pl = _decision_realized_pl(conn, llm_decision_id)
            base_pl = _decision_realized_pl(conn, base_decision_id)
            lines.append(
                f"| {ticker} | {tick_et} | "
                f"{_fmt_pl(llm_pl)} | {_fmt_pl(base_pl)} |"
            )
        if len(rows_b) > 20:
            lines.append(f"")
            lines.append(f"*({len(rows_b) - 20} more rows truncated.)*")
        lines.append("")
    return lines


def _decision_realized_pl(
    conn: sqlite3.Connection, decision_id: int,
) -> float | None:
    """Return the realized_pl on the fill linked to this decision (if any)."""
    row = conn.execute(
        "SELECT realized_pl FROM replay_fills "
        "WHERE decision_id = ? AND realized_pl IS NOT NULL",
        (decision_id,),
    ).fetchone()
    return row[0] if row else None


def _fmt_pl(pl: float | None) -> str:
    if pl is None:
        return "*(no fill)*"
    return f"${pl:,.2f}"


# ---------------------------------------------------------------------------
# Section 5: LLM-specific quality metrics
# ---------------------------------------------------------------------------


def _section_5_llm_quality(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 5. LLM-specific quality metrics", ""]
    rows = conn.execute(
        "SELECT confidence, setup_label, reasoning, action "
        "FROM replay_decisions "
        "WHERE run_id = ? AND decision_source = 'live_merged'",
        (run_id,),
    ).fetchall()
    if not rows:
        lines.append("*No live_merged decisions — nothing to summarize.*")
        return lines

    # Confidence histogram (10 buckets of width 10).
    lines.append("### Confidence distribution (live_merged)")
    lines.append("")
    buckets = [0] * 10  # 0-9, 10-19, ..., 90-99/100
    for conf, _, _, _ in rows:
        if conf is None:
            continue
        idx = min(int(conf) // 10, 9)
        buckets[idx] += 1
    lines.append("| Range | Count |")
    lines.append("|---|---|")
    for i, n in enumerate(buckets):
        low = i * 10
        high = (i + 1) * 10 - 1 if i < 9 else 100
        lines.append(f"| {low}-{high} | {n} |")
    lines.append("")

    # Setup-label frequency.
    lines.append("### Setup label frequency (live_merged)")
    lines.append("")
    setup_counts: dict[str, int] = defaultdict(int)
    for _, setup, _, _ in rows:
        setup_counts[setup or "*(none)*"] += 1
    lines.append("| Setup | Count |")
    lines.append("|---|---|")
    for setup, n in sorted(setup_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| {setup} | {n} |")
    lines.append("")

    # Reasoning length distribution.
    lengths = [len(r[2] or "") for r in rows]
    lengths_sorted = sorted(lengths)
    if lengths_sorted:
        lo = lengths_sorted[0]
        hi = lengths_sorted[-1]
        med = lengths_sorted[len(lengths_sorted) // 2]
        avg = sum(lengths_sorted) / len(lengths_sorted)
        lines.append("### Reasoning length (chars)")
        lines.append("")
        lines.append(f"- **Min:** {lo}")
        lines.append(f"- **Median:** {med}")
        lines.append(f"- **Mean:** {avg:.1f}")
        lines.append(f"- **Max:** {hi}")
    return lines


def _section_5c_deferred_stub() -> list[str]:
    """Stub for 5c (crash-period replay).

    5b landed in M2.2 sub-task #22 (regime-stratified persistence +
    section). 5d landed in M2.2 sub-task #21 (tier agreement). 5c
    remains stubbed because it is a separate-run analysis, not a
    section of the per-run report.
    """
    return [
        "## 5c. Crash-period replay",
        "",
        "*Deferred — this is a separate-run analysis, not a section "
        "of the regular per-run report. Run the harness on an "
        "identified high-volatility window with its own run_id; "
        "compare against the baseline run.*",
    ]


# ---------------------------------------------------------------------------
# Section 5b: Regime-stratified performance (M2.2 sub-task #22)
# ---------------------------------------------------------------------------
#
# Inputs: ``replay_decisions.regime`` (added in #22) joined with
# ``replay_fills.realized_pl``. Regime is per-day (one of
# ``bull`` / ``bear`` / ``neutral`` / ``unknown`` per
# ``DayState.market_regime_label``); every decision on the same trading
# day shares the same regime label, so the grouping is by (regime,
# decision_source).
#
# Output: one row per (regime, decision_source) pair with:
#   - Trades: count of fills attached to live_merged/base decisions
#   - Win rate: fills with realized_pl > 0 / total fills
#   - Total realized P&L: SUM(realized_pl)
#   - Avg P&L: mean realized_pl per fill
#
# NULL regime (legacy rows from pre-#22 DBs) renders as *(none)*.
#
# Notes:
#   - T3 has no fills (it's a labeling pass) so t3_only is intentionally
#     omitted from this section. Per-tier behavior lives in § 5d.
#   - Per-regime max drawdown is out of scope: drawdown is a continuous
#     portfolio-curve metric, and a single day has one regime label, so
#     per-regime DD requires a regime column on replay_equity_curve --
#     a separate sub-task.


def _section_5b_regime_stratified(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 5b. Regime-stratified performance", ""]

    rows = conn.execute(
        """
        SELECT
            d.regime,
            d.decision_source,
            COUNT(f.id) AS n_fills,
            SUM(CASE WHEN f.realized_pl > 0 THEN 1 ELSE 0 END)
                AS n_wins,
            SUM(f.realized_pl) AS total_pl,
            AVG(f.realized_pl) AS avg_pl
        FROM replay_fills f
        JOIN replay_decisions d ON f.decision_id = d.id
        WHERE d.run_id = ?
            AND d.decision_source IN ('live_merged', 'base')
            AND f.realized_pl IS NOT NULL
        GROUP BY d.regime, d.decision_source
        ORDER BY
            CASE WHEN d.regime IS NULL THEN 1 ELSE 0 END,
            d.regime,
            d.decision_source
        """,
        (run_id,),
    ).fetchall()

    if not rows:
        lines.append(
            "*No closed positions to stratify. "
            "Either the run had no fills, or all fills had NULL "
            "realized_pl (still-open positions).*"
        )
        return lines

    lines.append(
        "| Regime | Source | Trades | Win rate | "
        "Total realized P&L | Avg P&L |"
    )
    lines.append("|---|---|---|---|---|---|")
    for regime, source, n_fills, n_wins, total_pl, avg_pl in rows:
        regime_label = regime if regime is not None else "*(none)*"
        win_rate = (n_wins / n_fills) if n_fills else 0.0
        lines.append(
            f"| {regime_label} | {source} | {n_fills} | "
            f"{win_rate * 100:.1f}% | ${total_pl:,.2f} | "
            f"${avg_pl:,.2f} |"
        )
    lines.append("")
    lines.append(
        "*Note: per-regime max drawdown is omitted — drawdown is a "
        "continuous portfolio-curve metric and per-regime DD requires "
        "a regime column on `replay_equity_curve` (separate sub-task). "
        "T3 has no fills (labeling pass), so it is not included here; "
        "tier-level analysis lives in § 5d.*"
    )
    return lines


# ---------------------------------------------------------------------------
# Section 5d: Tier agreement & escalation analysis (M2.2 sub-task #21)
# ---------------------------------------------------------------------------
#
# Quantifies the tiered architecture per
# ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § 5d and
# ``docs/LLM_SIGNAL_INTERFACE.md`` § Tiered evaluation.
#
# Inputs: ``replay_decisions`` rows with ``decision_source IN
# ('live_merged', 't3_only')`` plus optional ``replay_fills.realized_pl``
# join on the T1 (live_merged) ``decision_id``.
#
# Sub-blocks rendered:
#   5d.1 Evaluated-pair denominator + T1 / T3 failure tally.
#   5d.2 Overall T1↔T3 agreement rate + 3×3 confusion matrix.
#   5d.3 Disagreement profitability on T1's realized P&L side.
#        (T3 has no portfolio simulation -- it's a labeling pass --
#         so a counterfactual T3 P&L is intentionally not computed.)
#   5d.4 Agreement breakdown by T1 confidence band (high ≥ 75 vs
#        low < 75). 75 matches the escalation rule's ceiling.
#   5d.5 Tier 2 escalation tier_provenance counts on the live_merged
#        side. The T2-reverse-vs-T1-original P&L analysis from the
#        design doc is intentionally out of scope -- it needs the
#        pre-merge T1 action persisted, which today is not stored.

_T1_FAIL_SETUPS = ("schema_invalid_t1", "api_failure_t1", "t1_unexpected")
_T3_FAIL_SETUPS = ("schema_invalid_t3", "api_failure_t3", "t3_unexpected")
_HIGH_CONF_THRESHOLD = 75  # matches escalation_rule.confidence_ceiling
_ACTIONS: tuple[str, ...] = ("Buy", "Sell", "Hold")


def _section_5d_tier_agreement(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 5d. Tier agreement & escalation analysis", ""]

    # Count tier-failure rows on each side (excluded from the agreement
    # math but surfaced for visibility).
    n_t1_fail = conn.execute(
        f"SELECT COUNT(*) FROM replay_decisions "
        f"WHERE run_id = ? AND decision_source = 'live_merged' "
        f"AND setup_label IN ("
        f"{','.join('?' * len(_T1_FAIL_SETUPS))})",
        (run_id, *_T1_FAIL_SETUPS),
    ).fetchone()[0]
    n_t3_fail = conn.execute(
        f"SELECT COUNT(*) FROM replay_decisions "
        f"WHERE run_id = ? AND decision_source = 't3_only' "
        f"AND (setup_label IN ("
        f"{','.join('?' * len(_T3_FAIL_SETUPS))}) "
        f"OR tier_provenance = 't3_failed')",
        (run_id, *_T3_FAIL_SETUPS),
    ).fetchone()[0]

    # Pair T1 (live_merged) with T3 (t3_only) on (ticker, tick_et).
    # Failure rows excluded on both sides.
    placeholders_t1 = ",".join("?" * len(_T1_FAIL_SETUPS))
    placeholders_t3 = ",".join("?" * len(_T3_FAIL_SETUPS))
    pairs = conn.execute(
        f"""
        SELECT t1.action, t3.action, t1.confidence, t1.id, t3.id
        FROM replay_decisions t1
        INNER JOIN replay_decisions t3
            ON t1.run_id = t3.run_id
            AND t1.ticker = t3.ticker
            AND t1.tick_et = t3.tick_et
        WHERE t1.run_id = ?
            AND t1.decision_source = 'live_merged'
            AND t3.decision_source = 't3_only'
            AND (t1.setup_label IS NULL
                 OR t1.setup_label NOT IN ({placeholders_t1}))
            AND (t3.setup_label IS NULL
                 OR t3.setup_label NOT IN ({placeholders_t3}))
            AND (t3.tier_provenance IS NULL
                 OR t3.tier_provenance != 't3_failed')
        """,
        (run_id, *_T1_FAIL_SETUPS, *_T3_FAIL_SETUPS),
    ).fetchall()

    if not pairs:
        lines.append(
            "*No paired T1 / T3 decisions for this run — T3 labeling "
            "not enabled, no overlapping (ticker, tick) pairs, or all "
            "pairs were tier failures.*"
        )
        if n_t1_fail or n_t3_fail:
            lines.append("")
            lines.append("### Tier failure counts")
            lines.append("")
            lines.append(f"- **T1 failures:** {n_t1_fail}")
            lines.append(f"- **T3 failures:** {n_t3_fail}")
        lines.append("")
        lines.extend(_section_5d5_escalation_counts(conn, run_id))
        return lines

    n_pairs = len(pairs)

    # ---- 5d.1: evaluated-pair denominator + tier failure tally ----
    lines.append("### Evaluated pairs")
    lines.append("")
    lines.append(f"- **Paired T1↔T3 decisions:** {n_pairs}")
    lines.append(f"- **T1 failures excluded:** {n_t1_fail}")
    lines.append(f"- **T3 failures excluded:** {n_t3_fail}")
    lines.append("")

    # ---- 5d.2: overall agreement + 3×3 confusion matrix ----
    n_agree = sum(1 for t1a, t3a, *_ in pairs if t1a == t3a)
    rate = n_agree / n_pairs

    matrix: dict[tuple[str, str], int] = {
        (a, b): 0 for a in _ACTIONS for b in _ACTIONS
    }
    for t1a, t3a, *_ in pairs:
        if t1a in _ACTIONS and t3a in _ACTIONS:
            matrix[(t1a, t3a)] += 1

    lines.append("### Overall T1↔T3 agreement")
    lines.append("")
    lines.append(
        f"- **Agreement rate:** {rate * 100:.1f}% ({n_agree}/{n_pairs})"
    )
    lines.append("")
    lines.append(
        "### Confusion matrix (rows = T1 action, cols = T3 action)"
    )
    lines.append("")
    lines.append("| T1 \\ T3 | Buy | Sell | Hold |")
    lines.append("|---|---|---|---|")
    for t1a in _ACTIONS:
        cells = " | ".join(
            str(matrix[(t1a, t3a)]) for t3a in _ACTIONS
        )
        lines.append(f"| **{t1a}** | {cells} |")
    lines.append("")

    # ---- 5d.3: disagreement profitability (T1 side) ----
    disagreements = [
        (t1a, t3a, conf, t1id, t3id)
        for (t1a, t3a, conf, t1id, t3id) in pairs
        if t1a != t3a
    ]
    lines.append("### Disagreement profitability (T1 side)")
    lines.append("")
    if not disagreements:
        lines.append("*No T1↔T3 disagreements in this run.*")
    else:
        pls: list[float] = []
        for _, _, _, t1id, _ in disagreements:
            pl = _decision_realized_pl(conn, t1id)
            if pl is not None:
                pls.append(pl)
        lines.append(f"- **Disagreements:** {len(disagreements)}")
        lines.append(f"- **Disagreements with fills:** {len(pls)}")
        if pls:
            mean_pl = sum(pls) / len(pls)
            lines.append(
                "- **Mean T1-side realized P&L on disagreements:** "
                f"${mean_pl:,.2f}"
            )
        else:
            lines.append(
                "- *No fills attached to disagreement decisions.*"
            )
        lines.append("")
        lines.append(
            "*Note: T3 has no portfolio simulation (it is a labeling "
            "pass), so a counterfactual \"what would T3 have realized\" "
            "P&L is not computed here.*"
        )
    lines.append("")

    # ---- 5d.4: agreement by T1 confidence band ----
    high = [
        (t1a, t3a)
        for (t1a, t3a, conf, _, _) in pairs
        if conf is not None and conf >= _HIGH_CONF_THRESHOLD
    ]
    low = [
        (t1a, t3a)
        for (t1a, t3a, conf, _, _) in pairs
        if conf is not None and conf < _HIGH_CONF_THRESHOLD
    ]
    lines.append(
        f"### Agreement by T1 confidence band "
        f"(threshold = {_HIGH_CONF_THRESHOLD})"
    )
    lines.append("")
    lines.append("| Band | N | Agreement rate |")
    lines.append("|---|---|---|")
    for label, group in (
        (f"High (≥{_HIGH_CONF_THRESHOLD})", high),
        (f"Low (<{_HIGH_CONF_THRESHOLD})", low),
    ):
        if group:
            ar = sum(1 for t1a, t3a in group if t1a == t3a) / len(group)
            lines.append(f"| {label} | {len(group)} | {ar * 100:.1f}% |")
        else:
            lines.append(f"| {label} | 0 | *(no data)* |")
    lines.append("")

    # ---- 5d.5: T2 escalation tier_provenance counts ----
    lines.extend(_section_5d5_escalation_counts(conn, run_id))
    return lines


def _section_5d5_escalation_counts(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    """Counts of live_merged rows grouped by ``tier_provenance``.

    The full design-doc § 5d "Tier 2 escalation behavior" block
    (T2-confirm vs T2-reverse vs T1-original P&L) requires persisting
    the pre-merge T1 action, which today is not stored. This sub-block
    renders the counts that ARE available from the existing schema and
    flags the missing analysis explicitly.
    """
    lines = [
        "### Tier 2 escalation behavior (tier_provenance counts)",
        "",
    ]
    rows = conn.execute(
        "SELECT tier_provenance, COUNT(*) FROM replay_decisions "
        "WHERE run_id = ? AND decision_source = 'live_merged' "
        "GROUP BY tier_provenance ORDER BY COUNT(*) DESC",
        (run_id,),
    ).fetchall()
    if not rows:
        lines.append("*No live_merged decisions in this run.*")
        return lines
    lines.append("| tier_provenance | Count |")
    lines.append("|---|---|")
    for prov, n in rows:
        label = prov if prov is not None else "*(none)*"
        lines.append(f"| `{label}` | {n} |")
    lines.append("")
    lines.append(
        "*Note: a future sub-task will add the T2-confirm-vs-reverse "
        "P&L analysis from the design doc — it requires persisting "
        "the pre-merge T1 action, which today is not stored.*"
    )
    return lines


# ---------------------------------------------------------------------------
# Section 6: Failure modes
# ---------------------------------------------------------------------------


def _section_6_failure_modes(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 6. Failure modes", ""]

    # Rejection counts by reason.
    rj_rows = conn.execute(
        "SELECT reason, COUNT(*) FROM replay_rejections "
        "WHERE run_id = ? GROUP BY reason ORDER BY COUNT(*) DESC",
        (run_id,),
    ).fetchall()
    lines.append("### Risk-module / fill-simulator rejections")
    lines.append("")
    if not rj_rows:
        lines.append("*No rejections recorded.*")
    else:
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for reason, n in rj_rows:
            lines.append(f"| `{reason}` | {n} |")
    lines.append("")

    # LLM tier-failure markers via setup_label patterns the signal_engine
    # uses (per its docstring: schema_invalid_t1, api_failure_t1,
    # t1_unexpected).
    lines.append("### LLM tier failures (live_merged setup_label patterns)")
    lines.append("")
    patterns = ["schema_invalid_t1", "api_failure_t1", "t1_unexpected"]
    rows = []
    for pat in patterns:
        n = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions "
            "WHERE run_id = ? AND decision_source = 'live_merged' "
            "AND setup_label = ?",
            (run_id, pat),
        ).fetchone()[0]
        rows.append((pat, n))
    if all(n == 0 for _, n in rows):
        lines.append("*No tier-failure setup_labels detected.*")
    else:
        lines.append("| Pattern | Count |")
        lines.append("|---|---|")
        for pat, n in rows:
            lines.append(f"| `{pat}` | {n} |")
    lines.append("")
    lines.append(
        "*Tier-2 budget exhaustion days are not yet surfaced — that "
        "lives in run-level `summary_json` (caller-supplied); a "
        "future sub-task will standardize the shape.*"
    )
    return lines


# ---------------------------------------------------------------------------
# Section 7: Top decisions
# ---------------------------------------------------------------------------


def _section_7_top_decisions(
    conn: sqlite3.Connection, run_id: int,
) -> list[str]:
    lines = ["## 7. Top decisions worth manual review", ""]

    # Highest-confidence wins (live_merged side).
    lines.append("### Top 5 wins (live_merged, highest realized P&L)")
    lines.append("")
    top_wins = conn.execute(
        "SELECT d.ticker, d.tick_et, d.action, d.setup_label, "
        "       d.confidence, f.realized_pl "
        "FROM replay_fills f JOIN replay_decisions d "
        "  ON f.decision_id = d.id "
        "WHERE f.run_id = ? AND d.decision_source = 'live_merged' "
        "  AND f.realized_pl IS NOT NULL "
        "ORDER BY f.realized_pl DESC LIMIT 5",
        (run_id,),
    ).fetchall()
    lines.extend(_render_decision_table(top_wins))
    lines.append("")

    lines.append("### Top 5 losses (live_merged, lowest realized P&L)")
    lines.append("")
    top_losses = conn.execute(
        "SELECT d.ticker, d.tick_et, d.action, d.setup_label, "
        "       d.confidence, f.realized_pl "
        "FROM replay_fills f JOIN replay_decisions d "
        "  ON f.decision_id = d.id "
        "WHERE f.run_id = ? AND d.decision_source = 'live_merged' "
        "  AND f.realized_pl IS NOT NULL "
        "ORDER BY f.realized_pl ASC LIMIT 5",
        (run_id,),
    ).fetchall()
    lines.extend(_render_decision_table(top_losses))
    lines.append("")

    # Top 5 most divergent (|llm_pl - base_pl| descending) -- requires both
    # portfolios.
    lines.append("### Top 5 divergent decisions (|LLM P&L − base P&L| descending)")
    lines.append("")
    divergent = conn.execute(
        "SELECT llm.ticker, llm.tick_et, "
        "       llm.action AS llm_action, base.action AS base_action, "
        "       lf.realized_pl AS llm_pl, bf.realized_pl AS base_pl "
        "FROM replay_decisions llm "
        "JOIN replay_decisions base "
        "  ON llm.ticker = base.ticker AND llm.tick_et = base.tick_et "
        "LEFT JOIN replay_fills lf ON lf.decision_id = llm.id "
        "LEFT JOIN replay_fills bf ON bf.decision_id = base.id "
        "WHERE llm.run_id = ? AND llm.decision_source = 'live_merged' "
        "  AND base.decision_source = 'base' "
        "  AND lf.realized_pl IS NOT NULL "
        "  AND bf.realized_pl IS NOT NULL "
        "ORDER BY ABS(lf.realized_pl - bf.realized_pl) DESC LIMIT 5",
        (run_id,),
    ).fetchall()
    if not divergent:
        lines.append("*No diverged decisions with paired fills on both sides.*")
    else:
        lines.append("| Ticker | Tick | LLM action | Base action | LLM P&L | Base P&L |")
        lines.append("|---|---|---|---|---|---|")
        for row in divergent:
            t, ts, la, ba, lp, bp = row
            lines.append(
                f"| {t} | {ts} | {la} | {ba} | "
                f"${lp:,.2f} | ${bp:,.2f} |"
            )
    return lines


def _render_decision_table(rows: list[tuple]) -> list[str]:
    if not rows:
        return ["*No decisions in this category.*"]
    lines = ["| Ticker | Tick | Action | Setup | Confidence | Realized P&L |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        t, ts, action, setup, conf, pl = r
        lines.append(
            f"| {t} | {ts} | {action} | {setup or '*(none)*'} | "
            f"{conf if conf is not None else '?'} | ${pl:,.2f} |"
        )
    return lines


__all__ = ["generate_report"]
