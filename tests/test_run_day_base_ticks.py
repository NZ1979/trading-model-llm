"""Tests for run_day_base_ticks (M2.2 sub-task #17).

Covers:

- Empty day_state.tickers -> empty decisions list.
- One ticker / 78 ticks -> 78 TickDecisions; ordering = (tick, ticker).
- Multi-ticker: outer loop is tick, inner is ticker order from
  day_state.tickers iteration.
- gap-and-go path fires when premarket_ctx satisfies the gates AND
  the tick is in the 09:35-10:00 ET window.
- Sentiment gate: gap-and-go Buy with sentiment below +3 Holds (per
  evaluate_trade's GAP_AND_GO_SENTIMENT_MIN_BUY).
- Sentiment lookup queries sentiment_conn at each (ticker, tick).
- futures_walls is always None -- walls_status='absent' in the
  reasoning for non-pullback paths.
- require_walls_for_pullback respects config flag.
- Indicator cache reused across ticks (verify with a spy on
  _build_today_5min_indicators).
- Adapter _trade_to_llm_decision: action / confidence /
  setup_label / reasoning round-trip; reasoning truncated to 280
  chars when the reasons list is long.
- Empty minute_bars on a ticker -> all-Hold (no_bars short-circuit).
- Adapter handles empty reasons tuple via the "no_reasons" sentinel.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, '.')

from analysis.indicators import DailyContext, PremarketContext
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.tick_loop import (
    TickDecision,
    _trade_to_llm_decision,
    run_day_base_ticks,
    tick_times_for_day,
)
from data.replay.ticker_metadata import TickerMetadata
from sim.portfolio import SimulatedPortfolio
from strategy.signal_engine import TradeDecision


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)


# ---------------------------------------------------------------------------
# Fixtures (mirror test_apply_bar_to_portfolio.py)
# ---------------------------------------------------------------------------


def _minute_bars(
    trading_date: date,
    *,
    price: float = 100.0,
    ticks: int = 78,
) -> pd.DataFrame:
    base = datetime(
        trading_date.year, trading_date.month, trading_date.day,
        9, 30, tzinfo=ET,
    )
    rows = []
    for i in range(ticks):
        ts = base + timedelta(minutes=5 * i)
        rows.append({
            "timestamp": ts,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 10_000, "vwap": price, "trade_count": 100,
        })
    df = pd.DataFrame(rows).set_index("timestamp")
    return df


def _daily_context(ticker: str = "AAPL", *, atr: float = 1.5) -> DailyContext:
    return DailyContext(
        ticker=ticker, last_close=100.0, sma_200=95.0,
        adx_14=20.0, regime="bull", is_trending=False, daily_atr_14=atr,
    )


def _premarket_context(
    ticker: str = "AAPL",
    *,
    gap_pct: float = 2.0,
    is_unusual_volume: bool = True,
    premarket_rvol: float = 6.0,
    premarket_high: float = 99.5,
    premarket_low: float = 99.0,
) -> PremarketContext:
    return PremarketContext(
        ticker=ticker, prior_close=98.0,
        prior_high=98.5, prior_low=97.5,
        premarket_high=premarket_high, premarket_low=premarket_low,
        premarket_volume=500_000, premarket_rvol=premarket_rvol,
        is_unusual_volume=is_unusual_volume,
        gap_pct=gap_pct, gap_atr_ratio=1.0,
    )


def _ticker_day_state(
    ticker: str = "AAPL",
    *,
    minute_bars: pd.DataFrame | None = None,
    daily_context: DailyContext | None = None,
    premarket_context: PremarketContext | None = None,
) -> TickerDayState:
    return TickerDayState(
        ticker=ticker,
        minute_bars=(
            _minute_bars(TRADING_DATE) if minute_bars is None
            else minute_bars
        ),
        daily_bars=pd.DataFrame(),
        daily_context=(
            _daily_context(ticker) if daily_context is None
            else daily_context
        ),
        premarket_context=premarket_context,
        ticker_metadata=TickerMetadata(
            ticker=ticker, sector="Information Technology",
            market_cap_bucket="mega", avg_daily_volume=50_000_000,
        ),
        news_items=[],
        last_5_daily_closes=(99.0, 99.5, 100.0, 100.5, 100.0),
    )


def _day_state(
    tickers: Iterable[TickerDayState] = (),
    *,
    trading_date: date = TRADING_DATE,
) -> DayState:
    if not tickers:
        tickers = (_ticker_day_state(),)
    ticker_map = {tds.ticker: tds for tds in tickers}
    return DayState(
        trading_date=trading_date,
        vix_level=20.0,
        market_regime_label="neutral",
        sentiment_lookup={},
        tickers=ticker_map,
        failed_tickers={},
        has_earnings_today={t: False for t in ticker_map},
        has_earnings_within_3d={t: False for t in ticker_map},
    )


def _config(**overrides) -> ReplayConfig:
    kwargs = dict(
        start_date=TRADING_DATE,
        end_date=TRADING_DATE,
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _empty_sentiment_conn() -> sqlite3.Connection:
    """Open an in-memory SQLite with the sentiment schema; no rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sentiment ("
        "news_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, "
        "sentiment INTEGER NOT NULL, reasoning TEXT, headline TEXT, "
        "scored_at REAL NOT NULL)"
    )
    return conn


