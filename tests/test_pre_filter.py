"""Tests for data/replay/pre_filter.py (M2.2 sub-task #11).

Covers:
  - Validation: naive tick_et raises ValueError
  - Empty cases: no tickers -> empty; no gate passes -> empty
  - Each gate in isolation: pm_rvol, gap_pct (positive and negative),
    news, currently_holding
  - Boundary cases: pm_rvol exactly at threshold included; just below
    excluded. |gap_pct| at threshold included; just below excluded.
  - PremarketContext None defaults to 0.0 for both quantitative gates
    -> ticker fails them but can still pass via news or holding
  - News gate respects config.pre_filter_news_lookback_hours (NOT the
    LLM context's 24h window) and config.news_lag_seconds
  - Multi-gate passes (ticker matches several gates) -> appears once
  - Cap at max_candidates_per_tick honored (50 survivors, cap 10 -> 10)
  - Cap preserves dict-insertion iteration order (first N survive)
  - Return type is tuple (not list)
  - failed_tickers in day_state ignored (iteration is over day_state.tickers)
  - currently_holding entry for ticker NOT in day_state silently ignored
  - Dict-insertion order preserved in output
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, '.')

from analysis.indicators import PremarketContext
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.historical_news import HistoricalNewsItem
from data.replay.pre_filter import pre_filter_candidates
from data.replay.ticker_metadata import TickerMetadata


ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides) -> ReplayConfig:
    kwargs = dict(
        start_date=date(2026, 4, 15),
        end_date=date(2026, 4, 15),
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _pm_ctx(
    *,
    ticker: str = "AAPL",
    pm_rvol: float = 0.0,
    gap_pct: float = 0.0,
) -> PremarketContext:
    return PremarketContext(
        ticker=ticker,
        prior_close=100.0,
        prior_high=101.0,
        prior_low=99.0,
        premarket_high=100.5,
        premarket_low=99.5,
        premarket_volume=100_000,
        premarket_rvol=pm_rvol,
        is_unusual_volume=False,
        gap_pct=gap_pct,
        gap_atr_ratio=0.5,
    )


def _ticker_state(
    *,
    ticker: str = "AAPL",
    pm_rvol: float = 0.0,
    gap_pct: float = 0.0,
    has_pm_ctx: bool = True,
    news_items: list[HistoricalNewsItem] | None = None,
) -> TickerDayState:
    pm_ctx = (
        _pm_ctx(ticker=ticker, pm_rvol=pm_rvol, gap_pct=gap_pct)
        if has_pm_ctx else None
    )
    return TickerDayState(
        ticker=ticker,
        minute_bars=pd.DataFrame(),
        daily_bars=pd.DataFrame(),
        daily_context=None,
        premarket_context=pm_ctx,
        ticker_metadata=TickerMetadata(
            ticker=ticker,
            sector="Information Technology",
            market_cap_bucket="mega",
            avg_daily_volume=50_000_000,
        ),
        news_items=news_items if news_items is not None else [],
        last_5_daily_closes=(),
    )


def _day_state(
    *,
    tickers: dict[str, TickerDayState] | None = None,
    failed_tickers: dict[str, str] | None = None,
) -> DayState:
    tx = tickers if tickers is not None else {"AAPL": _ticker_state()}
    return DayState(
        trading_date=date(2026, 4, 15),
        vix_level=None,
        market_regime_label="unknown",
        sentiment_lookup={},
        tickers=tx,
        failed_tickers=failed_tickers or {},
        has_earnings_today={t: False for t in tx},
        has_earnings_within_3d={t: False for t in tx},
    )


def _tick(h: int = 10, m: int = 0) -> datetime:
    return datetime(2026, 4, 15, h, m, tzinfo=ET)


def _news(
    *,
    ticker: str = "AAPL",
    ts_et: datetime | None = None,
    article_id: str = "a1",
) -> HistoricalNewsItem:
    return HistoricalNewsItem(
        ts_et=ts_et if ts_et is not None else _tick(9, 0),
        ticker=ticker,
        headline="h",
        source="polygon",
        polygon_article_id=article_id,
    )


# ===========================================================================
# Validation
# ===========================================================================


def test_naive_tick_et_raises():
    ds = _day_state()
    with pytest.raises(ValueError, match="tz-aware"):
        pre_filter_candidates(
            ds, datetime(2026, 4, 15, 10, 0), set(), _config()
        )


# ===========================================================================
# Empty cases
# ===========================================================================


def test_empty_tickers_returns_empty_tuple():
    ds = _day_state(tickers={})
    out = pre_filter_candidates(ds, _tick(), set(), _config())
    assert out == ()
    assert isinstance(out, tuple)


def test_no_gate_passes_returns_empty():
    """A ticker with zero rvol, zero gap, no news, not held -> filtered out."""
    ds = _day_state()
    out = pre_filter_candidates(ds, _tick(), set(), _config())
    assert out == ()


# ===========================================================================
# Single gate in isolation
# ===========================================================================


def test_pm_rvol_gate_passes_above_threshold():
    cfg = _config()  # pre_filter_min_pm_rvol default = 2.0
    ds = _day_state(
        tickers={"AAPL": _ticker_state(pm_rvol=2.5)}
    )
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL",)


def test_gap_pct_gate_passes_positive_gap():
    cfg = _config()  # pre_filter_min_gap_pct default = 1.0
    ds = _day_state(
        tickers={"AAPL": _ticker_state(gap_pct=2.5)}
    )
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL",)


def test_gap_pct_gate_passes_negative_gap():
    """A -3% gap is as actionable as +3%; |gap| satisfies the threshold."""
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(gap_pct=-3.0)}
    )
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL",)


def test_news_gate_passes_when_recent_news():
    """Item within pre_filter_news_lookback_hours and past the lag buffer."""
    tick = _tick()
    cfg = _config()  # default pre_filter_news_lookback_hours=2, news_lag_seconds=30
    news = [_news(ts_et=tick - timedelta(minutes=30))]
    ds = _day_state(
        tickers={"AAPL": _ticker_state(news_items=news)}
    )
    out = pre_filter_candidates(ds, tick, set(), cfg)
    assert out == ("AAPL",)


def test_currently_holding_gate_passes_held_ticker():
    """A held ticker passes even when no catalyst gate fires."""
    cfg = _config()
    ds = _day_state(tickers={"AAPL": _ticker_state()})
    out = pre_filter_candidates(ds, _tick(), {"AAPL"}, cfg)
    assert out == ("AAPL",)


# ===========================================================================
# Threshold boundary cases
# ===========================================================================


def test_pm_rvol_exactly_at_threshold_passes():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(pm_rvol=cfg.pre_filter_min_pm_rvol)}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ("AAPL",)


def test_pm_rvol_just_below_threshold_excluded():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(
            pm_rvol=cfg.pre_filter_min_pm_rvol - 0.01
        )}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ()


def test_abs_gap_pct_exactly_at_threshold_passes():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(gap_pct=cfg.pre_filter_min_gap_pct)}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ("AAPL",)


def test_abs_gap_pct_just_below_threshold_excluded():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(
            gap_pct=cfg.pre_filter_min_gap_pct - 0.01
        )}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ()


def test_negative_gap_pct_just_below_neg_threshold_excluded():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(
            gap_pct=-(cfg.pre_filter_min_gap_pct - 0.01)
        )}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ()


# ===========================================================================
# PremarketContext None
# ===========================================================================


def test_premarket_context_none_fails_quantitative_gates():
    """No PM data -> pm_rvol=0 and gap_pct=0, both gates fail."""
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(has_pm_ctx=False)}
    )
    assert pre_filter_candidates(ds, _tick(), set(), cfg) == ()


def test_premarket_context_none_but_news_still_passes():
    """News gate is independent of PM context."""
    cfg = _config()
    tick = _tick()
    news = [_news(ts_et=tick - timedelta(minutes=30))]
    ds = _day_state(
        tickers={"AAPL": _ticker_state(has_pm_ctx=False, news_items=news)}
    )
    assert pre_filter_candidates(ds, tick, set(), cfg) == ("AAPL",)


def test_premarket_context_none_but_holding_still_passes():
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(has_pm_ctx=False)}
    )
    assert pre_filter_candidates(ds, _tick(), {"AAPL"}, cfg) == ("AAPL",)


# ===========================================================================
# News gate: respects pre_filter_news_lookback_hours (NOT 24h LLM window)
# ===========================================================================


def test_news_gate_excludes_item_older_than_lookback():
    """An item 3h before the tick fails the default 2h lookback even
    though the LLM context's 24h window would include it."""
    cfg = _config()  # pre_filter_news_lookback_hours = 2
    tick = _tick()
    old_news = [_news(ts_et=tick - timedelta(hours=3))]
    ds = _day_state(
        tickers={"AAPL": _ticker_state(news_items=old_news)}
    )
    assert pre_filter_candidates(ds, tick, set(), cfg) == ()


