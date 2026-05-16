"""Tests for sim/comparison.py (M2.2 sub-task #18).

Covers:

- Empty-data path: missing DB, missing run_id, empty run, output_path
  default vs override, open run (completed_at NULL).
- Section 1: metadata fields from config_json + run row.
- Section 2: decision counts pivoted by source × action.
- Section 3: ending equity, realized P&L, win rate, max drawdown,
  Sharpe ratio (with short-window note).
- Section 4: divergence buckets (extra/missed/opposite), no-base note.
- Section 5: confidence histogram (10 buckets), setup_label
  frequency, reasoning length stats.
- Section 5b/5c/5d stubs present with deferred notes.
- Section 6: rejection counts; tier-failure setup_label patterns.
- Section 7: top wins/losses ordered by realized_pl; divergent
  decisions table.
- Robustness: multiple runs in same DB are isolated; zero-trades
  handled without divide-by-zero.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig
from data.replay.driver import DayRunResult
from data.replay.fill_simulator import RejectedEntry
from data.replay.persistence import (
    complete_run, init_replay_db, start_run, write_day_results,
)
from data.replay.tick_loop import TickDecision
from sim.comparison import generate_report
from sim.portfolio import EquityPoint, Position, SimulatedPortfolio
from strategy.llm.types import LLMDecision


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Helpers (mirror test_persistence.py)
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


def _decision(
    action: str = "Buy",
    *,
    confidence: int = 60,
    setup_label: str = "gap_and_go",
    tier_provenance: str | None = None,
) -> LLMDecision:
    return LLMDecision(
        action=action, confidence=confidence,
        setup_label=setup_label, reasoning="r",
        tier_provenance=tier_provenance,  # type: ignore[arg-type]
    )


def _tick(
    minute_offset: int,
    action: str = "Buy",
    ticker: str = "AAPL",
    *,
    confidence: int = 60,
    setup_label: str = "gap_and_go",
    tier_provenance: str | None = None,
) -> TickDecision:
    return TickDecision(
        tick_et=_bar_et(minute_offset),
        ticker=ticker,
        decision=_decision(action, confidence=confidence,
                            setup_label=setup_label,
                            tier_provenance=tier_provenance),
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
    decisions=None,
    rejections=(),
    equity_curve=(),
    skipped=False,
    trading_date=TRADING_DATE,
    base_decisions=None,
    base_rejections=(),
    base_equity_curve=(),
    t3_decisions=None,
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
        t3_decisions=t3_decisions if t3_decisions is not None else [],
    )


def _seed_run(
    db_path: Path,
    *,
    config: ReplayConfig | None = None,
    completed: bool = True,
) -> int:
    """Create + populate an empty run; return run_id."""
    conn = init_replay_db(db_path)
    try:
        rid = start_run(conn, config=config or _config())
        if completed:
            complete_run(conn, run_id=rid)
    finally:
        conn.close()
    return rid


# ===========================================================================
# Empty-data path
# ===========================================================================


def test_missing_db_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        generate_report(
            db_path=tmp_path / "nonexistent.db",
            run_id=1,
            output_path=tmp_path / "out.md",
        )


def test_missing_run_id_raises(tmp_path):
    db = tmp_path / "r.db"
    _seed_run(db)
    with pytest.raises(ValueError, match="run_id=999"):
        generate_report(
            db_path=db, run_id=999,
            output_path=tmp_path / "out.md",
        )


def test_empty_run_renders_with_placeholders(tmp_path):
    """A run with no decisions/fills/equity still produces a report."""
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    out = tmp_path / "out.md"
    result = generate_report(db_path=db, run_id=rid, output_path=out)
    assert result == out
    text = out.read_text(encoding="utf-8")
    assert "No decisions recorded" in text
    assert "No equity curve points" in text


def test_output_path_default_uses_start_date(tmp_path, monkeypatch):
    """Default output path is docs/reports/replay_<start_date>_run<id>.md."""
    db = tmp_path / "r.db"
    rid = _seed_run(db, config=_config(
        start_date=date(2026, 4, 15), end_date=date(2026, 4, 15),
    ))
    # Run from a chdir into tmp_path so default path resolves into a writable spot.
    monkeypatch.chdir(tmp_path)
    out = generate_report(db_path=db, run_id=rid)
    assert out.name == f"replay_2026-04-15_run{rid}.md"
    assert out.parent.name == "reports"
    assert out.exists()


def test_open_run_surfaces_still_open(tmp_path):
    """completed_at IS NULL -> section 1 says 'run still open'."""
    db = tmp_path / "r.db"
    rid = _seed_run(db, completed=False)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "run still open" in text


def test_output_parent_directory_created(tmp_path):
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    nested = tmp_path / "a" / "b" / "c" / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=nested)
    assert nested.exists()


# ===========================================================================
# Section 1: metadata
# ===========================================================================


def test_section_1_shows_date_range(tmp_path):
    db = tmp_path / "r.db"
    rid = _seed_run(db, config=_config(
        start_date=date(2026, 4, 15), end_date=date(2026, 4, 17),
    ))
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "2026-04-15" in text
    assert "2026-04-17" in text


def test_section_1_shows_prompt_version_and_tier_config(tmp_path):
    db = tmp_path / "r.db"
    rid = _seed_run(db, config=_config(
        llm_prompt_version="v-special",
        t1_backend="haiku_stand_in",
        t1_model_id="claude-haiku-4-5",
    ))
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "v-special" in text
    assert "haiku_stand_in" in text
    assert "claude-haiku-4-5" in text


# ===========================================================================
# Section 2: decision summary
# ===========================================================================


def test_section_2_shows_pivot_table(tmp_path):
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(0, "Buy"), _tick(5, "Hold"), _tick(10, "Sell")],
            base_decisions=[_tick(0, "Hold"), _tick(5, "Buy")],
        ),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        base_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "## 2. Decision summary" in text
    assert "| live_merged |" in text  # column header
    assert "| base |" in text


def test_section_2_no_base_note(tmp_path):
    """Run with only LLM decisions -> note about missing base column."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(decisions=[_tick(0, "Buy")]),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No base decisions" in text