def _seed_sentiment(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    sentiment: int,
    scored_at_et: datetime,
) -> None:
    conn.execute(
        "INSERT INTO sentiment (ticker, sentiment, scored_at) "
        "VALUES (?, ?, ?)",
        (ticker, sentiment, scored_at_et.timestamp()),
    )
    conn.commit()


# ===========================================================================
# 1. Empty inputs
# ===========================================================================


def test_empty_tickers_returns_empty_decisions():
    # day_state with zero tickers -> 78 ticks but inner loop empty -> []
    ds = DayState(
        trading_date=TRADING_DATE, vix_level=None,
        market_regime_label="neutral", sentiment_lookup={},
        tickers={}, failed_tickers={},
        has_earnings_today={}, has_earnings_within_3d={},
    )
    result = run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=_empty_sentiment_conn(),
    )
    assert result == []


# ===========================================================================
# 2. Tick count and ordering
# ===========================================================================


def test_one_ticker_78_ticks_yields_78_decisions():
    """One ticker, full day -> 78 TickDecisions."""
    ds = _day_state()
    result = run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=_empty_sentiment_conn(),
    )
    assert len(result) == 78
    assert all(isinstance(td, TickDecision) for td in result)
    # First tick is 09:30 ET.
    expected_first = tick_times_for_day(TRADING_DATE)[0]
    assert result[0].tick_et == expected_first


def test_multi_ticker_outer_tick_inner_ticker_order():
    """Two tickers, two ticks for brevity (slice the result to first 4).
    Verify ordering is (tick0, AAPL), (tick0, MSFT), (tick1, AAPL), (tick1, MSFT).
    """
    ds = _day_state((
        _ticker_day_state("AAPL"),
        _ticker_day_state("MSFT"),
    ))
    result = run_day_base_ticks(
        day_state=ds, config=_config(tickers=("AAPL", "MSFT")),
        sentiment_conn=_empty_sentiment_conn(),
    )
    # 78 ticks * 2 tickers = 156 total
    assert len(result) == 156
    first_four = result[:4]
    ticks = tick_times_for_day(TRADING_DATE)
    assert first_four[0].tick_et == ticks[0]
    assert first_four[0].ticker == "AAPL"
    assert first_four[1].tick_et == ticks[0]
    assert first_four[1].ticker == "MSFT"
    assert first_four[2].tick_et == ticks[1]
    assert first_four[2].ticker == "AAPL"
    assert first_four[3].tick_et == ticks[1]
    assert first_four[3].ticker == "MSFT"


# ===========================================================================
# 3. gap-and-go path fires
# ===========================================================================


def test_gap_and_go_buy_fires_with_strong_sentiment():
    """gap_pct=+2, premarket vol unusual, sentiment +5 -> Buy at 09:35."""
    conn = _empty_sentiment_conn()
    # Sentiment scored 5 minutes before market open -> within 1-hour
    # freshness window.
    _seed_sentiment(
        conn, ticker="AAPL", sentiment=5,
        scored_at_et=datetime(2026, 4, 15, 9, 25, tzinfo=ET),
    )
    pm = _premarket_context(gap_pct=2.0)
    ds = _day_state((_ticker_day_state(premarket_context=pm),))
    result = run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=conn,
    )
    # 09:35 is the second tick (index 1). gap-and-go window is 09:35-10:00.
    tick_0935 = [td for td in result if td.tick_et.minute == 35
                  and td.tick_et.hour == 9]
    assert len(tick_0935) == 1
    assert tick_0935[0].decision.action == "Buy"
    assert tick_0935[0].decision.setup_label == "gap_and_go"


# ===========================================================================
# 4. Sentiment gate
# ===========================================================================


def test_gap_and_go_holds_when_sentiment_below_threshold():
    """gap-and-go Buy needs sentiment >= +3. Score 0 -> Hold."""
    conn = _empty_sentiment_conn()
    _seed_sentiment(
        conn, ticker="AAPL", sentiment=0,
        scored_at_et=datetime(2026, 4, 15, 9, 25, tzinfo=ET),
    )
    pm = _premarket_context(gap_pct=2.0)
    ds = _day_state((_ticker_day_state(premarket_context=pm),))
    result = run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=conn,
    )
    tick_0935 = [td for td in result if td.tick_et.minute == 35
                  and td.tick_et.hour == 9]
    assert tick_0935[0].decision.action == "Hold"


# ===========================================================================
# 5. Sentiment lookup hits the connection at every tick
# ===========================================================================


