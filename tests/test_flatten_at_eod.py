"""Tests for flatten_at_eod (M2.2 sub-task #15).

Covers:

- Empty portfolio: no exits returned, final MTM still appended.
- Long position flattened at the bar's close; realized_pl matches the
  long-side formula.
- Short position flattened at the bar's close; realized_pl matches
  the short-side formula.
- Mixed long + short portfolio: both close in one pass.
- Missing-bar fallback: when the bar at flatten_et is absent for a
  ticker, falls back to the position's entry_price AND logs a WARNING
  (visible degradation per Rule 18 / fail-loud-never-fake).
- exit_reason on each closed Position is "eod_flatten".
- Final mark_to_market is emitted after the flatten loop and reflects
  zero open positions (all-cash state).
"""
from __future__ import annotations

import logging
import sys
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
    EodExit,
    EodFlattenResult,
    flatten_at_eod,
)
from data.replay.ticker_metadata import TickerMetadata
from sim.fills import SimulatedFill
from sim.portfolio import SimulatedPortfolio


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)  # Wednesday


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_apply_bar_to_portfolio.py)
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


# Last RTH 5-min bar of the day: 09:30 + 5*77 minutes = 15:55.
FLATTEN_MINUTE = 5 * 77


# ===========================================================================
# 1. Empty portfolio
# ===========================================================================


def test_empty_portfolio_no_exits_final_mtm_appended():
    cfg = _config()
    pf = _portfolio()
    result = flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert isinstance(result, EodFlattenResult)
    assert result.flatten_et == _bar_et(FLATTEN_MINUTE)
    assert result.exits == ()
    # Final MTM still appended.
    assert len(pf.equity_curve) == 1
    assert pf.equity_curve[-1].timestamp == _bar_et(FLATTEN_MINUTE)
    assert pf.equity_curve[-1].n_open_positions == 0
    assert pf.equity_curve[-1].equity == pf.starting_cash


# ===========================================================================
# 2. Long flatten
# ===========================================================================


def test_long_flatten_at_close():
    """Long 10 @ 100; 15:55 close = 105 -> exit at 105, realized = +50."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(FLATTEN_MINUTE): {"close": 105.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.exits) == 1
    exit_row = result.exits[0]
    assert exit_row.ticker == "AAPL"
    assert exit_row.side == "buy"
    assert exit_row.qty == 10
    assert exit_row.exit_price == 105.0
    assert exit_row.realized_pl == pytest.approx((105.0 - 100.0) * 10)
    assert not pf.has_position("AAPL")


# ===========================================================================
# 3. Short flatten
# ===========================================================================


def test_short_flatten_at_close():
    """Short 10 @ 100; 15:55 close = 95 -> exit at 95, realized = +50."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="sell",
        qty=10, entry_price=100.0, stop_price=105.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(FLATTEN_MINUTE): {"close": 95.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    result = flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.exits) == 1
    exit_row = result.exits[0]
    assert exit_row.side == "sell"
    assert exit_row.exit_price == 95.0
    assert exit_row.realized_pl == pytest.approx((100.0 - 95.0) * 10)
    assert not pf.has_position("AAPL")


# ===========================================================================
# 4. Mixed long + short, both close
# ===========================================================================


def test_mixed_long_and_short_both_close():
    cfg = _config(tickers=("AAPL", "MSFT"))
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    _seed_position(
        pf, ticker="MSFT", side="sell",
        qty=5, entry_price=200.0, stop_price=210.0,
    )
    aapl_bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(FLATTEN_MINUTE): {"close": 102.0}},
    )
    msft_bars = _with_overrides(
        _minute_bars(TRADING_DATE, price=200.0),
        {_bar_et(FLATTEN_MINUTE): {"close": 198.0}},
    )
    tickers = (
        _ticker_day_state("AAPL", minute_bars=aapl_bars),
        _ticker_day_state("MSFT", minute_bars=msft_bars),
    )
    result = flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state(tickers),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.exits) == 2
    by_ticker = {e.ticker: e for e in result.exits}
    assert by_ticker["AAPL"].exit_price == 102.0
    assert by_ticker["AAPL"].realized_pl == pytest.approx(20.0)
    assert by_ticker["MSFT"].exit_price == 198.0
    assert by_ticker["MSFT"].realized_pl == pytest.approx(10.0)
    assert not pf.positions  # all closed
    assert pf.equity_curve[-1].n_open_positions == 0


# ===========================================================================
# 5. Missing-bar fallback to entry_price WITH warning log
# ===========================================================================


def test_missing_bar_falls_back_to_entry_price_with_warning(caplog):
    """flatten_et bar absent -> fall back to entry_price; log WARNING."""
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    # Empty minute_bars -> no bar at flatten_et.
    empty = _minute_bars(TRADING_DATE).iloc[0:0]
    tds = _ticker_day_state(minute_bars=empty)
    with caplog.at_level(logging.WARNING, logger="data.replay.fill_simulator"):
        result = flatten_at_eod(
            flatten_et=_bar_et(FLATTEN_MINUTE),
            day_state=_day_state((tds,)),
            portfolio=pf,
            config=cfg,
        )
    assert len(result.exits) == 1
    assert result.exits[0].exit_price == 100.0  # entry_price fallback
    assert result.exits[0].realized_pl == 0.0
    # Visible degradation per Rule 18 -- WARNING log emitted.
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "flatten_at_eod" in r.message
    ]
    assert len(warnings) == 1
    assert "AAPL" in warnings[0].message
    assert "no bar" in warnings[0].message.lower()


# ===========================================================================
# 6. exit_reason is "eod_flatten" on closed positions
# ===========================================================================


def test_exit_reason_is_eod_flatten():
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(FLATTEN_MINUTE): {"close": 104.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    closed = pf.closed_positions[-1]
    assert closed.exit_reason == "eod_flatten"
    assert closed.exit_timestamp == _bar_et(FLATTEN_MINUTE)


# ===========================================================================
# 7. Final MTM appended even when there were positions to close
# ===========================================================================


def test_final_mtm_appended_after_flatten():
    cfg = _config()
    pf = _portfolio()
    _seed_position(
        pf, ticker="AAPL", side="buy",
        qty=10, entry_price=100.0, stop_price=95.0,
    )
    starting_curve_len = len(pf.equity_curve)
    bars = _with_overrides(
        _minute_bars(TRADING_DATE),
        {_bar_et(FLATTEN_MINUTE): {"close": 110.0}},
    )
    tds = _ticker_day_state(minute_bars=bars)
    flatten_at_eod(
        flatten_et=_bar_et(FLATTEN_MINUTE),
        day_state=_day_state((tds,)),
        portfolio=pf,
        config=cfg,
    )
    # One new equity point appended.
    assert len(pf.equity_curve) == starting_curve_len + 1
    final = pf.equity_curve[-1]
    assert final.timestamp == _bar_et(FLATTEN_MINUTE)
    assert final.n_open_positions == 0
    # Realized 100 on the long -> cash = 100k + 100 = 100,100.
    assert final.equity == pytest.approx(100_100.0)
    assert final.cash == pytest.approx(100_100.0)