# ===========================================================================
# Section 3: portfolio performance
# ===========================================================================


def test_section_3_reports_ending_equity_and_pl(tmp_path):
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config(starting_cash=100_000.0))
    pf = SimulatedPortfolio(starting_cash=100_000.0)
    # Two closed positions: one winner (+50), one loser (-30).
    pf.closed_positions.append(_closed_position(
        decision_id=1, exit_price=105.0,  # +50
    ))
    pf.closed_positions.append(_closed_position(
        decision_id=2, exit_price=97.0,  # -30
        ticker="MSFT",
    ))
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(5, "Buy"), _tick(10, "Buy", ticker="MSFT")],
            equity_curve=(
                _equity_point(0, equity=100_000.0),
                _equity_point(390, equity=100_020.0, n_open=0),
            ),
        ),
        llm_portfolio=pf,
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Starting cash" in text
    assert "Ending equity" in text
    # 50 + (-30) = 20 net P&L; 1 winner, 1 loser
    assert "Total realized P&L:** $20.00" in text
    assert "1 winners / 1 losers" in text


def test_section_3_max_drawdown_computed(tmp_path):
    """Equity goes 100k -> 101k -> 99k -> 102k. Max DD = 2,000."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    curve = (
        _equity_point(0, equity=100_000.0),
        _equity_point(5, equity=101_000.0),
        _equity_point(10, equity=99_000.0),
        _equity_point(15, equity=102_000.0),
    )
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(equity_curve=curve),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Max drawdown:** $2,000.00" in text


def test_section_3_zero_trades_no_crash(tmp_path):
    """No closed positions -> 'no closed positions' note, no divide-by-zero."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(0, "Hold")],
            equity_curve=(_equity_point(0),),
        ),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Trades:** 0" in text
    assert "no closed positions" in text


def test_section_3_sharpe_needs_two_days(tmp_path):
    """Single-day curve -> Sharpe shows 'need >= 2 trading days'."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(equity_curve=(_equity_point(0),)),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "need" in text.lower() and "2 trading days" in text


# ===========================================================================
# Section 4: divergence
# ===========================================================================


def test_section_4_no_base_note(tmp_path):
    """Run without base_portfolio -> divergence section says so."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(decisions=[_tick(0, "Buy")]),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "## 4. Divergence analysis" in text
    assert "No comparable data" in text


def test_section_4_disagreement_bucketed(tmp_path):
    """LLM=Buy, base=Hold at the same tick -> 'LLM extra Buys' bucket."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(5, "Buy")],
            base_decisions=[_tick(5, "Hold")],
        ),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        base_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "LLM extra Buys" in text


def test_section_4_full_agreement_no_disagreements(tmp_path):
    """All actions match -> 'All LLM and base actions agreed' note."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(5, "Hold")],
            base_decisions=[_tick(5, "Hold")],
        ),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        base_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "All LLM and base actions agreed" in text


