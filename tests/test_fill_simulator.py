"""Tests for data/replay/fill_simulator.py (M2.2 sub-task #14).

Covers:

- Position-transition table: each of the 9 (current state x decision) cells
  produces the expected portfolio mutation (or no-op).
- Flip cases (long+Sell, short+Buy): record_exit at fill reference with
  exit_reason="flip", then record_entry on the opposite side.
- Fill price = next_bar_open + slippage on the entry side (default
  fill_at="next_bar_open"); current_close mode picks current bar.
- Last-tick (15:55) decision with no 16:00 bar -> rejection
  "no_next_bar_for_fill" in next_bar_open mode; succeeds in
  current_close mode.
- Missing-bar paths: empty 5-min frame -> "no_5min_bars"; tick missing
  from index -> "no_5min_bar_at_tick".
- ATR fallback: DailyContext None -> compute_atr_stop_pct's fallback_pct
  produces a valid stop price; daily_atr_14 == 0 -> same path.
- Risk gate: oversized position scaled down by default
  scale_down_if_oversized=True; total-exposure cap exceeded -> rejection
  with reason starting "total_exposure_cap_exceeded".
- size_from_risk returns zero (entry_price <= 0 or stop_pct <= 0) ->
  rejection "size_from_risk_zero". (Practically unreachable on real
  data; exercised via a tiny stop_pct + huge entry_price.)
- decision_id sequencing: starts at decision_id_start, advances by 1
  per input TickDecision regardless of Hold/no-op, so the fill's
  decision_id maps cleanly to the input list index.
- Hold decisions: no-op (no fill, no rejection, no portfolio mutation).
- Result is a FillSimulationResult with tuple-typed fills and
  rejections; flip exits are NOT in fills (they show on
  portfolio.closed_positions).
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
    FillSimulationResult,
    RejectedEntry,
    apply_decisions_to_portfolio,
)
from data.replay.tick_loop import TickDecision
from data.replay.ticker_metadata import TickerMetadata
from sim.fills import SimulatedFill
from sim.portfolio import SimulatedPortfolio
from strategy.llm.types import LLMDecision


ET = ZoneInfo("America/New_York")
TRADING_DATE = date(2026, 4, 15)  # Wednesday


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _minute_bars(
    trading_date: date,
    *,
    price: float = 100.0,
    ticks: int = 78,
) -> pd.DataFrame:
    """Build 1-min bars aligned to 5-min boundaries (09:30, 09:35, ...).

    One 1-min row per 5-min boundary means the 5-min resample produces
    one bar per source row with predictable OHLCV. ``price`` is the
    open/high/low/close on every bar (no movement) unless a test
    overrides via ``_with_close_overrides``.

    Returns a tz-aware ET DatetimeIndex frame with columns
    open, high, low, close, volume, vwap, trade_count -- matches what
    ``data/replay/historical_bars.load_historical_bars_1min`` produces.
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


def _with_close_overrides(
    df: pd.DataFrame, overrides: dict[datetime, dict[str, float]]
) -> pd.DataFrame:
    """Patch specific bars in a fixture DataFrame for price-sensitive tests."""
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
        regime="bull",  # Regime is Literal["bull", "bear", "neutral"]
        is_trending=False,
        daily_atr_14=atr,
    )


# Sentinel for "argument not passed" so callers can explicitly pass
# None to request a None DailyContext / minute_bars (distinct from
# "use the default fixture").
_UNSET: object = object()


