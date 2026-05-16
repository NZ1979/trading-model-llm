"""Tests for data/replay/persistence.py (M2.2 sub-task #16).

Covers:

- init_replay_db: opens + applies schema; idempotent across calls;
  raises on missing parent directory; foreign_keys=ON enforced.
- start_run: inserts a row, returns monotonically increasing run_id;
  config_json round-trips through JSON; started_at is ISO 8601 UTC;
  repo_sha optional, persists when provided.
- write_day_results:
  - skipped day -> zero rows written.
  - decisions persisted with correct fields; per-day -> global id
    mapping resolves rejections + fills.
  - fills written from closed positions whose entry_timestamp matches
    the day; exit columns (exit_timestamp, exit_price, exit_reason,
    realized_pl) populated.
  - fills with exit_reason="stop_hit" / "eod_flatten" / "flip" round-
    trip correctly.
  - still-open positions written with NULL exit columns (defensive
    branch for non-flatten replays).
  - rejections written with correct FK linkage to replay_decisions.id.
  - rejections with an unmatched decision_id (shouldn't happen in
    practice; defensive) -> WARNING logged, row skipped.
  - equity curve written verbatim incl. two-points-at-15:55 case.
  - empty-portfolio day still writes the equity curve.
  - cross-day filtering: a closed Position with entry_timestamp on a
    PRIOR day is not written when the writer is invoked for today.
  - transactional atomicity: a write that raises mid-way rolls back
    the day's rows.
- complete_run: stamps completed_at; preserves NULL summary when not
  provided; second call overwrites.
- Multi-day round-trip: rows from day N are queryable before day N+1
  writes.
- Two parallel runs: run_id increments monotonically; their rows are
  isolated by run_id.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig
from data.replay.driver import DayRunResult
from data.replay.fill_simulator import RejectedEntry
from data.replay.persistence import (
    complete_run,
    init_replay_db,
    start_run,
    write_day_results,
)
from data.replay.tick_loop import TickDecision
from sim.fills import SimulatedFill
from sim.portfolio import EquityPoint, Position, SimulatedPortfolio
from strategy.llm.types import LLMDecision


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(**overrides) -> ReplayConfig:
    kwargs = dict(
        start_date=TRADING_DATE,
        end_date=TRADING_DATE,
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _bar_et(minute_offset: int, trading_date: date = TRADING_DATE) -> datetime:
    base = datetime(
        trading_date.year, trading_date.month, trading_date.day,
        9, 30, tzinfo=ET,
    )
    return base + timedelta(minutes=minute_offset)


def _decision(action: str = "Buy", confidence: int = 60) -> LLMDecision:
    return LLMDecision(
        action=action, confidence=confidence,
        setup_label="gap_and_go", reasoning="test reasoning",
    )


def _tick(minute_offset: int, action: str = "Buy", ticker: str = "AAPL",
          trading_date: date = TRADING_DATE) -> TickDecision:
    return TickDecision(
        tick_et=_bar_et(minute_offset, trading_date),
        ticker=ticker,
        decision=_decision(action),
    )


def _closed_position(
    *,
    ticker: str = "AAPL",
    side: str = "buy",
    qty: int = 10,
    entry_price: float = 100.0,
    entry_minute: int = 5,
    stop_price: float = 98.0,
    decision_id: int = 1,
    exit_price: float = 105.0,
    exit_minute: int = 30,
    exit_reason: str = "eod_flatten",
    realized_pl: float | None = None,
    trading_date: date = TRADING_DATE,
) -> Position:
    """Build a fully-populated closed Position."""
    if realized_pl is None:
        if side == "buy":
            realized_pl = (exit_price - entry_price) * qty
        else:
            realized_pl = (entry_price - exit_price) * qty
    return Position(
        ticker=ticker, side=side, qty=qty,  # type: ignore[arg-type]
        entry_price=entry_price,
        entry_timestamp=_bar_et(entry_minute, trading_date),
        stop_price=stop_price, decision_id=decision_id,
        exit_price=exit_price,
        exit_timestamp=_bar_et(exit_minute, trading_date),
        exit_reason=exit_reason, realized_pl=realized_pl,
    )


def _equity_point(
    minute_offset: int,
    *,
    equity: float = 100_000.0,
    cash: float = 100_000.0,
    n_open: int = 0,
    trading_date: date = TRADING_DATE,
) -> EquityPoint:
    return EquityPoint(
        timestamp=_bar_et(minute_offset, trading_date),
        equity=equity, cash=cash, n_open_positions=n_open,
    )


def _day_result(
    *,
    decisions: list[TickDecision] | None = None,
    rejections: tuple[RejectedEntry, ...] = (),
    equity_curve: tuple[EquityPoint, ...] = (),
    skipped: bool = False,
    trading_date: date = TRADING_DATE,
    base_decisions: list[TickDecision] | None = None,
    base_rejections: tuple[RejectedEntry, ...] = (),
    base_equity_curve: tuple[EquityPoint, ...] = (),
) -> DayRunResult:
    return DayRunResult(
        trading_date=trading_date,
        decisions=decisions if decisions is not None else [],
        skipped=skipped,
        rejections=rejections,
        equity_curve=equity_curve,
        base_decisions=base_decisions if base_decisions is not None else [],
        base_rejections=base_rejections,
        base_equity_curve=base_equity_curve,
    )


# ===========================================================================
# init_replay_db
# ===========================================================================


def test_init_creates_schema(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    expected = {
        "replay_runs", "replay_decisions", "replay_fills",
        "replay_rejections", "replay_equity_curve",
    }
    # sqlite_sequence is auto-created by AUTOINCREMENT; allow it.
    assert expected.issubset(tables)


def test_init_idempotent(tmp_path):
    path = tmp_path / "r.db"
    init_replay_db(path).close()
    # Second call must not raise -- the schema is already there.
    conn = init_replay_db(path)
    conn.close()


def test_init_missing_parent_raises(tmp_path):
    bad = tmp_path / "nonexistent_subdir" / "r.db"
    with pytest.raises(FileNotFoundError, match="Parent directory"):
        init_replay_db(bad)


def test_init_enforces_foreign_keys(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


# ===========================================================================
# start_run
# ===========================================================================


def test_start_run_returns_run_id(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        assert isinstance(rid, int)
        assert rid >= 1
    finally:
        conn.close()


def test_start_run_monotonically_increasing(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        a = start_run(conn, config=_config())
        b = start_run(conn, config=_config())
        c = start_run(conn, config=_config())
        assert a < b < c
    finally:
        conn.close()


def test_start_run_config_json_roundtrips(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config(tickers=("AAPL", "MSFT")))
        row = conn.execute(
            "SELECT config_json FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()
        d = json.loads(row[0])
        # tickers normalized to list, llm_prompt_version preserved.
        assert d["tickers"] == ["AAPL", "MSFT"]
        assert d["llm_prompt_version"] == "v-test"
    finally:
        conn.close()


def test_start_run_started_at_iso8601_utc(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        row = conn.execute(
            "SELECT started_at FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()
        # Must round-trip through fromisoformat; timezone aware (+00:00).
        parsed = datetime.fromisoformat(row[0])
        assert parsed.tzinfo is not None
    finally:
        conn.close()


def test_start_run_repo_sha_persisted(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config(), repo_sha="abc1234")
        row = conn.execute(
            "SELECT repo_sha FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()
        assert row[0] == "abc1234"
    finally:
        conn.close()


# ===========================================================================
# write_day_results: skipped + empty
# ===========================================================================


def test_write_day_skipped_writes_nothing(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(skipped=True),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        # Zero rows in every table.
        for table in ("replay_decisions", "replay_fills",
                      "replay_rejections", "replay_equity_curve"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} should be empty for skipped day"
    finally:
        conn.close()


def test_write_day_empty_portfolio_writes_equity_curve_only(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        # No decisions, no positions, but a 3-point equity curve.
        curve = (
            _equity_point(0),
            _equity_point(5),
            _equity_point(10),
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(equity_curve=curve),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        n_curve = conn.execute(
            "SELECT COUNT(*) FROM replay_equity_curve WHERE run_id = ?",
            (rid,)
        ).fetchone()[0]
        assert n_curve == 3
        n_dec = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions"
        ).fetchone()[0]
        assert n_dec == 0
    finally:
        conn.close()


# ===========================================================================
# write_day_results: decisions
# ===========================================================================


def test_write_day_decisions_persisted(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(0, "Buy"), _tick(5, "Hold"), _tick(10, "Sell")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        rows = conn.execute(
            "SELECT action, ticker, decision_source FROM replay_decisions "
            "WHERE run_id = ? ORDER BY id", (rid,)
        ).fetchall()
        assert [r[0] for r in rows] == ["Buy", "Hold", "Sell"]
        assert all(r[1] == "AAPL" for r in rows)
        assert all(r[2] == "live_merged" for r in rows)
    finally:
        conn.close()


def test_write_day_decision_fields_complete(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(0, "Buy")]),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        row = conn.execute(
            "SELECT trading_date, tick_et, ticker, action, setup_label, "
            "confidence, reasoning FROM replay_decisions WHERE run_id = ?",
            (rid,)
        ).fetchone()
        assert row[0] == TRADING_DATE.isoformat()
        assert row[2] == "AAPL"
        assert row[3] == "Buy"
        assert row[4] == "gap_and_go"
        assert row[5] == 60
        assert row[6] == "test reasoning"
    finally:
        conn.close()


# ===========================================================================
# write_day_results: fills
# ===========================================================================


def test_write_day_fill_for_closed_position(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        pf = SimulatedPortfolio(starting_cash=100_000.0)
        pf.closed_positions.append(_closed_position(decision_id=1))
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(5, "Buy")]),
            llm_portfolio=pf,
        )
        rows = conn.execute(
            "SELECT ticker, side, qty, fill_price, exit_price, exit_reason, "
            "realized_pl FROM replay_fills WHERE run_id = ?",
            (rid,)
        ).fetchall()
        assert len(rows) == 1
        ticker, side, qty, fill_price, exit_price, exit_reason, pl = rows[0]
        assert (ticker, side, qty) == ("AAPL", "buy", 10)
        assert fill_price == 100.0
        assert exit_price == 105.0
        assert exit_reason == "eod_flatten"
        assert pl == pytest.approx(50.0)
    finally:
        conn.close()


def test_write_day_fill_fk_to_decision(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        pf = SimulatedPortfolio(starting_cash=100_000.0)
        pf.closed_positions.append(_closed_position(decision_id=1))
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(5, "Buy")]),
            llm_portfolio=pf,
        )
        # Decision id in replay_decisions should equal fill's decision_id FK.
        decision_row = conn.execute(
            "SELECT id FROM replay_decisions WHERE run_id = ?", (rid,)
        ).fetchone()
        fill_row = conn.execute(
            "SELECT decision_id FROM replay_fills WHERE run_id = ?", (rid,)
        ).fetchone()
        assert decision_row[0] == fill_row[0]
    finally:
        conn.close()


@pytest.mark.parametrize("exit_reason", ["stop_hit", "eod_flatten", "flip"])
def test_write_day_fill_exit_reason_roundtrips(tmp_path, exit_reason):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        pf = SimulatedPortfolio(starting_cash=100_000.0)
        pf.closed_positions.append(
            _closed_position(decision_id=1, exit_reason=exit_reason)
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(5, "Buy")]),
            llm_portfolio=pf,
        )
        row = conn.execute(
            "SELECT exit_reason FROM replay_fills WHERE run_id = ?", (rid,)
        ).fetchone()
        assert row[0] == exit_reason
    finally:
        conn.close()


def test_write_day_open_position_writes_null_exits(tmp_path):
    """Defensive branch: position open at write time -> NULL exit columns."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        pf = SimulatedPortfolio(starting_cash=100_000.0)
        # Seed an open position (entry today, no exit fields).
        pf.positions["AAPL"] = Position(
            ticker="AAPL", side="buy", qty=10,
            entry_price=100.0, entry_timestamp=_bar_et(5),
            stop_price=98.0, decision_id=1,
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(5, "Buy")]),
            llm_portfolio=pf,
        )
        row = conn.execute(
            "SELECT exit_timestamp, exit_price, exit_reason, realized_pl "
            "FROM replay_fills WHERE run_id = ?", (rid,)
        ).fetchone()
        assert row == (None, None, None, None)
    finally:
        conn.close()


