"""Tests for apply_bar_to_portfolio (M2.2 sub-task #15).

Covers:

- Empty portfolio: stop_outs is empty, mtm_point still appended (cash
  snapshot).
- Long stop fires when bar_low <= stop_price; does NOT fire when
  bar_low > stop_price.
- Short stop fires when bar_high >= stop_price; does NOT fire when
  bar_high < stop_price.
- Edge case: stop_price exactly equals bar_low (long) / bar_high
  (short) -> triggers (boundary is inclusive per check_stops).
- Stop fill price is stop_price itself (not bar_low / bar_high) --
  stops are market-on-trigger with no additional slippage.
- exit_reason on the closed Position is "stop_hit".
- realized_pl sign correctness: long stopped below entry -> negative;
  short stopped above entry -> negative.
- Multi-ticker selectivity: one position stops, the other doesn't.
- Missing-bar safety: ticker absent from the 5-min frame at bar_et ->
  no stop check, no false trigger; MTM falls back to entry_price for
  that ticker (no false equity swing).
- resample_cache: when caller passes one, it is populated in place so
  subsequent calls (other bars, the decision walker, the EOD flatten)
  re-use the same resampled frames.
- mtm_point reflects the bar's close price (not entry price).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, '.')

from analysis.indicators import DailyContext
from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.fill_simulator import (
    BarApplicationResult,
    StopOut,
    apply_bar_to_portfolio,
)
from data.replay.ticker_metadata import TickerMetadata
from sim.fills import SimulatedFill
from sim.portfolio import EquityPoint, SimulatedPortfolio


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)  # Wednesday


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_fill_simulator.py for consistency)
# ---------------------------------------------------------------------------


def _minute_bars(
    trading_date: date,
    *,
    price: float = 100.0,
    ticks: int = 78,
) -> pd.DataFrame:
    """One 1-min row at every 5-min boundary, flat OHLC = price.

    With one 1-min row per 5-min window, the resample produces one
    bar per row with high=low=open=close=price. Tests override
    specific rows to set high/low for stop-trigger cases.
    """
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


def _with_overrides(
    df: pd.DataFrame, overrides: dict[datetime, dict[str, float]]
) -> pd.DataFrame:
    out = df.copy()
    for ts, fields in overrides.items():
        for col, val in fields.items():
            out.at[pd.Timestamp(ts), col] = val
    return out


def _daily_context(
    ticker: str = "AAPL", *, atr: float = 1.5, last_close: float = 100.0
) -> DailyContext:
    return DailyContext(
        ticker=ticker,
        last_close=last_close,
        sma_200=last_close * 0.95,
        adx_14=20.0,
        regime="bull",
        is_trending=False,
        daily_atr_14=atr,
    )


def _ticker_day_state(
    ticker: str = "AAPL",
    *,
    minute_bars: pd.DataFrame | None = None,
) -> TickerDayState:
    return TickerDayState(
        ticker=ticker,
        minute_bars=(
            _minute_bars(TRADING_DATE) if minute_bars is None
            else minute_bars
        ),
        daily_bars=pd.DataFrame(),
        daily_context=_daily_context(ticker),
        premarket_context=None,
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
        slippage_bps=0.0,
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _portfolio(starting_cash: float = 100_000.0) -> SimulatedPortfolio:
    return SimulatedPortfolio(starting_cash=starting_cash, name="llm")


def _bar_et(minute_offset: int) -> datetime:
    """Canonical 5-min bar timestamp at 09:30 + minute_offset."""
    base = datetime(
        TRADING_DATE.year, TRADING_DATE.month, TRADING_DATE.day,
        9, 30, tzinfo=ET,
    )
    return base + timedelta(minutes=minute_offset)


def _seed_position(
    portfolio: SimulatedPortfolio,
    *,
    ticker: str,
    side: str,
    qty: int,
    entry_price: float,
    stop_price: float,
    entry_minute: int = 0,
) -> None:
    """Open a position by calling record_entry with a synthetic fill."""
    portfolio.record_entry(
        SimulatedFill(
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            fill_price=entry_price,
            fill_timestamp=_bar_et(entry_minute),
            stop_price=stop_price,
            decision_id=1,
        )
    )


# ===========================================================================
# 1. Empty portfolio
# ===========================================================================


def test_empty_portfolio_emits_only_mtm_point():
    cfg = _config()
    pf = _portfolio()
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert isinstance(result, BarApplicationResult)
    assert result.bar_et == _bar_et(5)
    assert result.stop_outs == ()
    assert isinstance(result.mtm_point, EquityPoint)
    assert result.mtm_point.timestamp == _bar_et(5)
    assert result.mtm_point.equity == pf.starting_cash
    assert result.mtm_point.n_open_positions == 0
    # The portfolio's equity_curve was extended.
    assert pf.equity_curve[-1] is result.mtm_point


# ===========================================================================
# 2. Long stop trigger
# ===========================================================================


def test_long_stop_fires_when_bar_low_below_stop():
    """Long entered at 100, stop at 98. 09:35 bar dips to 97 -> stop fires."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    # Override the 09:35 bar to dip to 97 (low) but close at 99.
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 97.0, "high": 100.0, "close": 99.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.stop_outs) == 1
    so = result.stop_outs[0]
    assert so.ticker == "AAPL"
    assert so.side == "buy"
    assert so.qty == 10
    assert so.stop_price == 98.0
    # Long stopped below entry -> negative realized
    assert so.realized_pl == pytest.approx((98.0 - 100.0) * 10)
    assert not pf.has_position("AAPL")
    closed = pf.closed_positions[-1]
    assert closed.exit_reason == "stop_hit"
    assert closed.exit_price == 98.0
    assert closed.exit_timestamp == _bar_et(5)


