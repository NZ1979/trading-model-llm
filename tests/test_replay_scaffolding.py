"""Tests for the M2.1 replay-harness scaffolding.

Covers:
  - ReplayConfig defaults + __post_init__ validation
  - cache_key determinism + isolation across tier/backend/prompt_version
  - SimulatedFill construction + apply_slippage math
  - simulate_fill branching on fill_at, missing next-bar handling
  - SimulatedPortfolio: entry/exit bookkeeping, mark-to-market math,
    short-side equity math, max-drawdown tracking, stop-loss detection
  - CLI argparse round-trip and date/ticker parsing
  - Loader stubs raise NotImplementedError with expected docstrings

These tests run from the repo root: `pytest tests/test_replay_scaffolding.py -v`.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig, cache_key
from data.replay import (
    historical_bars,
    historical_sentiment,
    market_context,
)
from sim.fills import SimulatedFill, apply_slippage, simulate_fill
from sim.portfolio import SimulatedPortfolio
from scripts.replay_with_llm import (
    args_to_config,
    build_arg_parser,
    config_to_printable,
)


# ---------------------------------------------------------------------------
# ReplayConfig
# ---------------------------------------------------------------------------


def _min_cfg(**overrides) -> ReplayConfig:
    """Build a minimal valid ReplayConfig with overrides applied."""
    kwargs = dict(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        tickers=("AAPL", "NVDA"),
        llm_prompt_version="v0.1-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def test_replay_config_defaults_apply_when_only_required_given():
    cfg = _min_cfg()
    assert cfg.t1_backend == "qwen_local"
    assert cfg.t1_model_id == "qwen3.6-27b-instruct-q4"
    assert cfg.t2_enabled is True
    assert cfg.t3_enabled is True
    assert cfg.t3_sample_rate == 1.0
    assert cfg.t3_max_dollars_per_run == 500.0
    assert cfg.starting_cash == 100_000.0
    assert cfg.slippage_bps == 5.0
    assert cfg.fill_at == "next_bar_open"
    assert cfg.news_lag_seconds == 30
    assert cfg.max_candidates_per_tick == 30
    assert cfg.sentiment_fixture_path == Path("data/replay/fixtures/sentiment.sqlite")


def test_replay_config_end_before_start_raises():
    with pytest.raises(ValueError, match="end_date .* before start_date"):
        _min_cfg(start_date=date(2026, 5, 1), end_date=date(2026, 4, 30))


def test_replay_config_t3_sample_rate_out_of_range_raises():
    with pytest.raises(ValueError, match="t3_sample_rate"):
        _min_cfg(t3_sample_rate=1.5)
    with pytest.raises(ValueError, match="t3_sample_rate"):
        _min_cfg(t3_sample_rate=-0.1)


def test_replay_config_bad_fill_at_raises():
    with pytest.raises(ValueError, match="fill_at"):
        # bypass Literal type check at construction time
        _min_cfg(fill_at="midpoint")  # type: ignore[arg-type]


def test_replay_config_empty_prompt_version_raises():
    with pytest.raises(ValueError, match="llm_prompt_version"):
        _min_cfg(llm_prompt_version="")


def test_replay_config_empty_tickers_tuple_raises():
    with pytest.raises(ValueError, match="tickers tuple"):
        _min_cfg(tickers=())


def test_replay_config_watchlist_literal_accepted():
    cfg = _min_cfg(tickers="watchlist")
    assert cfg.tickers == "watchlist"
    with pytest.raises(ValueError, match="watchlist"):
        cfg.tickers_tuple


def test_replay_config_negative_slippage_raises():
    with pytest.raises(ValueError, match="slippage_bps"):
        _min_cfg(slippage_bps=-1.0)


def test_replay_config_max_position_pct_bounds():
    with pytest.raises(ValueError, match="max_position_pct"):
        _min_cfg(max_position_pct=0)
    with pytest.raises(ValueError, match="max_position_pct"):
        _min_cfg(max_position_pct=101)


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------


def test_cache_key_deterministic_for_same_inputs():
    cd = Path(".replay_cache")
    a = cache_key(prompt="abc", prompt_version="v1", backend="qwen_local",
                  model_id="qwen3.6-27b-instruct-q4", cache_dir=cd)
    b = cache_key(prompt="abc", prompt_version="v1", backend="qwen_local",
                  model_id="qwen3.6-27b-instruct-q4", cache_dir=cd)
    assert a == b


def test_cache_key_changes_when_prompt_version_bumps():
    cd = Path(".replay_cache")
    a = cache_key(prompt="abc", prompt_version="v1", backend="qwen_local",
                  model_id="m", cache_dir=cd)
    b = cache_key(prompt="abc", prompt_version="v2", backend="qwen_local",
                  model_id="m", cache_dir=cd)
    assert a != b


def test_cache_key_isolates_tiers_by_backend_and_model():
    cd = Path(".replay_cache")
    a = cache_key(prompt="abc", prompt_version="v1", backend="qwen_local",
                  model_id="m", cache_dir=cd)
    b = cache_key(prompt="abc", prompt_version="v1", backend="anthropic",
                  model_id="m", cache_dir=cd)
    c = cache_key(prompt="abc", prompt_version="v1", backend="anthropic",
                  model_id="claude-opus-4-6", cache_dir=cd)
    # Different backends produce different parent dirs.
    assert a.parts[:2] != b.parts[:2]
    # Different models within same backend produce different parent dirs.
    assert b.parents[0] != c.parents[0]


def test_cache_key_handles_slash_in_model_id():
    cd = Path(".replay_cache")
    p = cache_key(prompt="abc", prompt_version="v1", backend="local",
                  model_id="meta-llama/Llama-3.3-70B", cache_dir=cd)
    # No path separator from the model name should reach the filename's
    # parent component as a real directory split.
    assert "meta-llama_Llama-3.3-70B" in p.parts


def test_cache_key_rejects_empty_fields():
    cd = Path(".replay_cache")
    with pytest.raises(ValueError):
        cache_key(prompt="x", prompt_version="", backend="b", model_id="m",
                  cache_dir=cd)
    with pytest.raises(ValueError):
        cache_key(prompt="x", prompt_version="v1", backend="", model_id="m",
                  cache_dir=cd)
    with pytest.raises(ValueError):
        cache_key(prompt="x", prompt_version="v1", backend="b", model_id="",
                  cache_dir=cd)


# ---------------------------------------------------------------------------
# Fills: apply_slippage + simulate_fill
# ---------------------------------------------------------------------------


def test_apply_slippage_buy_is_above_reference():
    out = apply_slippage(100.0, "buy", 5.0)
    assert out == pytest.approx(100.05)


def test_apply_slippage_sell_is_below_reference():
    out = apply_slippage(100.0, "sell", 5.0)
    assert out == pytest.approx(99.95)


def test_apply_slippage_zero_is_passthrough():
    assert apply_slippage(100.0, "buy", 0.0) == 100.0
    assert apply_slippage(100.0, "sell", 0.0) == 100.0


def test_apply_slippage_rejects_bad_side():
    with pytest.raises(ValueError, match="side"):
        apply_slippage(100.0, "midpoint", 5.0)


def test_apply_slippage_rejects_negative_bps():
    with pytest.raises(ValueError, match="slippage_bps"):
        apply_slippage(100.0, "buy", -1.0)


def _ts(h: int, m: int) -> datetime:
    return datetime(2026, 4, 15, h, m, tzinfo=timezone.utc)


def test_simulate_fill_next_bar_open_buy_path():
    fill = simulate_fill(
        ticker="AAPL", side="buy", qty=10, decision_id=1,
        fill_at="next_bar_open",
        current_bar_close=100.0,
        next_bar_open=101.0,
        next_bar_timestamp=_ts(10, 5),
        current_bar_timestamp=_ts(10, 0),
        stop_price=98.5,
        slippage_bps=10.0,
    )
    assert fill is not None
    assert fill.ticker == "AAPL"
    assert fill.side == "buy"
    assert fill.qty == 10
    assert fill.fill_price == pytest.approx(101.0 * 1.001)
    assert fill.fill_timestamp == _ts(10, 5)
    assert fill.stop_price == 98.5


def test_simulate_fill_current_close_sell_path():
    fill = simulate_fill(
        ticker="NVDA", side="sell", qty=5, decision_id=2,
        fill_at="current_close",
        current_bar_close=200.0,
        next_bar_open=None,
        next_bar_timestamp=None,
        current_bar_timestamp=_ts(11, 30),
        stop_price=205.0,
        slippage_bps=20.0,
    )
    assert fill is not None
    assert fill.fill_price == pytest.approx(200.0 * 0.998)
    assert fill.fill_timestamp == _ts(11, 30)


def test_simulate_fill_returns_none_when_next_bar_missing():
    fill = simulate_fill(
        ticker="AAPL", side="buy", qty=10, decision_id=1,
        fill_at="next_bar_open",
        current_bar_close=100.0,
        next_bar_open=None,
        next_bar_timestamp=None,
        current_bar_timestamp=_ts(15, 55),
        stop_price=98.0,
        slippage_bps=5.0,
    )
    assert fill is None


def test_simulate_fill_rejects_zero_qty():
    with pytest.raises(ValueError, match="qty"):
        simulate_fill(
            ticker="AAPL", side="buy", qty=0, decision_id=1,
            fill_at="current_close",
            current_bar_close=100.0,
            next_bar_open=None,
            next_bar_timestamp=None,
            current_bar_timestamp=_ts(10, 0),
            stop_price=98.0,
            slippage_bps=5.0,
        )


# ---------------------------------------------------------------------------
# SimulatedPortfolio
# ---------------------------------------------------------------------------


def _fill(ticker: str, side: str, qty: int, price: float,
          stop: float, ts: datetime, did: int = 1) -> SimulatedFill:
    return SimulatedFill(
        ticker=ticker, side=side, qty=qty,  # type: ignore[arg-type]
        fill_price=price, fill_timestamp=ts,
        stop_price=stop, decision_id=did,
    )


def test_portfolio_starts_with_cash_equal_to_starting_cash():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    assert p.cash == 100_000.0
    assert p.peak_equity == 100_000.0
    assert p.realized_pl_total == 0.0
    assert p.max_drawdown == 0.0
    assert not p.has_position("AAPL")


def test_portfolio_record_entry_long_deducts_cash():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    assert p.cash == pytest.approx(100_000.0 - 15_000.0)
    assert p.has_position("AAPL")
    pos = p.get_position("AAPL")
    assert pos is not None
    assert pos.qty == 100
    assert pos.entry_price == 150.0


def test_portfolio_record_entry_short_credits_cash():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "sell", 100, 150.0, 155.0, _ts(10, 0)))
    assert p.cash == pytest.approx(100_000.0 + 15_000.0)


def test_portfolio_long_winner_realized_pl():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    realized = p.record_exit("AAPL", exit_price=160.0,
                             exit_timestamp=_ts(11, 0), exit_reason="take_profit")
    # 100 shares * $10 profit = $1000
    assert realized == pytest.approx(1000.0)
    assert p.realized_pl_total == pytest.approx(1000.0)
    # cash back to: starting - entry_cost + exit_proceeds
    assert p.cash == pytest.approx(100_000.0 - 15_000.0 + 16_000.0)
    assert not p.has_position("AAPL")
    assert len(p.closed_positions) == 1


def test_portfolio_short_winner_realized_pl():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "sell", 100, 150.0, 155.0, _ts(10, 0)))
    realized = p.record_exit("AAPL", exit_price=140.0,
                             exit_timestamp=_ts(11, 0), exit_reason="take_profit")
    # short: (entry - exit) * qty = (150 - 140) * 100 = $1000
    assert realized == pytest.approx(1000.0)
    # cash: starting + entry_credit - cover_cost
    assert p.cash == pytest.approx(100_000.0 + 15_000.0 - 14_000.0)


def test_portfolio_double_entry_raises():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 10, 150.0, 145.0, _ts(10, 0)))
    with pytest.raises(ValueError, match="already has open position"):
        p.record_entry(_fill("AAPL", "buy", 10, 151.0, 145.0, _ts(10, 5)))


def test_portfolio_exit_without_position_raises():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    with pytest.raises(KeyError, match="no open position"):
        p.record_exit("AAPL", exit_price=100.0,
                      exit_timestamp=_ts(10, 0), exit_reason="stop_hit")


def test_portfolio_mark_to_market_long_unrealized_gain():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    point = p.mark_to_market(_ts(10, 5), {"AAPL": 160.0})
    # equity = cash + 100 * 160 = (100k - 15k) + 16k = 101k
    assert point.equity == pytest.approx(101_000.0)
    assert p.peak_equity == pytest.approx(101_000.0)
    assert p.max_drawdown == 0.0


def test_portfolio_mark_to_market_short_unrealized_gain():
    """Verifies the short-side equity formula.

    Bug history: original draft used qty*(2*entry - cp) which inflated
    short positions by 2*entry. The accounting is: cash holds the entry
    credit (+qty*entry); position is a liability at current_price; net
    contribution to equity is -qty*cp. Fixed before M2.1.f.

    Scenario: short 100 AAPL @ 150, mark at 140.
      - cash after entry: 100k + 15k = 115k
      - liability at 140: 14k
      - equity: 115k - 14k = 101k (unrealized +1k)
    """
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "sell", 100, 150.0, 155.0, _ts(10, 0)))
    point = p.mark_to_market(_ts(10, 5), {"AAPL": 140.0})
    assert point.equity == pytest.approx(101_000.0)
    # And at mark = entry price, equity should be flat.
    point2 = p.mark_to_market(_ts(10, 6), {"AAPL": 150.0})
    assert point2.equity == pytest.approx(100_000.0)
    # And mark above entry: short loses, equity < 100k.
    point3 = p.mark_to_market(_ts(10, 7), {"AAPL": 160.0})
    assert point3.equity == pytest.approx(99_000.0)


def test_portfolio_max_drawdown_tracked_across_marks():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    # equity at 160 -> 101k (peak)
    p.mark_to_market(_ts(10, 5), {"AAPL": 160.0})
    # equity at 140 -> cash + 14k = 85k + 14k = 99k -> drawdown 2k from peak
    p.mark_to_market(_ts(10, 10), {"AAPL": 140.0})
    assert p.peak_equity == pytest.approx(101_000.0)
    assert p.max_drawdown == pytest.approx(2_000.0)
    assert p.max_drawdown_at == _ts(10, 10)


def test_portfolio_check_stops_long_hit_below_stop():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    triggered = p.check_stops(_ts(10, 5),
                              bar_lows={"AAPL": 144.0},
                              bar_highs={"AAPL": 150.0})
    assert triggered == [("AAPL", 145.0)]


def test_portfolio_check_stops_short_hit_above_stop():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "sell", 100, 150.0, 155.0, _ts(10, 0)))
    triggered = p.check_stops(_ts(10, 5),
                              bar_lows={"AAPL": 150.0},
                              bar_highs={"AAPL": 156.0})
    assert triggered == [("AAPL", 155.0)]


def test_portfolio_check_stops_no_trigger_within_range():
    p = SimulatedPortfolio(starting_cash=100_000.0)
    p.record_entry(_fill("AAPL", "buy", 100, 150.0, 145.0, _ts(10, 0)))
    triggered = p.check_stops(_ts(10, 5),
                              bar_lows={"AAPL": 146.0},
                              bar_highs={"AAPL": 152.0})
    assert triggered == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _argv(**overrides) -> list[str]:
    """Build a baseline argv list with overrides applied."""
    base = {
        "--start": "2026-04-01",
        "--end": "2026-04-30",
        "--tickers": "AAPL,NVDA",
        "--prompt-version": "v0.1-test",
    }
    base.update(overrides)
    argv = []
    for k, v in base.items():
        argv.extend([k, v])
    return argv


def test_cli_parses_minimum_required_args():
    parser = build_arg_parser()
    args = parser.parse_args(_argv())
    cfg = args_to_config(args)
    assert cfg.start_date == date(2026, 4, 1)
    assert cfg.end_date == date(2026, 4, 30)
    assert cfg.tickers == ("AAPL", "NVDA")
    assert cfg.llm_prompt_version == "v0.1-test"
    # Defaults should round-trip from argparse.
    assert cfg.t1_backend == "qwen_local"
    assert cfg.t2_enabled is True
    assert cfg.t3_enabled is True


def test_cli_no_t2_enabled_toggles_off():
    parser = build_arg_parser()
    argv = _argv() + ["--no-t2-enabled"]
    args = parser.parse_args(argv)
    cfg = args_to_config(args)
    assert cfg.t2_enabled is False


def test_cli_no_t3_enabled_toggles_off():
    parser = build_arg_parser()
    argv = _argv() + ["--no-t3-enabled"]
    args = parser.parse_args(argv)
    cfg = args_to_config(args)
    assert cfg.t3_enabled is False


def test_cli_tickers_watchlist_literal():
    parser = build_arg_parser()
    argv = _argv()
    # Replace --tickers value with 'watchlist'
    idx = argv.index("--tickers")
    argv[idx + 1] = "watchlist"
    args = parser.parse_args(argv)
    cfg = args_to_config(args)
    assert cfg.tickers == "watchlist"


def test_cli_invalid_date_format_rejected():
    parser = build_arg_parser()
    argv = _argv(**{"--start": "04/01/2026"})
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_cli_config_to_printable_serializes_dates_and_paths():
    parser = build_arg_parser()
    args = parser.parse_args(_argv())
    cfg = args_to_config(args)
    d = config_to_printable(cfg)
    assert d["start_date"] == "2026-04-01"
    assert d["end_date"] == "2026-04-30"
    assert isinstance(d["sentiment_fixture_path"], str)
    assert d["sentiment_fixture_path"].endswith("sentiment.sqlite")
    assert isinstance(d["cache_dir"], str)


# ---------------------------------------------------------------------------
# Loader stubs raise NotImplementedError
# ---------------------------------------------------------------------------


def test_sentiment_loader_docstring_mentions_rule_26():
    """Sanity check: future maintainers must not delete the Rule 26 note."""
    doc = historical_sentiment.__doc__ or ""
    assert "Rule 26" in doc, "historical_sentiment module docstring must cite Rule 26"
    assert "fixture" in doc, "historical_sentiment must reference the fixture resolution"