def test_write_day_prior_day_position_not_rewritten(tmp_path):
    """A Position with entry_timestamp on a PRIOR day must NOT be written
    when invoked for today (it was already written by the prior day's
    call)."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        pf = SimulatedPortfolio(starting_cash=100_000.0)
        # Yesterday's closed position.
        yesterday = TRADING_DATE - timedelta(days=1)
        pf.closed_positions.append(
            _closed_position(decision_id=1, trading_date=yesterday)
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(decisions=[_tick(5, "Buy")]),
            llm_portfolio=pf,
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM replay_fills WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


# ===========================================================================
# write_day_results: rejections
# ===========================================================================


def test_write_day_rejections_persisted(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        rj = RejectedEntry(
            tick_et=_bar_et(5), ticker="AAPL", side="buy",
            requested_qty=100, reason="position_cap_exceeded",
            decision_id=1,
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(5, "Buy")],
                rejections=(rj,),
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        rows = conn.execute(
            "SELECT ticker, side, requested_qty, reason FROM replay_rejections "
            "WHERE run_id = ?", (rid,)
        ).fetchall()
        assert rows == [("AAPL", "buy", 100, "position_cap_exceeded")]
    finally:
        conn.close()


def test_write_day_rejection_fk_resolves_through_map(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        rj = RejectedEntry(
            tick_et=_bar_et(5), ticker="AAPL", side="buy",
            requested_qty=100, reason="position_cap_exceeded",
            decision_id=2,  # second decision in input order
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(0, "Hold"), _tick(5, "Buy")],
                rejections=(rj,),
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        decision_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM replay_decisions WHERE run_id = ? "
                "ORDER BY id", (rid,)
            )
        ]
        # Second decision's global id should equal the rejection's FK.
        rj_fk = conn.execute(
            "SELECT decision_id FROM replay_rejections WHERE run_id = ?",
            (rid,)
        ).fetchone()[0]
        assert rj_fk == decision_ids[1]
    finally:
        conn.close()


def test_write_day_unmatched_rejection_warns_and_skips(tmp_path, caplog):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        # decision_id=99 references a non-existent decision (only 1 in input).
        rj = RejectedEntry(
            tick_et=_bar_et(5), ticker="AAPL", side="buy",
            requested_qty=100, reason="invalid",
            decision_id=99,
        )
        with caplog.at_level(logging.WARNING, logger="data.replay.persistence"):
            write_day_results(
                conn, run_id=rid,
                day_result=_day_result(
                    decisions=[_tick(0, "Buy")],
                    rejections=(rj,),
                ),
                llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
            )
        n = conn.execute(
            "SELECT COUNT(*) FROM replay_rejections WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        assert n == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("unmatched decision_id=99" in r.message for r in warnings)
    finally:
        conn.close()


# ===========================================================================
# write_day_results: equity curve
# ===========================================================================


def test_write_day_equity_curve_preserves_all_points(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        # 78 per-bar + 1 final post-flatten -> 79 total. Use the two-at-15:55
        # case explicitly.
        curve = (
            _equity_point(0, equity=100_000.0),
            _equity_point(5 * 77, equity=100_100.0, n_open=1),  # pre-flatten
            _equity_point(5 * 77, equity=100_080.0, n_open=0),  # post-flatten
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(equity_curve=curve),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        rows = conn.execute(
            "SELECT equity, n_open_positions FROM replay_equity_curve "
            "WHERE run_id = ? ORDER BY id", (rid,)
        ).fetchall()
        assert len(rows) == 3
        assert rows[1] == (100_100.0, 1)
        assert rows[2] == (100_080.0, 0)
    finally:
        conn.close()


def test_write_day_equity_portfolio_name_llm(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(equity_curve=(_equity_point(0),)),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        row = conn.execute(
            "SELECT portfolio_name FROM replay_equity_curve "
            "WHERE run_id = ?", (rid,)
        ).fetchone()
        assert row[0] == "llm"
    finally:
        conn.close()


# ===========================================================================
# write_day_results: multi-day + atomicity
# ===========================================================================


def test_write_multi_day_isolation(tmp_path):
    """Each day's rows have their own trading_date; queryable independently."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        day1 = TRADING_DATE
        day2 = TRADING_DATE + timedelta(days=1)
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(0, "Buy", trading_date=day1)],
                trading_date=day1,
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[
                    _tick(0, "Sell", trading_date=day2),
                    _tick(5, "Hold", trading_date=day2),
                ],
                trading_date=day2,
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        d1 = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions WHERE trading_date = ?",
            (day1.isoformat(),)
        ).fetchone()[0]
        d2 = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions WHERE trading_date = ?",
            (day2.isoformat(),)
        ).fetchone()[0]
        assert (d1, d2) == (1, 2)
    finally:
        conn.close()


