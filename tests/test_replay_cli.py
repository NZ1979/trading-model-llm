"""Tests for the M2.2 sub-task #19 wiring in scripts/replay_with_llm.py.

The M2.1 scaffolding (argparse, args_to_config, config_to_printable)
is already covered in tests/test_replay_scaffolding.py. This file
focuses on the additions sub-task #19 brought:

- New flags: --base-portfolio, --base-require-walls-for-pullback,
  --skip-report.
- _replay_config_to_llm_config_dict bridge to factory.build_tier_clients.
- _build_summary_json aggregation.
- _setup_logging Rule 22 invariant.
- main() orchestration with mocks (happy path, --skip-report,
  --base-portfolio toggle, FileNotFoundError -> exit 2, ValueError
  -> exit 3, --echo-only bypass).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig
from data.replay.driver import DayRunResult
from data.replay_cache import CachedLLMClient
from scripts import replay_with_llm
from scripts.replay_with_llm import (
    _build_summary_json,
    _replay_config_to_llm_config_dict,
    _setup_logging,
    args_to_config,
    build_arg_parser,
    main,
)
from strategy.llm.signal_engine import TierClients


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _argv(**overrides: str) -> list[str]:
    """Build minimum CLI argv with overrides."""
    base = {
        "--start": "2026-04-15",
        "--end": "2026-04-15",
        "--tickers": "AAPL",
        "--prompt-version": "v-test",
    }
    base.update(overrides)
    argv: list[str] = []
    for k, v in base.items():
        argv.extend([k, v])
    return argv


# ===========================================================================
# New CLI flags
# ===========================================================================


def test_base_portfolio_flag_parses():
    parser = build_arg_parser()
    args = parser.parse_args(_argv() + ["--base-portfolio"])
    assert args.base_portfolio is True


def test_base_portfolio_flag_default_off():
    parser = build_arg_parser()
    args = parser.parse_args(_argv())
    assert args.base_portfolio is False


def test_base_require_walls_for_pullback_flag_default_false():
    """Default mirrors live's dormant-walls reality."""
    parser = build_arg_parser()
    args = parser.parse_args(_argv())
    cfg = args_to_config(args)
    assert cfg.base_require_walls_for_pullback is False


def test_base_require_walls_for_pullback_flag_on():
    parser = build_arg_parser()
    args = parser.parse_args(
        _argv() + ["--base-require-walls-for-pullback"]
    )
    cfg = args_to_config(args)
    assert cfg.base_require_walls_for_pullback is True


def test_skip_report_flag_parses():
    parser = build_arg_parser()
    args = parser.parse_args(_argv() + ["--skip-report"])
    assert args.skip_report is True


# ===========================================================================
# _replay_config_to_llm_config_dict
# ===========================================================================