def test_long_stop_does_not_fire_when_bar_low_above_stop():
    """Long stop at 98, bar low at 99 -> no fire."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 99.0, "high": 101.0, "close": 100.5}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.stop_outs == ()
    assert pf.has_position("AAPL")


# ===========================================================================
# 3. Short stop trigger
# ===========================================================================


def test_short_stop_fires_when_bar_high_above_stop():
    """Short entered at 100, stop at 102. 09:35 bar prints 103 high -> fires."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="sell",
        qty=10, entry_price=100.0, stop_price=102.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 100.0, "high": 103.0, "close": 101.5}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.stop_outs) == 1
    so = result.stop_outs[0]
    assert so.side == "sell"
    assert so.stop_price == 102.0
    # Short stopped above entry -> negative realized
    assert so.realized_pl == pytest.approx((100.0 - 102.0) * 10)
    assert not pf.has_position("AAPL")
    closed = pf.closed_positions[-1]
    assert closed.exit_reason == "stop_hit"


def test_short_stop_does_not_fire_when_bar_high_below_stop():
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="sell",
        qty=10, entry_price=100.0, stop_price=102.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 99.0, "high": 101.0, "close": 100.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.stop_outs == ()
    assert pf.has_position("AAPL")


# ===========================================================================
# 4. Boundary cases (equality)
# ===========================================================================


def test_long_stop_fires_at_exact_equality():
    """bar_low == stop_price triggers (<= per check_stops)."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 98.0, "high": 100.0, "close": 99.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.stop_outs) == 1


def test_short_stop_fires_at_exact_equality():
    """bar_high == stop_price triggers (>= per check_stops)."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="sell",
        qty=10, entry_price=100.0, stop_price=102.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 100.0, "high": 102.0, "close": 101.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.stop_outs) == 1


# ===========================================================================
# 5. Stop fill price is stop_price, not bar low/high
# ===========================================================================