def test_sentiment_lookup_called_per_tick(monkeypatch):
    """latest_sentiment should be invoked once per (tick, ticker)."""
    calls: list[tuple] = []
    original = __import__(
        "data.replay.tick_loop", fromlist=["latest_sentiment"]
    ).latest_sentiment

    def _spy(conn, ticker, as_of_et, max_age_seconds=3600):
        calls.append((ticker, as_of_et))
        return original(conn, ticker, as_of_et, max_age_seconds)

    monkeypatch.setattr("data.replay.tick_loop.latest_sentiment", _spy)
    ds = _day_state()
    run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=_empty_sentiment_conn(),
    )
    # 78 ticks * 1 ticker = 78 calls
    assert len(calls) == 78


# ===========================================================================
# 6. require_walls_for_pullback respects config flag
# ===========================================================================


@pytest.mark.parametrize("flag", [True, False])
def test_require_walls_flag_propagated_to_evaluate_trade(monkeypatch, flag):
    """ReplayConfig.base_require_walls_for_pullback should be the value
    passed into evaluate_trade's require_walls_for_pullback kwarg."""
    captured: list[dict] = []
    original = __import__(
        "data.replay.tick_loop", fromlist=["evaluate_trade"]
    ).evaluate_trade

    def _spy(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr("data.replay.tick_loop.evaluate_trade", _spy)
    ds = _day_state()
    run_day_base_ticks(
        day_state=ds,
        config=_config(base_require_walls_for_pullback=flag),
        sentiment_conn=_empty_sentiment_conn(),
    )
    # All calls should see the same flag value.
    assert all(c["require_walls_for_pullback"] == flag for c in captured)


def test_evaluate_trade_always_called_with_walls_none(monkeypatch):
    """Replay never passes a walls bundle -- evaluate_trade gets None."""
    captured: list[dict] = []
    original = __import__(
        "data.replay.tick_loop", fromlist=["evaluate_trade"]
    ).evaluate_trade

    def _spy(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr("data.replay.tick_loop.evaluate_trade", _spy)
    ds = _day_state()
    run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=_empty_sentiment_conn(),
    )
    assert all(c["futures_walls"] is None for c in captured)


# ===========================================================================
# 7. Indicator cache reused across ticks
# ===========================================================================


def test_indicator_cache_reused_across_ticks(monkeypatch):
    """_build_today_5min_indicators should be called once per ticker per day."""
    calls: list[str] = []
    original = __import__(
        "data.replay.tick_loop",
        fromlist=["_build_today_5min_indicators"],
    )._build_today_5min_indicators

    def _spy(tds, trading_date):
        calls.append(tds.ticker)
        return original(tds, trading_date)

    monkeypatch.setattr(
        "data.replay.tick_loop._build_today_5min_indicators", _spy
    )
    ds = _day_state((
        _ticker_day_state("AAPL"),
        _ticker_day_state("MSFT"),
    ))
    run_day_base_ticks(
        day_state=ds, config=_config(tickers=("AAPL", "MSFT")),
        sentiment_conn=_empty_sentiment_conn(),
    )
    # Each ticker resampled exactly once despite 78 ticks.
    assert sorted(calls) == ["AAPL", "MSFT"]


# ===========================================================================
# 8. Adapter: TradeDecision -> LLMDecision
# ===========================================================================


def test_adapter_maps_basic_fields():
    td = TradeDecision(
        action="Buy", ticker="AAPL", setup="gap_and_go",
        sentiment_score=5, technical_confidence=80,
        walls_status="n/a",
        reasons=("gap_up_2%", "premarket_rvol=6x"),
    )
    out = _trade_to_llm_decision(td)
    assert out.action == "Buy"
    assert out.confidence == 80
    assert out.setup_label == "gap_and_go"
    assert "gap_up_2%" in out.reasoning
    assert "premarket_rvol=6x" in out.reasoning


def test_adapter_truncates_reasoning_to_280():
    long_reasons = tuple(f"reason_{i:03d}_padded_with_extra_text"
                          for i in range(50))
    td = TradeDecision(
        action="Hold", ticker="AAPL", setup="none",
        sentiment_score=None, technical_confidence=50,
        walls_status="absent",
        reasons=long_reasons,
    )
    out = _trade_to_llm_decision(td)
    assert len(out.reasoning) <= 280


def test_adapter_handles_empty_reasons_tuple():
    td = TradeDecision(
        action="Hold", ticker="AAPL", setup="none",
        sentiment_score=None, technical_confidence=0,
        walls_status="absent",
        reasons=(),
    )
    out = _trade_to_llm_decision(td)
    assert out.reasoning == "no_reasons"


# ===========================================================================
# 9. Empty minute_bars -> all Holds
# ===========================================================================


def test_empty_minute_bars_produces_all_holds():
    empty = _minute_bars(TRADING_DATE).iloc[0:0]
    tds = _ticker_day_state(minute_bars=empty)
    ds = _day_state((tds,))
    result = run_day_base_ticks(
        day_state=ds, config=_config(),
        sentiment_conn=_empty_sentiment_conn(),
    )
    assert len(result) == 78
    assert all(td.decision.action == "Hold" for td in result)