def _cfg(**overrides) -> ReplayConfig:
    kwargs = dict(
        start_date=date(2026, 4, 15),
        end_date=date(2026, 4, 15),
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def test_llm_config_dict_roundtrips_t1_fields():
    cfg = _cfg(t1_backend="haiku_stand_in", t1_model_id="claude-haiku-4-5")
    d = _replay_config_to_llm_config_dict(cfg)
    assert d["enabled"] is True
    assert d["t1"]["backend"] == "haiku_stand_in"
    assert d["t1"]["model_id"] == "claude-haiku-4-5"


def test_llm_config_dict_t2_t3_enabled_flags():
    cfg = _cfg(t2_enabled=False, t3_enabled=False)
    d = _replay_config_to_llm_config_dict(cfg)
    assert d["t2"]["enabled"] is False
    assert d["t3"]["enabled"] is False
    # backend hardcoded to anthropic for T2/T3 regardless of enabled flag
    assert d["t2"]["backend"] == "anthropic"
    assert d["t3"]["backend"] == "anthropic"


# ===========================================================================
# _build_summary_json
# ===========================================================================


def test_build_summary_json_aggregates_counts():
    results = [
        DayRunResult(
            trading_date=date(2026, 4, 15),
            decisions=[MagicMock(), MagicMock(), MagicMock()],
            base_decisions=[MagicMock()],
            fills=(MagicMock(),),
            base_fills=(),
            rejections=(MagicMock(), MagicMock()),
            base_rejections=(),
            t2_escalations_used=3,
        ),
        DayRunResult(
            trading_date=date(2026, 4, 16),
            decisions=[MagicMock(), MagicMock()],
            base_decisions=[MagicMock(), MagicMock()],
            fills=(MagicMock(), MagicMock()),
            base_fills=(MagicMock(),),
            rejections=(),
            base_rejections=(MagicMock(),),
            t2_escalations_used=5,
        ),
        DayRunResult(  # skipped day
            trading_date=date(2026, 4, 17),
            skipped=True,
        ),
    ]
    # Tier clients with no caches (T3 None, T1/T2 raw not wrapped)
    clients = TierClients(t1=MagicMock(), t2=None, t3=None)
    summary_str = _build_summary_json(results, clients)
    data = json.loads(summary_str)
    assert data["n_days_total"] == 3
    assert data["n_days_skipped"] == 1
    assert data["n_llm_decisions"] == 5
    assert data["n_base_decisions"] == 3
    assert data["n_llm_fills"] == 3
    assert data["n_base_fills"] == 1
    assert data["n_llm_rejections"] == 2
    assert data["n_base_rejections"] == 1
    assert data["n_t2_escalations"] == 8
    # No cache stats because T1 isn't a CachedLLMClient
    assert "cache_t1_hits" not in data


def test_build_summary_json_includes_cache_stats_when_wrapped():
    """T1 wrapped in CachedLLMClient -> hits/misses appear in summary."""
    inner = MagicMock()
    inner.backend = "haiku_stand_in"
    inner.model_id = "claude-haiku-4-5"
    cached_t1 = CachedLLMClient(
        inner, cache_dir=Path("/tmp/x"), prompt_version="v",
    )
    cached_t1.hits = 17
    cached_t1.misses = 3
    clients = TierClients(t1=cached_t1, t2=None, t3=None)
    summary = json.loads(_build_summary_json([], clients))
    assert summary["cache_t1_hits"] == 17
    assert summary["cache_t1_misses"] == 3


# ===========================================================================
# _setup_logging (Rule 22)
# ===========================================================================


def test_setup_logging_silences_httpx_anthropic():
    """Rule 22: httpx / anthropic / urllib3 must be at WARNING to prevent
    Polygon-API-key-in-URL leaks into stderr."""
    _setup_logging()
    for name in ("httpx", "httpcore", "aiohttp", "anthropic", "urllib3"):
        assert logging.getLogger(name).level >= logging.WARNING, \
            f"{name} logger must be WARNING+ per Rule 22"


# ===========================================================================
# main() orchestration with mocks
# ===========================================================================


@pytest.fixture
def cli_mocks(monkeypatch):
    """Patch all heavy dependencies main() invokes through _run()."""
    sentiment_conn = sqlite3.connect(":memory:")
    persistence_conn = sqlite3.connect(":memory:")

    # Build a minimal TierClients to return from build_tier_clients
    fake_t1 = MagicMock()
    fake_t1.backend = "haiku_stand_in"
    fake_t1.model_id = "claude-haiku-4-5"
    fake_clients = TierClients(t1=fake_t1, t2=None, t3=None)

    open_fixture_mock = MagicMock(return_value=sentiment_conn)
    build_tier_clients_mock = MagicMock(return_value=fake_clients)
    init_replay_db_mock = MagicMock(return_value=persistence_conn)
    start_run_mock = MagicMock(return_value=42)
    complete_run_mock = MagicMock()
    run_replay_mock = AsyncMock(return_value=[])
    generate_report_mock = MagicMock(return_value=Path("/tmp/report.md"))

    monkeypatch.setattr(replay_with_llm, "open_fixture", open_fixture_mock)
    monkeypatch.setattr(
        replay_with_llm, "build_tier_clients", build_tier_clients_mock,
    )
    monkeypatch.setattr(replay_with_llm, "init_replay_db", init_replay_db_mock)
    monkeypatch.setattr(replay_with_llm, "start_run", start_run_mock)
    monkeypatch.setattr(replay_with_llm, "complete_run", complete_run_mock)
    monkeypatch.setattr(replay_with_llm, "run_replay", run_replay_mock)
    monkeypatch.setattr(
        replay_with_llm, "generate_report", generate_report_mock,
    )
    return {
        "open_fixture": open_fixture_mock,
        "build_tier_clients": build_tier_clients_mock,
        "init_replay_db": init_replay_db_mock,
        "start_run": start_run_mock,
        "complete_run": complete_run_mock,
        "run_replay": run_replay_mock,
        "generate_report": generate_report_mock,
    }


def test_main_happy_path_returns_zero(cli_mocks, capsys, tmp_path):
    rc = main(_argv() + ["--output-dir", str(tmp_path)])
    assert rc == 0
    # Report path was printed to stdout (path separator is platform-
    # dependent on Windows vs POSIX, so check for the filename only).
    captured = capsys.readouterr()
    assert "report.md" in captured.out
    # Verify orchestration order
    assert cli_mocks["open_fixture"].called
    assert cli_mocks["build_tier_clients"].called
    assert cli_mocks["init_replay_db"].called
    assert cli_mocks["start_run"].called
    assert cli_mocks["run_replay"].await_count == 1
    assert cli_mocks["complete_run"].called
    assert cli_mocks["generate_report"].called


def test_main_base_portfolio_flag_threads_to_run_replay(cli_mocks, tmp_path):
    main(_argv() + ["--output-dir", str(tmp_path), "--base-portfolio"])
    kwargs = cli_mocks["run_replay"].await_args.kwargs
    assert kwargs["base_portfolio"] is not None
    assert kwargs["base_portfolio"].starting_cash == 100_000.0


def test_main_no_base_portfolio_default_passes_none(cli_mocks, tmp_path):
    main(_argv() + ["--output-dir", str(tmp_path)])
    kwargs = cli_mocks["run_replay"].await_args.kwargs
    assert kwargs["base_portfolio"] is None


def test_main_skip_report_skips_generate_report(cli_mocks, tmp_path, capsys):
    main(_argv() + ["--output-dir", str(tmp_path), "--skip-report"])
    assert not cli_mocks["generate_report"].called
    captured = capsys.readouterr()
    # The skip banner lands on stderr, not stdout
    assert "report skipped" in captured.err.lower()


def test_main_file_not_found_returns_2(cli_mocks, tmp_path, capsys):
    cli_mocks["open_fixture"].side_effect = FileNotFoundError(
        "sentiment fixture missing"
    )
    rc = main(_argv() + ["--output-dir", str(tmp_path)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "sentiment fixture missing" in captured.err


def test_main_value_error_returns_3(cli_mocks, tmp_path, capsys):
    cli_mocks["run_replay"].side_effect = ValueError(
        "bad config somewhere"
    )
    rc = main(_argv() + ["--output-dir", str(tmp_path)])
    assert rc == 3
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "bad config" in captured.err


def test_main_echo_only_bypasses_run(cli_mocks, capsys):
    """--echo-only prints JSON + exits 0 WITHOUT touching _run()."""
    rc = main(_argv() + ["--echo-only"])
    assert rc == 0
    # None of the orchestration helpers should be invoked
    assert not cli_mocks["open_fixture"].called
    assert not cli_mocks["build_tier_clients"].called
    assert not cli_mocks["run_replay"].called
    captured = capsys.readouterr()
    # JSON config printed on stdout
    assert "start_date" in captured.out
    assert "v-test" in captured.out