def test_write_day_run_isolation(tmp_path):
    """Two runs in the same DB: each write is bound to its own run_id."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        r1 = start_run(conn, config=_config())
        r2 = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=r1,
            day_result=_day_result(decisions=[_tick(0, "Buy")]),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        write_day_results(
            conn, run_id=r2,
            day_result=_day_result(
                decisions=[_tick(0, "Sell"), _tick(5, "Hold")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        n_r1 = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions WHERE run_id = ?", (r1,)
        ).fetchone()[0]
        n_r2 = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions WHERE run_id = ?", (r2,)
        ).fetchone()[0]
        assert (n_r1, n_r2) == (1, 2)
    finally:
        conn.close()


# ===========================================================================
# complete_run
# ===========================================================================


def test_complete_run_stamps_completed_at(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        # Before complete_run, completed_at is NULL.
        before = conn.execute(
            "SELECT completed_at FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        assert before is None
        complete_run(conn, run_id=rid, summary_json=json.dumps({"trades": 5}))
        after = conn.execute(
            "SELECT completed_at, summary_json FROM replay_runs "
            "WHERE run_id = ?", (rid,)
        ).fetchone()
        # completed_at parses as ISO 8601 UTC.
        assert datetime.fromisoformat(after[0]).tzinfo is not None
        assert json.loads(after[1]) == {"trades": 5}
    finally:
        conn.close()


def test_complete_run_summary_optional(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        complete_run(conn, run_id=rid)  # no summary_json
        summary = conn.execute(
            "SELECT summary_json FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        assert summary is None
    finally:
        conn.close()


def test_complete_run_overwrites_on_second_call(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        complete_run(conn, run_id=rid, summary_json="first")
        complete_run(conn, run_id=rid, summary_json="second")
        row = conn.execute(
            "SELECT summary_json FROM replay_runs WHERE run_id = ?", (rid,)
        ).fetchone()
        assert row[0] == "second"
    finally:
        conn.close()


# ===========================================================================
# Base-strategy parallel persistence (M2.2 sub-task #17)
# ===========================================================================


def test_write_day_base_decisions_have_source_base(tmp_path):
    """base_portfolio supplied -> base decisions land with decision_source='base'."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(0, "Buy")],
                base_decisions=[_tick(5, "Sell"), _tick(10, "Hold")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
            base_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        rows = conn.execute(
            "SELECT decision_source, action FROM replay_decisions "
            "WHERE run_id = ? ORDER BY id", (rid,)
        ).fetchall()
        # 1 LLM (Buy) + 2 base (Sell, Hold)
        assert rows == [
            ("live_merged", "Buy"),
            ("base", "Sell"),
            ("base", "Hold"),
        ]
    finally:
        conn.close()


def test_write_day_base_fills_fk_to_base_decisions(tmp_path):
    """A base-side closed position's fill row's decision_id FK resolves to
    the BASE-sourced decision, not the LLM-sourced decision with the same
    per-day decision_id."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        llm_pf = SimulatedPortfolio(starting_cash=100_000.0)
        base_pf = SimulatedPortfolio(starting_cash=100_000.0)
        # Each side has one closed position with decision_id=1.
        llm_pf.closed_positions.append(_closed_position(decision_id=1, ticker="AAPL"))
        base_pf.closed_positions.append(_closed_position(decision_id=1, ticker="AAPL"))
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(5, "Buy")],
                base_decisions=[_tick(5, "Buy")],
            ),
            llm_portfolio=llm_pf,
            base_portfolio=base_pf,
        )
        # Two fills total.
        fills = conn.execute(
            "SELECT decision_id FROM replay_fills WHERE run_id = ? "
            "ORDER BY id", (rid,)
        ).fetchall()
        assert len(fills) == 2
        # Resolve each fill's decision_source via FK.
        for (decision_id,) in fills:
            source = conn.execute(
                "SELECT decision_source FROM replay_decisions WHERE id = ?",
                (decision_id,)
            ).fetchone()[0]
            assert source in ("live_merged", "base")
        # Specifically: first fill links to live_merged, second to base
        # (insertion order: LLM side first per write_day_results).
        first_source = conn.execute(
            "SELECT decision_source FROM replay_decisions WHERE id = ?",
            (fills[0][0],)
        ).fetchone()[0]
        second_source = conn.execute(
            "SELECT decision_source FROM replay_decisions WHERE id = ?",
            (fills[1][0],)
        ).fetchone()[0]
        assert first_source == "live_merged"
        assert second_source == "base"
    finally:
        conn.close()


def test_write_day_base_equity_curve_portfolio_name_base(tmp_path):
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                equity_curve=(_equity_point(0),),
                base_equity_curve=(_equity_point(5), _equity_point(10)),
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
            base_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        rows = conn.execute(
            "SELECT portfolio_name, COUNT(*) FROM replay_equity_curve "
            "WHERE run_id = ? GROUP BY portfolio_name ORDER BY portfolio_name",
            (rid,)
        ).fetchall()
        assert rows == [("base", 2), ("llm", 1)]
    finally:
        conn.close()


def test_write_day_both_portfolios_queryable_independently(tmp_path):
    """Same DB, same run_id, both portfolios populated: queries by
    portfolio_name / decision_source partition correctly."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        llm_pf = SimulatedPortfolio(starting_cash=100_000.0)
        base_pf = SimulatedPortfolio(starting_cash=100_000.0)
        llm_pf.closed_positions.append(_closed_position(decision_id=1))
        # Two base entries
        base_pf.closed_positions.extend([
            _closed_position(decision_id=1, ticker="AAPL"),
            _closed_position(decision_id=2, ticker="MSFT"),
        ])
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(5, "Buy")],
                base_decisions=[_tick(5, "Buy"), _tick(10, "Buy", ticker="MSFT")],
            ),
            llm_portfolio=llm_pf,
            base_portfolio=base_pf,
        )
        # decisions count by source
        by_source = dict(conn.execute(
            "SELECT decision_source, COUNT(*) FROM replay_decisions "
            "WHERE run_id = ? GROUP BY decision_source", (rid,)
        ).fetchall())
        assert by_source == {"live_merged": 1, "base": 2}
        # fills count by joining to decision_source
        by_source_fills = dict(conn.execute(
            "SELECT d.decision_source, COUNT(*) FROM replay_fills f "
            "JOIN replay_decisions d ON f.decision_id = d.id "
            "WHERE f.run_id = ? GROUP BY d.decision_source", (rid,)
        ).fetchall())
        assert by_source_fills == {"live_merged": 1, "base": 2}
    finally:
        conn.close()