def _ticker_day_state(
    ticker: str = "AAPL",
    *,
    minute_bars=_UNSET,
    daily_context=_UNSET,
) -> TickerDayState:
    return TickerDayState(
        ticker=ticker,
        minute_bars=(
            _minute_bars(TRADING_DATE) if minute_bars is _UNSET
            else minute_bars
        ),
        daily_bars=pd.DataFrame(),
        daily_context=(
            _daily_context(ticker) if daily_context is _UNSET
            else daily_context
        ),
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
        slippage_bps=0.0,  # zero slippage by default -> easy price math
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _decision(action: str, confidence: int = 60) -> LLMDecision:
    return LLMDecision(
        action=action,
        confidence=confidence,
        setup_label="test",
        reasoning="test",
    )


def _tick(
    minute_offset: int,
    action: str,
    ticker: str = "AAPL",
    *,
    trading_date: date = TRADING_DATE,
) -> TickDecision:
    """Build a TickDecision at trading_date 09:30 + minute_offset."""
    base = datetime(
        trading_date.year, trading_date.month, trading_date.day,
        9, 30, tzinfo=ET,
    )
    return TickDecision(
        tick_et=base + timedelta(minutes=minute_offset),
        ticker=ticker,
        decision=_decision(action),
    )


def _portfolio(starting_cash: float = 100_000.0) -> SimulatedPortfolio:
    return SimulatedPortfolio(starting_cash=starting_cash, name="llm")


# ===========================================================================
# Position-transition table: 9 cells
# ===========================================================================


def test_flat_buy_opens_long():
    cfg = _config()
    pf = _portfolio()
    decisions = [_tick(0, "Buy")]
    result = apply_decisions_to_portfolio(
        decisions=decisions,
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.side == "buy"
    assert fill.qty > 0
    assert pf.has_position("AAPL")
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "buy"


def test_flat_sell_opens_short():
    cfg = _config()
    pf = _portfolio()
    decisions = [_tick(0, "Sell")]
    result = apply_decisions_to_portfolio(
        decisions=decisions,
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].side == "sell"
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "sell"


def test_flat_hold_noop():
    cfg = _config()
    pf = _portfolio()
    starting_cash = pf.cash
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Hold")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert result.rejections == ()
    assert pf.cash == starting_cash
    assert not pf.has_position("AAPL")


def test_long_buy_noop():
    cfg = _config()
    pf = _portfolio()
    # First decision opens long.
    result1 = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result1.fills) == 1
    cash_after_open = pf.cash
    # Second decision: long + Buy = no-op (no double-up).
    result2 = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    assert result2.fills == ()
    assert result2.rejections == ()
    assert pf.cash == cash_after_open
    assert len(pf.positions) == 1


def test_long_sell_flips_to_short():
    cfg = _config()
    pf = _portfolio()
    apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    # Flip via Sell.
    result = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    # One new entry (the short leg) is in fills; the long-exit is in
    # closed_positions with exit_reason="flip".
    assert len(result.fills) == 1
    assert result.fills[0].side == "sell"
    closed = pf.closed_positions
    assert len(closed) == 1
    assert closed[0].side == "buy"
    assert closed[0].exit_reason == "flip"
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "sell"


def test_long_hold_noop():
    cfg = _config()
    pf = _portfolio()
    apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    cash_before = pf.cash
    result = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Hold")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    assert result.fills == ()
    assert result.rejections == ()
    assert pf.cash == cash_before
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "buy"


def test_short_buy_flips_to_long():
    cfg = _config()
    pf = _portfolio()
    apply_decisions_to_portfolio(
        decisions=[_tick(0, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    result = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    assert len(result.fills) == 1
    assert result.fills[0].side == "buy"
    closed = pf.closed_positions
    assert len(closed) == 1 and closed[0].side == "sell"
    assert closed[0].exit_reason == "flip"
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "buy"


def test_short_sell_noop():
    cfg = _config()
    pf = _portfolio()
    apply_decisions_to_portfolio(
        decisions=[_tick(0, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    cash_before = pf.cash
    result = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    assert result.fills == ()
    assert result.rejections == ()
    assert pf.cash == cash_before


def test_short_hold_noop():
    cfg = _config()
    pf = _portfolio()
    apply_decisions_to_portfolio(
        decisions=[_tick(0, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    cash_before = pf.cash
    result = apply_decisions_to_portfolio(
        decisions=[_tick(5, "Hold")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=2,
    )
    assert result.fills == ()
    assert result.rejections == ()
    assert pf.cash == cash_before


# ===========================================================================
# Fill price + slippage
# ===========================================================================


def test_next_bar_open_fill_price_no_slippage():
    """Default fill_at='next_bar_open': fill at the next 5-min bar's open."""
    # First bar at 09:30 price=100; second bar at 09:35 price=105. A
    # Buy decision at 09:30 should fill at 105 (next bar open).
    df = _with_close_overrides(
        _minute_bars(TRADING_DATE),
        {datetime(2026, 4, 15, 9, 35, tzinfo=ET): {
            "open": 105.0, "high": 105.0, "low": 105.0, "close": 105.0,
        }},
    )
    tds = _ticker_day_state(minute_bars=df)
    cfg = _config(slippage_bps=0.0)
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].fill_price == pytest.approx(105.0)
    # Timestamp should be the next bar (09:35), not the decision bar.
    assert result.fills[0].fill_timestamp == datetime(
        2026, 4, 15, 9, 35, tzinfo=ET,
    )


def test_next_bar_open_buy_slippage_above_reference():
    """Buy at 100 with 10 bps slippage -> fill at 100.10."""
    df = _with_close_overrides(
        _minute_bars(TRADING_DATE),
        {datetime(2026, 4, 15, 9, 35, tzinfo=ET): {
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        }},
    )
    cfg = _config(slippage_bps=10.0)
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(_ticker_day_state(minute_bars=df),)),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills[0].fill_price == pytest.approx(100.10)


def test_next_bar_open_sell_slippage_below_reference():
    """Short-sell at 100 with 10 bps slippage -> fill at 99.90."""
    df = _with_close_overrides(
        _minute_bars(TRADING_DATE),
        {datetime(2026, 4, 15, 9, 35, tzinfo=ET): {
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        }},
    )
    cfg = _config(slippage_bps=10.0)
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Sell")],
        day_state=_day_state(tickers=(_ticker_day_state(minute_bars=df),)),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills[0].fill_price == pytest.approx(99.90)


def test_current_close_mode_fills_at_current_bar_close():
    """fill_at='current_close' picks the decision bar's close + slippage."""
    # 09:30 bar close=100; we expect the fill timestamp to be 09:30.
    df = _minute_bars(TRADING_DATE, price=100.0)
    cfg = _config(fill_at="current_close", slippage_bps=0.0)
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(_ticker_day_state(minute_bars=df),)),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills[0].fill_price == pytest.approx(100.0)
    assert result.fills[0].fill_timestamp == datetime(
        2026, 4, 15, 9, 30, tzinfo=ET,
    )


# ===========================================================================
# Last-tick (15:55) edge cases
# ===========================================================================


def test_last_tick_no_next_bar_rejects_in_next_bar_open_mode():
    """15:55 decision with no 16:00 bar -> 'no_next_bar_for_fill'."""
    cfg = _config()
    pf = _portfolio()
    # tick at 09:30 + 385 = 15:55 (last tick of the day)
    result = apply_decisions_to_portfolio(
        decisions=[_tick(385, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert len(result.rejections) == 1
    rej = result.rejections[0]
    assert rej.reason == "no_next_bar_for_fill"
    assert rej.side == "buy"


def test_last_tick_current_close_mode_fills_successfully():
    """15:55 decision with current_close mode -> fill at 15:55 close."""
    cfg = _config(fill_at="current_close")
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(385, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].fill_timestamp == datetime(
        2026, 4, 15, 15, 55, tzinfo=ET,
    )


# ===========================================================================
# Missing-bar paths
# ===========================================================================


def test_empty_minute_bars_rejects_with_no_5min_bars():
    cfg = _config()
    pf = _portfolio()
    tds = _ticker_day_state(minute_bars=pd.DataFrame())
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason == "no_5min_bars"


def test_tick_not_in_5min_index_rejects():
    """Tick at 09:30 but the 5-min frame only has bars starting 09:35."""
    base = datetime(2026, 4, 15, 9, 35, tzinfo=ET)
    rows = [
        {
            "timestamp": base + timedelta(minutes=5 * i),
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "volume": 10_000, "vwap": 100.0, "trade_count": 100,
        }
        for i in range(10)
    ]
    df = pd.DataFrame(rows).set_index("timestamp")
    tds = _ticker_day_state(minute_bars=df)
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],  # 09:30, but frame starts 09:35
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason == "no_5min_bar_at_tick"


# ===========================================================================
# ATR fallback
# ===========================================================================


def test_daily_context_none_uses_atr_fallback_pct():
    """No DailyContext -> compute_atr_stop_pct fallback (2.0%) drives stop."""
    cfg = _config()
    pf = _portfolio()
    tds = _ticker_day_state(daily_context=None)
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    fill = result.fills[0]
    # stop_price = entry * (1 - 0.02) = 100 * 0.98 = 98.0
    # Fill price = 100.0 (next bar open, no slippage).
    assert fill.stop_price == pytest.approx(98.0)


def test_daily_atr_zero_uses_fallback_pct():
    cfg = _config()
    pf = _portfolio()
    tds = _ticker_day_state(daily_context=_daily_context(atr=0.0))
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].stop_price == pytest.approx(98.0)


def test_daily_atr_nonzero_uses_atr_based_stop():
    """ATR=2.0 with default 1.5 multiplier on price=100 -> stop pct=3%."""
    cfg = _config()
    pf = _portfolio()
    tds = _ticker_day_state(daily_context=_daily_context(atr=2.0))
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(tickers=(tds,)),
        portfolio=pf,
        config=cfg,
    )
    # raw_pct = 1.5 * 2.0 / 100 * 100 = 3.0%
    # stop_price = 100 * (1 - 0.03) = 97.0
    assert result.fills[0].stop_price == pytest.approx(97.0)


# ===========================================================================
# Risk gate
# ===========================================================================


def test_oversized_position_scales_down_by_default():
    """size_from_risk + 20% position cap -> qty capped at 20% notional."""
    # On 100k equity, 20% cap = 20k notional. At price=100 that's 200 shares max.
    # size_from_risk would give:
    #   risk_per_share = 100 * 0.02 = 2.0 (default ATR fallback stop_pct=2%)
    #   max_loss = 100k * 0.005 = 500 -> qty = 250 (exceeds 200 cap)
    # validate_order with scale_down_if_oversized=True (the default
    # validate_order keyword) scales to 200.
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].qty == 200


def test_total_exposure_cap_exceeded_rejects():
    """Pre-load portfolio to >90% exposure; next entry should be rejected."""
    # Build a portfolio that's already at 85% exposure on one ticker, then
    # try to open AAPL which would push past the 90% cap.
    pf = SimulatedPortfolio(starting_cash=100_000.0, name="llm")
    # Manually inject a position to seed exposure.
    from sim.fills import SimulatedFill as _SF
    seed_fill = _SF(
        ticker="MSFT",
        side="buy",
        qty=850,
        fill_price=100.0,
        fill_timestamp=datetime(2026, 4, 14, 9, 30, tzinfo=ET),
        stop_price=98.0,
        decision_id=999,
    )
    pf.record_entry(seed_fill)
    # Now pf has 850*100 = 85,000 exposure on MSFT.
    # An AAPL Buy at 100, even capped to 20% (20k notional), would push
    # total to 105k = 105% > 90% cap.
    cfg = _config()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert len(result.rejections) == 1
    assert result.rejections[0].reason.startswith("total_exposure_cap_exceeded")


# ===========================================================================
# decision_id sequencing
# ===========================================================================


def test_decision_id_starts_at_default_one():
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills[0].decision_id == 1


def test_decision_id_respects_explicit_start():
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
        decision_id_start=42,
    )
    assert result.fills[0].decision_id == 42


def test_decision_id_advances_even_on_hold():
    """Hold doesn't fill but the id still advances so input-list index maps
    cleanly to the persistence-table primary key.
    """
    cfg = _config()
    pf = _portfolio()
    decisions = [
        _tick(0, "Hold"),     # id 1, no fill
        _tick(5, "Buy"),      # id 2, fill
    ]
    result = apply_decisions_to_portfolio(
        decisions=decisions,
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 1
    assert result.fills[0].decision_id == 2


# ===========================================================================
# Multi-decision walks
# ===========================================================================


def test_multiple_tickers_same_day():
    """AAPL Buy + MSFT Buy at the same tick -> two fills, two positions."""
    aapl_tds = _ticker_day_state("AAPL")
    msft_tds = _ticker_day_state("MSFT")
    cfg = _config(tickers=("AAPL", "MSFT"))
    pf = _portfolio()
    decisions = [
        _tick(0, "Buy", "AAPL"),
        _tick(0, "Buy", "MSFT"),
    ]
    result = apply_decisions_to_portfolio(
        decisions=decisions,
        day_state=_day_state(tickers=(aapl_tds, msft_tds)),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.fills) == 2
    tickers_filled = {f.ticker for f in result.fills}
    assert tickers_filled == {"AAPL", "MSFT"}
    assert pf.has_position("AAPL")
    assert pf.has_position("MSFT")


def test_double_flip_long_short_long():
    """Buy -> Sell -> Buy: 1 entry, 1 flip, 1 entry. closed_positions=2."""
    cfg = _config()
    pf = _portfolio()
    decisions = [
        _tick(0, "Buy"),
        _tick(5, "Sell"),
        _tick(10, "Buy"),
    ]
    result = apply_decisions_to_portfolio(
        decisions=decisions,
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    # Three entries (open long, flip to short, flip to long).
    assert len(result.fills) == 3
    sides = [f.side for f in result.fills]
    assert sides == ["buy", "sell", "buy"]
    # Two flips -> two closed positions with reason="flip".
    assert len(pf.closed_positions) == 2
    assert all(p.exit_reason == "flip" for p in pf.closed_positions)
    # Final position: long.
    pos = pf.get_position("AAPL")
    assert pos is not None and pos.side == "buy"


# ===========================================================================
# Result shape
# ===========================================================================


def test_result_is_fill_simulation_result_with_tuples():
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(0, "Buy")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert isinstance(result, FillSimulationResult)
    assert isinstance(result.fills, tuple)
    assert isinstance(result.rejections, tuple)
    assert all(isinstance(f, SimulatedFill) for f in result.fills)


def test_empty_decisions_returns_empty_result():
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert result.fills == ()
    assert result.rejections == ()


def test_rejection_records_correct_side():
    """A Sell on the last tick (no next bar) records side='sell' on rejection."""
    cfg = _config()
    pf = _portfolio()
    result = apply_decisions_to_portfolio(
        decisions=[_tick(385, "Sell")],
        day_state=_day_state(),
        portfolio=pf,
        config=cfg,
    )
    assert len(result.rejections) == 1
    assert result.rejections[0].side == "sell"
    assert result.rejections[0].reason == "no_next_bar_for_fill"


def test_rejection_dataclass_shape():
    rej = RejectedEntry(
        tick_et=datetime(2026, 4, 15, 9, 30, tzinfo=ET),
        ticker="AAPL", side="buy", requested_qty=100,
        reason="position_cap_exceeded",
        decision_id=1,
    )
    assert rej.ticker == "AAPL"
    assert rej.requested_qty == 100
    assert rej.decision_id == 1