# ===========================================================================
# Section 5: LLM quality + deferred stubs
# ===========================================================================


def test_section_5_confidence_histogram_10_buckets(tmp_path):
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    decisions = [
        _tick(0, "Buy", confidence=5),
        _tick(5, "Buy", confidence=55),
        _tick(10, "Buy", confidence=95),
    ]
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(decisions=decisions),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Confidence distribution" in text
    # 10 buckets: 0-9, 10-19, ..., 90-100
    for low in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90):
        assert f"| {low}-" in text


def test_section_5_setup_label_frequency(tmp_path):
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    decisions = [
        _tick(0, "Buy", setup_label="gap_and_go"),
        _tick(5, "Buy", setup_label="gap_and_go"),
        _tick(10, "Buy", setup_label="pullback"),
    ]
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(decisions=decisions),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Setup label frequency" in text
    assert "gap_and_go" in text
    assert "pullback" in text


def test_section_5_deferred_stubs_present(tmp_path):
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "5b. Regime-stratified performance" in text
    assert "5c. Crash-period replay" in text
    assert "5d. Tier agreement" in text


# ===========================================================================
# Section 5d: Tier agreement & escalation analysis (M2.2 sub-task #21)
# ===========================================================================


def _run_with(
    db: Path,
    *,
    decisions=None,
    t3_decisions=None,
    base_decisions=None,
    llm_pf: SimulatedPortfolio | None = None,
    config: ReplayConfig | None = None,
) -> int:
    """Seed one run with the supplied decisions / portfolio, return run_id."""
    conn = init_replay_db(db)
    try:
        rid = start_run(conn, config=config or _config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=decisions or [],
                t3_decisions=t3_decisions or [],
                base_decisions=base_decisions,
            ),
            llm_portfolio=llm_pf or SimulatedPortfolio(starting_cash=100_000.0),
        )
        complete_run(conn, run_id=rid)
    finally:
        conn.close()
    return rid


def test_5d_empty_run_renders_placeholder(tmp_path):
    """No live_merged, no t3_only -> 5d header + placeholder + 5d.5
    'no live_merged' note. No crash."""
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "## 5d. Tier agreement" in text
    assert "No paired T1 / T3 decisions" in text
    assert "No live_merged decisions in this run" in text


def test_5d_live_merged_only_renders_placeholder_plus_provenance(tmp_path):
    """live_merged rows present, no t3_only -> 5d.1-5d.4 placeholder
    but 5d.5 still renders tier_provenance counts for live_merged."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No paired T1 / T3 decisions" in text
    # 5d.5 section header + the tier_provenance value rendered.
    assert "tier_provenance counts" in text
    assert "`t1_only`" in text


def test_5d_t3_only_no_live_merged_renders_placeholder(tmp_path):
    """t3_only rows present, no live_merged -> placeholder + 5d.5
    'no live_merged' note."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        t3_decisions=[_tick(0, "Buy", tier_provenance="t3_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No paired T1 / T3 decisions" in text
    assert "No live_merged decisions in this run" in text