def test_stop_fill_price_is_stop_price_not_bar_low():
    """Long stopped: fill price = stop_price (98), not bar_low (95)."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 95.0, "high": 100.0, "close": 96.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.stop_outs[0].stop_price == 98.0
    # Even with bar dipping to 95, fill is at stop_price=98.
    closed = pf.closed_positions[-1]
    assert closed.exit_price == 98.0


# ===========================================================================
# 6. Multi-ticker selectivity
# ===========================================================================


def test_multi_ticker_one_stops_other_doesnt():
    cfg = _config(tickers=("AAPL", "MSFT"))
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    _seed_position(
        pf, ticker="MSFT", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    aapl_bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 97.0, "high": 100.0, "close": 99.0}},
    )
    msft_bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 99.0, "high": 101.0, "close": 100.5}},
    )
    tickers = (
        _ticker_day_state("AAPL", minute_bars=aapl_bars),
        _ticker_day_state("MSFT", minute_bars=msft_bars),
    )
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state(tickers),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.stop_outs) == 1
    assert result.stop_outs[0].ticker == "AAPL"
    assert not pf.has_position("AAPL")
    assert pf.has_position("MSFT")


# ===========================================================================
# 7. Missing bar safety
# ===========================================================================


def test_missing_bar_skips_stop_check_no_false_trigger():
    """Ticker absent from 5-min frame at bar_et -> no stop check fires."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    # Empty minute_bars frame -> 5-min frame is empty at bar_et.
    empty = _minute_bars(TRADING_DATE).iloc[0:0]
    tds = _ticker_day_state(minute_bars=empty)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.stop_outs == ()
    assert pf.has_position("AAPL")
    # MTM still appended; missing-price ticker falls back to entry_price
    # in equity() -> position contribution = qty * entry = 1000.
    # cash was reduced by qty * entry = 1000 at record_entry.
    # equity = cash + 1000 = starting_cash.
    assert result.mtm_point.equity == pytest.approx(pf.starting_cash)


# ===========================================================================
# 8. MTM uses bar close
# ===========================================================================


def test_mtm_reflects_bar_close_for_long():
    """Long 10 @ 100, bar close 110 -> equity = starting + 100 unrealized."""
    cfg = _config()
    pf = _portfolio(starting_cash=100_000.0)
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(5): {"low": 99.0, "high": 111.0, "close": 110.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    # Cash = 100k - qty*entry = 100k - 1000 = 99k.
    # Position MTM at close 110 = 10*110 = 1100.
    # Equity = 99k + 1100 = 100,100.
    assert result.mtm_point.equity == pytest.approx(100_100.0)
    assert result.mtm_point.n_open_positions == 1


# ===========================================================================
# 9. resample_cache shared with caller
# ===========================================================================


def test_resample_cache_populated_by_caller_lookup():
    """Caller passes empty cache; call populates it for the ticker."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    cache: dict[str, pd.DataFrame] = {}
    apply_bar_to_portfolio(
        bar_et=_bar_et(5),
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        resample_cache=cache,
    )
    assert "AAPL" in cache
    assert not cache["AAPL"].empty


def test_resample_cache_reused_across_two_calls():
    """Same cache passed to two consecutive calls -> resample runs once."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=98.0,
    )
    cache: dict[str, pd.DataFrame] = {}
    day = _day_state()
    apply_bar_to_portfolio(
        bar_et=_bar_et(5), day_state=day, portfolio=pf,
        config=cfg, resample_cache=cache,
    )
    frame_first = cache["AAPL"]
    apply_bar_to_portfolio(
        bar_et=_bar_et(10), day_state=day, portfolio=pf,
        config=cfg, resample_cache=cache,
    )
    frame_second = cache["AAPL"]
    # Same object reference -> not re-resampled.
    assert frame_first is frame_second


# ===========================================================================
# 10. Equity curve grows with each call
# ===========================================================================


def test_consecutive_calls_grow_equity_curve():
    cfg = _config()
    pf = _portfolio()
    day = _day_state()
    for minute_offset in (0, 5, 10, 15):
        apply_bar_to_portfolio(
            bar_et=_bar_et(minute_offset),
            day_state=day,
            portfolio=pf,
            config=cfg,
        )
    assert len(pf.equity_curve) == 4
    assert [pt.timestamp for pt in pf.equity_curve] == [
        _bar_et(0), _bar_et(5), _bar_et(10), _bar_et(15),
    ]