def test_news_gate_excludes_item_inside_lag_buffer():
    """An item 10s before the tick is hidden by the 30s lag buffer."""
    cfg = _config()  # news_lag_seconds default 30
    tick = _tick()
    too_fresh = [_news(ts_et=tick - timedelta(seconds=10))]
    ds = _day_state(
        tickers={"AAPL": _ticker_state(news_items=too_fresh)}
    )
    assert pre_filter_candidates(ds, tick, set(), cfg) == ()


def test_news_gate_respects_custom_lookback_hours():
    """If config bumps the lookback to 6h, a 3h-old item now passes."""
    cfg = _config(pre_filter_news_lookback_hours=6)
    tick = _tick()
    news = [_news(ts_et=tick - timedelta(hours=3))]
    ds = _day_state(
        tickers={"AAPL": _ticker_state(news_items=news)}
    )
    assert pre_filter_candidates(ds, tick, set(), cfg) == ("AAPL",)


# ===========================================================================
# Multi-gate passes (no duplicates)
# ===========================================================================


def test_ticker_matching_multiple_gates_appears_once():
    """High rvol + big gap + news + held => still only one entry."""
    cfg = _config()
    tick = _tick()
    state = _ticker_state(
        pm_rvol=3.0, gap_pct=2.5, news_items=[_news(ts_et=tick - timedelta(minutes=15))]
    )
    ds = _day_state(tickers={"AAPL": state})
    out = pre_filter_candidates(ds, tick, {"AAPL"}, cfg)
    assert out == ("AAPL",)