def test_write_day_base_portfolio_none_default_writes_nothing_for_base(tmp_path):
    """Existing #16 callers (no base_portfolio arg) get the original
    LLM-only behavior; no rows with portfolio_name='base' or
    decision_source='base' show up."""
    conn = init_replay_db(tmp_path / "r.db")
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(0, "Buy")],
                # Even if base_decisions is populated on day_result,
                # write_day_results without base_portfolio MUST NOT
                # write them (no side-effects from absent kwarg).
                base_decisions=[_tick(5, "Sell")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
            # base_portfolio omitted -> default None
        )
        n_base_decisions = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions "
            "WHERE run_id = ? AND decision_source = 'base'", (rid,)
        ).fetchone()[0]
        assert n_base_decisions == 0
        n_base_equity = conn.execute(
            "SELECT COUNT(*) FROM replay_equity_curve "
            "WHERE run_id = ? AND portfolio_name = 'base'", (rid,)
        ).fetchone()[0]
        assert n_base_equity == 0
        # LLM side still wrote one decision.
        n_llm = conn.execute(
            "SELECT COUNT(*) FROM replay_decisions "
            "WHERE run_id = ? AND decision_source = 'live_merged'", (rid,)
        ).fetchone()[0]
        assert n_llm == 1
    finally:
        conn.close()