def test_5d_full_agreement_renders_100_percent(tmp_path):
    """Two paired Buys -> agreement rate 100% and matrix (Buy,Buy)=2."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[
            _tick(0, "Buy", tier_provenance="t1_only"),
            _tick(5, "Buy", ticker="MSFT", tier_provenance="t1_only"),
        ],
        t3_decisions=[
            _tick(0, "Buy", tier_provenance="t3_only"),
            _tick(5, "Buy", ticker="MSFT", tier_provenance="t3_only"),
        ],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Agreement rate:** 100.0% (2/2)" in text
    # Diagonal cell (Buy,Buy) = 2; off-diagonal cells = 0.
    # The Buy row in the matrix is "**Buy** | 2 | 0 | 0".
    assert "| **Buy** | 2 | 0 | 0 |" in text
    assert "No T1↔T3 disagreements in this run." in text


def test_5d_full_disagreement_renders_0_percent(tmp_path):
    """T1=Buy paired with T3=Sell on the same key -> 0% agreement,
    matrix (Buy,Sell)=1."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
        t3_decisions=[_tick(0, "Sell", tier_provenance="t3_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Agreement rate:** 0.0% (0/1)" in text
    assert "| **Buy** | 0 | 1 | 0 |" in text
    assert "Disagreements:** 1" in text


def test_5d_mixed_matrix_cell_counts(tmp_path):
    """4 paired decisions across 4 different (T1,T3) cells.

    Pairs: (AAPL Buy/Buy), (MSFT Sell/Hold), (NVDA Hold/Buy),
    (GOOG Buy/Sell). Agreement = 1/4 = 25%.
    """
    db = tmp_path / "r.db"
    pairs = [
        ("AAPL", "Buy", "Buy"),
        ("MSFT", "Sell", "Hold"),
        ("NVDA", "Hold", "Buy"),
        ("GOOG", "Buy", "Sell"),
    ]
    decisions = [
        _tick(i * 5, t1, ticker=t, tier_provenance="t1_only")
        for i, (t, t1, _) in enumerate(pairs)
    ]
    t3 = [
        _tick(i * 5, t3a, ticker=t, tier_provenance="t3_only")
        for i, (t, _, t3a) in enumerate(pairs)
    ]
    rid = _run_with(db, decisions=decisions, t3_decisions=t3)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Agreement rate:** 25.0% (1/4)" in text
    # (Buy, Buy)=1, (Buy, Sell)=1, (Buy, Hold)=0 -> Buy row "1 | 1 | 0"
    assert "| **Buy** | 1 | 1 | 0 |" in text
    # (Sell, *) -> 0 | 0 | 1
    assert "| **Sell** | 0 | 0 | 1 |" in text
    # (Hold, *) -> 1 | 0 | 0
    assert "| **Hold** | 1 | 0 | 0 |" in text


def test_5d_failure_markers_excluded_from_matrix(tmp_path):
    """T1 row with setup_label='schema_invalid_t1' paired with a normal T3
    row -> pair excluded from the matrix, counted in the failures tally."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[
            _tick(0, "Hold", setup_label="schema_invalid_t1",
                  tier_provenance="t1_failed"),
        ],
        t3_decisions=[_tick(0, "Buy", tier_provenance="t3_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    # No valid pair -> placeholder + failure tally.
    assert "No paired T1 / T3 decisions" in text
    assert "T1 failures:** 1" in text


def test_5d_t3_failure_excluded_from_matrix(tmp_path):
    """T3 row with tier_provenance='t3_failed' paired with a normal T1
    row -> pair excluded, T3 failure counted."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
        t3_decisions=[
            _tick(0, "Hold", setup_label="api_failure_t3",
                  tier_provenance="t3_failed"),
        ],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No paired T1 / T3 decisions" in text
    assert "T3 failures:** 1" in text


def test_5d_disagreement_with_fill_renders_mean_pl(tmp_path):
    """Disagreement on AAPL; T1's decision_id=1 has a closed-position
    fill with realized_pl=+50. Mean P&L line shows $50.00."""
    db = tmp_path / "r.db"
    pf = SimulatedPortfolio(starting_cash=100_000.0)
    # Position references decision_id=1 (the first live_merged row).
    pf.closed_positions.append(_closed_position(
        decision_id=1, exit_price=105.0,  # +50 on 10 shares
    ))
    conn = init_replay_db(db)
    try:
        rid = start_run(conn, config=_config())
        write_day_results(
            conn, run_id=rid,
            day_result=_day_result(
                decisions=[_tick(5, "Buy", tier_provenance="t1_only")],
                t3_decisions=[_tick(5, "Sell", tier_provenance="t3_only")],
            ),
            llm_portfolio=pf,
        )
        complete_run(conn, run_id=rid)
    finally:
        conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Disagreements:** 1" in text
    assert "Disagreements with fills:** 1" in text
    assert "Mean T1-side realized P&L on disagreements:** $50.00" in text


def test_5d_disagreement_without_fill_renders_no_fills_note(tmp_path):
    """Disagreement but no fills on T1 side -> 'no fills attached' note."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
        t3_decisions=[_tick(0, "Sell", tier_provenance="t3_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "Disagreements:** 1" in text
    assert "Disagreements with fills:** 0" in text
    assert "No fills attached to disagreement decisions" in text


def test_5d_confidence_band_split(tmp_path):
    """4 pairs: 2 high-conf (75+) all agree, 2 low-conf (<75) all disagree.

    High band agreement = 100%, low band = 0%.
    """
    db = tmp_path / "r.db"
    decisions = [
        _tick(0, "Buy", ticker="A", confidence=80, tier_provenance="t1_only"),
        _tick(5, "Buy", ticker="B", confidence=90, tier_provenance="t1_only"),
        _tick(10, "Buy", ticker="C", confidence=40, tier_provenance="t1_only"),
        _tick(15, "Buy", ticker="D", confidence=50, tier_provenance="t1_only"),
    ]
    t3 = [
        _tick(0, "Buy", ticker="A", tier_provenance="t3_only"),
        _tick(5, "Buy", ticker="B", tier_provenance="t3_only"),
        _tick(10, "Sell", ticker="C", tier_provenance="t3_only"),
        _tick(15, "Sell", ticker="D", tier_provenance="t3_only"),
    ]
    rid = _run_with(db, decisions=decisions, t3_decisions=t3)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    # Section header includes the threshold.
    assert "confidence band (threshold = 75)" in text
    # High band: 2 pairs, 100% agreement.
    assert "| High (≥75) | 2 | 100.0% |" in text
    # Low band: 2 pairs, 0% agreement.
    assert "| Low (<75) | 2 | 0.0% |" in text


def test_5d_provenance_counts_table_renders_present_values(tmp_path):
    """Mix of tier_provenance values on live_merged side -> rows for
    each value rendered in the counts table."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[
            _tick(0, "Buy", ticker="A", tier_provenance="t1_only"),
            _tick(5, "Buy", ticker="B", tier_provenance="t1_t2_agree"),
            _tick(10, "Hold", ticker="C", tier_provenance="t1_t2_disagree"),
        ],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "`t1_only`" in text
    assert "`t1_t2_agree`" in text
    assert "`t1_t2_disagree`" in text


def test_5d_provenance_counts_handles_null(tmp_path):
    """live_merged rows with tier_provenance=None render as '*(none)*'."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance=None)],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    # The table renders the literal markdown string "*(none)*" inside
    # the cell. The Python source is "*(none)*".
    assert "*(none)*" in text


def test_5d_multi_run_isolation(tmp_path):
    """Pairs from run B do not leak into run A's report."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    try:
        ra = start_run(conn, config=_config(llm_prompt_version="va"))
        rb = start_run(conn, config=_config(llm_prompt_version="vb"))
        # Run A: T1=Buy, T3=Buy at minute 0 (agreement).
        write_day_results(
            conn, run_id=ra,
            day_result=_day_result(
                decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
                t3_decisions=[_tick(0, "Buy", tier_provenance="t3_only")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        # Run B: T1=Buy, T3=Sell (disagreement).
        write_day_results(
            conn, run_id=rb,
            day_result=_day_result(
                decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
                t3_decisions=[_tick(0, "Sell", tier_provenance="t3_only")],
            ),
            llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
        )
        complete_run(conn, run_id=ra)
        complete_run(conn, run_id=rb)
    finally:
        conn.close()
    out_a = tmp_path / "a.md"
    out_b = tmp_path / "b.md"
    generate_report(db_path=db, run_id=ra, output_path=out_a)
    generate_report(db_path=db, run_id=rb, output_path=out_b)
    text_a = out_a.read_text(encoding="utf-8")
    text_b = out_b.read_text(encoding="utf-8")
    assert "Agreement rate:** 100.0% (1/1)" in text_a
    assert "Agreement rate:** 0.0% (0/1)" in text_b


def test_5d_inner_join_skips_unpaired_keys(tmp_path):
    """Three tickers: A has both T1+T3 (paired), B has T1-only (no T3),
    C has T3-only (no T1). Only A contributes to the pair denominator."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[
            _tick(0, "Buy", ticker="A", tier_provenance="t1_only"),
            _tick(5, "Buy", ticker="B", tier_provenance="t1_only"),
        ],
        t3_decisions=[
            _tick(0, "Buy", ticker="A", tier_provenance="t3_only"),
            _tick(10, "Sell", ticker="C", tier_provenance="t3_only"),
        ],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    # Only one pair (A,A); agreement = 100%.
    assert "Paired T1↔T3 decisions:** 1" in text
    assert "Agreement rate:** 100.0% (1/1)" in text


def test_5d_renders_before_section_6(tmp_path):
    """Smoke: 5d header appears before section 6 header."""
    db = tmp_path / "r.db"
    rid = _run_with(
        db,
        decisions=[_tick(0, "Buy", tier_provenance="t1_only")],
        t3_decisions=[_tick(0, "Buy", tier_provenance="t3_only")],
    )
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    idx_5d = text.index("## 5d. Tier agreement")
    idx_6 = text.index("## 6. Failure modes")
    assert idx_5d < idx_6


# ===========================================================================
# Section 6: failure modes
# ===========================================================================


def test_section_6_lists_rejections_by_reason(tmp_path):
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
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
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "position_cap_exceeded" in text


def test_section_6_tier_failure_patterns_zero(tmp_path):
    """No schema_invalid_t1 / api_failure_t1 / t1_unexpected setup_labels
    -> 'no tier-failure setup_labels detected'."""
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No tier-failure setup_labels detected" in text


def test_section_6_tier_failure_pattern_detected(tmp_path):
    """A live_merged decision with setup_label='schema_invalid_t1'
    surfaces in section 6."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(0, "Hold", setup_label="schema_invalid_t1")],
        ),
        llm_portfolio=SimulatedPortfolio(starting_cash=100_000.0),
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "schema_invalid_t1" in text


# ===========================================================================
# Section 7: top decisions
# ===========================================================================


def test_section_7_top_wins_ordered(tmp_path):
    """Two closed positions; the +50 should appear before the +20 in Top 5 wins."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    pf = SimulatedPortfolio(starting_cash=100_000.0)
    pf.closed_positions.append(_closed_position(
        decision_id=1, exit_price=105.0,  # +50
    ))
    pf.closed_positions.append(_closed_position(
        decision_id=2, exit_price=102.0,  # +20
        ticker="MSFT",
    ))
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(
            decisions=[_tick(5, "Buy"), _tick(10, "Buy", ticker="MSFT")],
        ),
        llm_portfolio=pf,
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    # AAPL (+$50) must appear before MSFT (+$20) in the Top wins table.
    wins_section = text[text.index("Top 5 wins"):text.index("Top 5 losses")]
    assert wins_section.index("AAPL") < wins_section.index("MSFT")


def test_section_7_top_divergences_requires_both_portfolios(tmp_path):
    """LLM-only run -> 'No diverged decisions' note in top-divergences."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    rid = start_run(conn, config=_config())
    pf = SimulatedPortfolio(starting_cash=100_000.0)
    pf.closed_positions.append(_closed_position(decision_id=1, exit_price=105.0))
    write_day_results(
        conn, run_id=rid,
        day_result=_day_result(decisions=[_tick(5, "Buy")]),
        llm_portfolio=pf,
    )
    complete_run(conn, run_id=rid)
    conn.close()
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    assert "No diverged decisions" in text


# ===========================================================================
# Robustness
# ===========================================================================


def test_multi_run_isolation(tmp_path):
    """Two runs in the same DB: each report sees only its own rows."""
    db = tmp_path / "r.db"
    conn = init_replay_db(db)
    r1 = start_run(conn, config=_config(llm_prompt_version="run1"))
    r2 = start_run(conn, config=_config(llm_prompt_version="run2"))
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
    complete_run(conn, run_id=r1)
    complete_run(conn, run_id=r2)
    conn.close()
    out1 = tmp_path / "r1.md"
    out2 = tmp_path / "r2.md"
    generate_report(db_path=db, run_id=r1, output_path=out1)
    generate_report(db_path=db, run_id=r2, output_path=out2)
    t1 = out1.read_text(encoding="utf-8")
    t2 = out2.read_text(encoding="utf-8")
    assert "run1" in t1 and "run1" not in t2
    assert "run2" in t2 and "run2" not in t1


def test_report_is_valid_markdown_structure(tmp_path):
    """Smoke: report has expected top-level headings in order."""
    db = tmp_path / "r.db"
    rid = _seed_run(db)
    out = tmp_path / "out.md"
    generate_report(db_path=db, run_id=rid, output_path=out)
    text = out.read_text(encoding="utf-8")
    expected = [
        "# Replay Comparison Report",
        "## 1. Run metadata",
        "## 2. Decision summary",
        "## 3. Portfolio performance",
        "## 4. Divergence analysis",
        "## 5. LLM-specific quality metrics",
        "## 5b. Regime-stratified performance",
        "## 5c. Crash-period replay",
        "## 5d. Tier agreement",
        "## 6. Failure modes",
        "## 7. Top decisions",
    ]
    last = -1
    for marker in expected:
        idx = text.find(marker)
        assert idx > last, f"missing or out-of-order: {marker!r}"
        last = idx