# ===========================================================================
# Cap at max_candidates_per_tick
# ===========================================================================


def test_cap_honored_when_many_pass():
    """50 surviving tickers, cap=10 -> 10 output."""
    cfg = _config(max_candidates_per_tick=10)
    tickers = {
        f"T{i:02d}": _ticker_state(ticker=f"T{i:02d}", pm_rvol=5.0)
        for i in range(50)
    }
    ds = _day_state(tickers=tickers)
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert len(out) == 10


def test_cap_preserves_iteration_order():
    """First N survivors are kept, not random N."""
    cfg = _config(max_candidates_per_tick=3)
    tickers = {
        f"T{i:02d}": _ticker_state(ticker=f"T{i:02d}", pm_rvol=5.0)
        for i in range(10)
    }
    ds = _day_state(tickers=tickers)
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("T00", "T01", "T02")


def test_cap_above_survivor_count_returns_all():
    """Cap=30, only 2 survive -> output length 2."""
    cfg = _config(max_candidates_per_tick=30)
    tickers = {
        "AAPL": _ticker_state(ticker="AAPL", pm_rvol=5.0),
        "NVDA": _ticker_state(ticker="NVDA", pm_rvol=5.0),
        "TSLA": _ticker_state(ticker="TSLA"),  # fails all gates
    }
    ds = _day_state(tickers=tickers)
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL", "NVDA")


# ===========================================================================
# Return type + ordering
# ===========================================================================


def test_return_type_is_tuple():
    ds = _day_state()
    out = pre_filter_candidates(ds, _tick(), set(), _config())
    assert isinstance(out, tuple)


def test_output_order_matches_dict_insertion_order():
    """Python 3.7+ guarantees dict insertion order; verify our output
    preserves it."""
    cfg = _config()
    # Insert in a deliberately non-alphabetical order.
    tickers = {
        "ZZZ": _ticker_state(ticker="ZZZ", pm_rvol=5.0),
        "AAA": _ticker_state(ticker="AAA", pm_rvol=5.0),
        "MMM": _ticker_state(ticker="MMM", pm_rvol=5.0),
    }
    ds = _day_state(tickers=tickers)
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("ZZZ", "AAA", "MMM")


# ===========================================================================
# Quirks at the boundary with DayState
# ===========================================================================


def test_failed_tickers_in_day_state_ignored():
    """failed_tickers shouldn't be considered -- iteration is over
    day_state.tickers only."""
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(pm_rvol=5.0)},
        failed_tickers={"NVDA": "polygon 503"},
    )
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL",)


def test_holding_entry_not_in_day_state_silently_ignored():
    """A ticker in currently_holding but absent from day_state.tickers
    doesn't appear in output."""
    cfg = _config()
    ds = _day_state(
        tickers={"AAPL": _ticker_state(pm_rvol=5.0)},
    )
    # Ask about holding for AAPL (in day_state) and ZOMBIE (not).
    out = pre_filter_candidates(ds, _tick(), {"AAPL", "ZOMBIE"}, cfg)
    assert out == ("AAPL",)
    assert "ZOMBIE" not in out


def test_holding_empty_set_does_not_crash():
    """Defensive: empty holding set is a valid input."""
    cfg = _config()
    ds = _day_state(tickers={"AAPL": _ticker_state(pm_rvol=5.0)})
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAPL",)


# ===========================================================================
# Early-exit semantics: cap reached mid-iteration
# ===========================================================================


def test_cap_breaks_iteration_early():
    """Once the cap is reached, iteration stops -- later tickers are
    not even checked. Verifies the early-break logic."""
    cfg = _config(max_candidates_per_tick=2)
    tickers = {
        "AAA": _ticker_state(ticker="AAA", pm_rvol=5.0),
        "BBB": _ticker_state(ticker="BBB", pm_rvol=5.0),
        "CCC": _ticker_state(ticker="CCC", pm_rvol=5.0),  # would also pass
    }
    ds = _day_state(tickers=tickers)
    out = pre_filter_candidates(ds, _tick(), set(), cfg)
    assert out == ("AAA", "BBB")
    assert "CCC" not in out
