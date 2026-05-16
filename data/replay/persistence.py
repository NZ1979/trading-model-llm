"""SQLite persistence layer for the M2 replay harness.

Writes one replay run's decisions, fills (with their exits inlined),
rejections, and equity curve to ``replay_results.db``. The downstream
markdown comparison report (sub-task #18+ in the M2.2 sequence) reads
from these tables; this module owns the write side only.

Schema is an extension of the design doc's § Storage spec. The
original design enumerated only ``replay_runs`` / ``replay_decisions``
/ ``replay_fills``; M2.2 sub-tasks #14 and #15 introduced
``RejectedEntry``, ``StopOut``, ``EodExit``, and the per-bar equity
curve, which this module persists too. Stops, EOD flattens, and flips
do NOT get their own tables: they all close an existing fill, and
their exit info lives on ``replay_fills``' nullable exit columns
(matching the design doc's one-row-per-fill mental model). Rejections
DO get their own table since a rejection has no fill to attach to.

Public surface (the only functions ``run_replay`` and tests should
import):

- ``init_replay_db(path)``: open + apply schema (idempotent).
- ``start_run(conn, *, config, repo_sha=None)``: insert a ``replay_runs``
  row, return the new ``run_id``.
- ``write_day_results(conn, *, run_id, day_result, llm_portfolio)``:
  write one day's rows in a single transaction. No-op for skipped
  days.
- ``complete_run(conn, *, run_id, summary_json=None)``: stamp the run
  with ``completed_at`` and an optional summary JSON.

Caller owns connection lifecycle. The pattern from ``run_replay``::

    conn = init_replay_db(config.replay_db_path)
    run_id = start_run(conn, config=config)
    try:
        ... # run_replay calls write_day_results per non-skipped day
        complete_run(conn, run_id=run_id, summary_json=...)
    finally:
        conn.close()

Failure semantics (Rule 18):

- Bad path / permission denied at open: propagates from ``sqlite3.connect``.
- Schema apply failure: propagates from ``executescript`` after a
  loud message naming the path.
- Per-day write: wrapped in ``with conn:`` (autocommit transaction).
  Any exception rolls back the day's rows and propagates. A partial
  day will never land on disk.
- Multi-day position (entry on one day, exit on another -- shouldn't
  happen post-EOD-flatten but defensive): the writer logs a WARNING
  with ``unmatched_decision_id`` and skips the fill row. The
  replay_decisions row is still written (it's mapped to the day the
  decision fired, regardless of when the position closes).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from data.replay.config import ReplayConfig
from sim.portfolio import SimulatedPortfolio

if TYPE_CHECKING:
    # Avoid a runtime circular import: driver.py imports
    # write_day_results from this module, so persistence.py cannot
    # import DayRunResult at module-load time. ``from __future__ import
    # annotations`` stringifies the type hint, so this TYPE_CHECKING
    # guard is sufficient for static analysis.
    from data.replay.driver import DayRunResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    config_json TEXT NOT NULL,
    repo_sha TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS replay_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    trading_date TEXT NOT NULL,
    tick_et TEXT NOT NULL,
    ticker TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    action TEXT NOT NULL,
    setup_label TEXT,
    confidence INTEGER,
    reasoning TEXT,
    raw_response TEXT,
    risk_check_result TEXT,
    tier_provenance TEXT,
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_run_date
    ON replay_decisions(run_id, trading_date);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker
    ON replay_decisions(run_id, ticker, tick_et);

CREATE TABLE IF NOT EXISTS replay_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    fill_price REAL NOT NULL,
    fill_timestamp TEXT NOT NULL,
    stop_price REAL NOT NULL,
    exit_timestamp TEXT,
    exit_price REAL,
    exit_reason TEXT,
    realized_pl REAL,
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id),
    FOREIGN KEY (decision_id) REFERENCES replay_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_fills_run ON replay_fills(run_id);

CREATE TABLE IF NOT EXISTS replay_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_qty INTEGER NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id),
    FOREIGN KEY (decision_id) REFERENCES replay_decisions(id)
);

CREATE TABLE IF NOT EXISTS replay_equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    portfolio_name TEXT NOT NULL,
    bar_et TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    n_open_positions INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES replay_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_equity_run_curve
    ON replay_equity_curve(run_id, portfolio_name, bar_et);
"""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def init_replay_db(path: Path) -> sqlite3.Connection:
    """Open a read-write connection and apply the schema (idempotent).

    Uses ``CREATE TABLE IF NOT EXISTS`` for every table + index, so
    calling this against an already-initialized DB is a no-op (no
    error, no ALTER). For schema migrations (column additions in
    future sub-tasks), the migration logic will live here too.

    Args:
        path: filesystem path to ``replay_results.db``. Typically
            ``ReplayConfig.replay_db_path``. Parent directory must
            exist; this function does NOT mkdir (callers own
            filesystem prep so tests can stage tmpdirs without surprise
            directory creation).

    Returns:
        ``sqlite3.Connection`` in read-write mode. Caller closes.

    Raises:
        sqlite3.OperationalError: parent directory missing or
            permission denied.
        sqlite3.DatabaseError: file exists but is not a valid SQLite
            database (corrupt, half-transferred).
    """
    # Sanity-probe the path so the failure is loud and named here
    # rather than on the first INSERT.
    if not path.parent.exists():
        raise FileNotFoundError(
            f"Parent directory does not exist: {path.parent}. "
            "Run ``path.parent.mkdir(parents=True, exist_ok=True)`` "
            "before init_replay_db."
        )
    conn = sqlite3.connect(path)
    try:
        # Force a header parse so a corrupt existing file fails loud here.
        conn.execute("PRAGMA schema_version").fetchone()
        # Enable FK enforcement so bad inserts surface immediately.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn


def start_run(
    conn: sqlite3.Connection,
    *,
    config: ReplayConfig,
    repo_sha: str | None = None,
) -> int:
    """Insert a new ``replay_runs`` row and return its ``run_id``.

    ``started_at`` is stamped at call time with UTC ISO 8601.
    ``config_json`` is the full dataclass dump (``dataclasses.asdict``
    -> ``json.dumps(default=str)`` so Path / date values stringify
    cleanly). The run is "open" until ``complete_run`` lands.

    Args:
        conn: opened by ``init_replay_db``.
        config: the run's ``ReplayConfig``.
        repo_sha: optional ``git rev-parse HEAD`` at run start, for
            reproducibility. Callers compute this; this module does
            not shell out.

    Returns:
        The new ``run_id`` (``cursor.lastrowid``).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    config_json = _serialize_config(config)
    cur = conn.execute(
        "INSERT INTO replay_runs (started_at, config_json, repo_sha) "
        "VALUES (?, ?, ?)",
        (started_at, config_json, repo_sha),
    )
    conn.commit()
    run_id = cur.lastrowid
    assert run_id is not None  # AUTOINCREMENT guarantees this
    return run_id


def write_day_results(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    day_result: DayRunResult,
    llm_portfolio: SimulatedPortfolio,
    base_portfolio: SimulatedPortfolio | None = None,
) -> None:
    """Write one day's decisions / fills / rejections / equity curve.

    Wraps every write in ``with conn:`` (sqlite3's autocommit
    transaction) so the day either lands fully or not at all. Skipped
    days are a no-op -- absence of rows is the signal.

    For each portfolio side (LLM always; base when ``base_portfolio`` is
    supplied), the writer:

    1. Inserts each ``TickDecision`` into ``replay_decisions`` with the
       appropriate ``decision_source`` ('live_merged' for LLM, 'base'
       for the base rule-based pass). Captures a per-side per-day
       decision_id -> global ``replay_decisions.id`` map.
    2. Inserts each ``RejectedEntry`` into ``replay_rejections`` with
       its decision_id resolved through that side's map.
    3. Iterates the side's portfolio's ``closed_positions`` filtered to
       those with ``entry_timestamp.date() == trading_date``. Writes
       one ``replay_fills`` row per Position with entry + exit columns
       populated. Multi-day positions (entry on prior day) are skipped
       with a WARNING -- they shouldn't exist with EOD flatten, but
       the guard is defensive (Rule 18 visible degradation).
    4. Iterates the side's portfolio's still-``positions`` (open at
       write time, entry today). Defensive non-flatten branch: writes
       a row with NULL exit columns.
    5. Inserts each ``EquityPoint`` from the side's equity curve into
       ``replay_equity_curve`` with the appropriate ``portfolio_name``
       ('llm' / 'base'). The autoincrement ``id`` preserves the
       chronological order even when two points share a timestamp
       (e.g. pre + post EOD-flatten at 15:55).

    Args:
        conn: opened by ``init_replay_db``.
        run_id: from ``start_run``.
        day_result: the ``DayRunResult`` for one trading day. Provides
            both LLM and base decisions / rejections / equity-curve
            sequences via its parallel field sets.
        llm_portfolio: the LLM-side ``SimulatedPortfolio``.
        base_portfolio: optional base-strategy ``SimulatedPortfolio``.
            When provided, the base side's rows are written in the
            same transaction. Default ``None`` keeps backward
            compatibility with pre-#17 callers.

    Side effects: writes to all five tables (zero rows for empty
    days). No return value.
    """
    if day_result.skipped:
        return

    trading_date_str = day_result.trading_date.isoformat()

    with conn:
        # LLM side -- always written (the only side that existed pre-#17).
        _write_portfolio_side(
            conn,
            run_id=run_id,
            trading_date_str=trading_date_str,
            trading_date=day_result.trading_date,
            decisions=day_result.decisions,
            rejections=day_result.rejections,
            equity_curve=day_result.equity_curve,
            portfolio=llm_portfolio,
            decision_source="live_merged",
            portfolio_name="llm",
        )

        # Base side -- written only when base_portfolio is supplied.
        # Same transaction so a base-write failure rolls back the LLM
        # side too (atomic per-day semantics).
        if base_portfolio is not None:
            _write_portfolio_side(
                conn,
                run_id=run_id,
                trading_date_str=trading_date_str,
                trading_date=day_result.trading_date,
                decisions=day_result.base_decisions,
                rejections=day_result.base_rejections,
                equity_curve=day_result.base_equity_curve,
                portfolio=base_portfolio,
                decision_source="base",
                portfolio_name="base",
            )

        # Tier 3 (Opus) labeling rows (M2.2 sub-task #20). T3 has no
        # portfolio / fills / rejections / equity curve -- it's pure
        # labeling for the comparison report's § 5d analysis. Just
        # write the decisions with decision_source='t3_only'.
        for td in day_result.t3_decisions:
            conn.execute(
                "INSERT INTO replay_decisions "
                "(run_id, trading_date, tick_et, ticker, decision_source, "
                " action, setup_label, confidence, reasoning, "
                " tier_provenance) "
                "VALUES (?, ?, ?, ?, 't3_only', ?, ?, ?, ?, ?)",
                (
                    run_id,
                    trading_date_str,
                    td.tick_et.isoformat(),
                    td.ticker,
                    td.decision.action,
                    td.decision.setup_label,
                    td.decision.confidence,
                    td.decision.reasoning,
                    td.decision.tier_provenance,
                ),
            )


def _write_portfolio_side(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    trading_date_str: str,
    trading_date,  # datetime.date  -- avoid extra import at module top
    decisions,
    rejections,
    equity_curve,
    portfolio: SimulatedPortfolio,
    decision_source: str,
    portfolio_name: str,
) -> None:
    """Write one portfolio side's rows under an open transaction.

    Called by ``write_day_results`` once for the LLM side and (when
    enabled) once for the base side. Schema is the same for both --
    only ``decision_source`` and ``portfolio_name`` differ. Each side
    builds its own per-day -> global decision_id map so the FK
    linkage is unambiguous even though both sides use per-day
    decision_id sequences starting at 1.
    """
    # 1) Decisions
    per_day_to_global: dict[int, int] = {}
    for idx, td in enumerate(decisions):
        per_day_id = idx + 1
        cur = conn.execute(
            "INSERT INTO replay_decisions "
            "(run_id, trading_date, tick_et, ticker, decision_source, "
            " action, setup_label, confidence, reasoning) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                trading_date_str,
                td.tick_et.isoformat(),
                td.ticker,
                decision_source,
                td.decision.action,
                td.decision.setup_label,
                td.decision.confidence,
                td.decision.reasoning,
            ),
        )
        global_id = cur.lastrowid
        assert global_id is not None
        per_day_to_global[per_day_id] = global_id

    # 2) Rejections
    for rj in rejections:
        global_decision_id = per_day_to_global.get(rj.decision_id)
        if global_decision_id is None:
            logger.warning(
                "write_day_results[%s]: rejection has unmatched "
                "decision_id=%d (ticker=%s, tick=%s); skipping row",
                portfolio_name, rj.decision_id, rj.ticker, rj.tick_et,
            )
            continue
        conn.execute(
            "INSERT INTO replay_rejections "
            "(run_id, decision_id, ticker, side, requested_qty, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id, global_decision_id, rj.ticker,
                rj.side, rj.requested_qty, rj.reason,
            ),
        )

    # 3) Fills -- closed positions opened today
    for pos in portfolio.closed_positions:
        if pos.entry_timestamp.date() != trading_date:
            continue
        global_decision_id = per_day_to_global.get(pos.decision_id)
        if global_decision_id is None:
            logger.warning(
                "write_day_results[%s]: fill has unmatched decision_id=%d "
                "(ticker=%s, entry=%s); skipping row",
                portfolio_name, pos.decision_id,
                pos.ticker, pos.entry_timestamp,
            )
            continue
        conn.execute(
            "INSERT INTO replay_fills "
            "(run_id, decision_id, ticker, side, qty, fill_price, "
            " fill_timestamp, stop_price, exit_timestamp, exit_price, "
            " exit_reason, realized_pl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, global_decision_id, pos.ticker, pos.side, pos.qty,
                pos.entry_price, pos.entry_timestamp.isoformat(),
                pos.stop_price,
                pos.exit_timestamp.isoformat()
                if pos.exit_timestamp is not None else None,
                pos.exit_price, pos.exit_reason, pos.realized_pl,
            ),
        )

    # Defensive: still-open positions whose entry was today
    for ticker, pos in portfolio.positions.items():
        if not pos.is_open:
            continue
        if pos.entry_timestamp.date() != trading_date:
            continue
        global_decision_id = per_day_to_global.get(pos.decision_id)
        if global_decision_id is None:
            logger.warning(
                "write_day_results[%s]: open fill has unmatched "
                "decision_id=%d (ticker=%s); skipping row",
                portfolio_name, pos.decision_id, ticker,
            )
            continue
        conn.execute(
            "INSERT INTO replay_fills "
            "(run_id, decision_id, ticker, side, qty, fill_price, "
            " fill_timestamp, stop_price, exit_timestamp, exit_price, "
            " exit_reason, realized_pl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            (
                run_id, global_decision_id, pos.ticker,
                pos.side, pos.qty, pos.entry_price,
                pos.entry_timestamp.isoformat(), pos.stop_price,
            ),
        )

    # 4) Equity curve
    for pt in equity_curve:
        conn.execute(
            "INSERT INTO replay_equity_curve "
            "(run_id, portfolio_name, bar_et, equity, cash, "
            " n_open_positions) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id, portfolio_name, pt.timestamp.isoformat(),
                pt.equity, pt.cash, pt.n_open_positions,
            ),
        )


def complete_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    summary_json: str | None = None,
) -> None:
    """Stamp a run as complete with optional summary JSON.

    ``completed_at`` is set to the current UTC time in ISO 8601.
    ``summary_json`` is opaque to this module -- callers serialize
    aggregated metrics (winners/losers, total P&L, drawdown, etc.) as
    they see fit. Pass ``None`` if no summary is available yet.

    Idempotent in the sense that calling it twice on the same
    ``run_id`` overwrites the previous ``completed_at`` and
    ``summary_json``. The replay caller's pattern is one call per
    run at the end.
    """
    completed_at = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "UPDATE replay_runs "
            "SET completed_at = ?, summary_json = ? "
            "WHERE run_id = ?",
            (completed_at, summary_json, run_id),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _serialize_config(config: ReplayConfig) -> str:
    """JSON-serialize a ReplayConfig dataclass.

    ``dataclasses.asdict`` produces nested dicts; ``json.dumps`` with
    ``default=str`` stringifies the non-JSON types we hold (Path, date,
    Literal-typed strings round-trip as themselves). The output is one
    line; the comparison report can pretty-print on demand.

    The ``tickers`` field is normalized to a list (it's a tuple inside
    the dataclass) for JSON canonicalization.
    """
    d = dataclasses.asdict(config)
    # tickers may be a tuple or the literal "watchlist"
    if isinstance(d.get("tickers"), tuple):
        d["tickers"] = list(d["tickers"])
    return json.dumps(d, default=str, sort_keys=True)


__all__ = [
    "complete_run",
    "init_replay_db",
    "start_run",
    "write_day_results",
]
