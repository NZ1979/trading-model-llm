"""Tests for data/replay/tick_context.py (M2.2 sub-task #10).

Covers:
  - 5-min OHLCV resample correctness (open=first, high=max, low=min,
    close=last, volume=sum, trade_count=sum; vwap volume-weighted)
  - Empty 1-min input -> empty 5-min output
  - SPY change_pct positive / negative / zero / empty / non-positive open
  - SPY rvol normal / fewer-than-20-bars / empty fallback to 1.0
  - minutes_since_open and minutes_until_close at 09:30 / 10:30 / 12:00
    / 15:55 / before-open clamp / after-close clamp
  - in_gap_and_go_window True at 09:30, True at 10:30 (60-min boundary
    inclusive), False at 10:31
  - build_tick_context happy path: LLMContext fields all wired,
    including last_5_daily_closes from TickerDayState
  - KeyError when ticker is in day_state.failed_tickers (not in
    day_state.tickers)
  - News lag and lookback enforcement at tick boundary
  - News cap at 5 most-recent
  - Sentiment lookup wired from day_state.sentiment_lookup
  - VIX None passes through
  - DailyContext None handled (defaults applied via build_llm_context)
  - PremarketContext None handled (defaults applied)
  - Position dict mapped to flat LLMContext fields
  - todays_prior_decisions passed through unchanged
  - prompt_version from config.llm_prompt_version
  - Earnings flags wired from DayState
  - catalyst_flags hardcoded to ()
  - Quick coverage that build_llm_context's new last_5_daily_closes
    kwarg flows through to LLMContext (kwarg-level test)
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig
from data.replay.day_state import DayState, TickerDayState
from data.replay.historical_news import HistoricalNewsItem
from data.replay.market_context import MarketContextBundle
from data.replay.tick_context import (
    GAP_AND_GO_WINDOW_MINUTES,
    LLM_NEWS_LOOKBACK_HOURS,
    LLM_NEWS_MAX_ITEMS,
    _aggregate_5min,
    _minutes_since_open,
    _minutes_until_close,
    _spy_change_pct,
    _spy_rvol,
    _spy_slice_today_to_as_of,
    build_tick_context,
)
from data.replay.ticker_metadata import TickerMetadata
from strategy.llm.context_builder import build_llm_context

# Bring in DailyContext + PremarketContext for synthetic state.
from analysis.indicators import DailyContext, PremarketContext


ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides):
    kwargs = dict(
        start_date=date(2026, 4, 15),
        end_date=date(2026, 4, 15),
        tickers=("AAPL",),
        llm_prompt_version="v-test",
    )
    kwargs.update(overrides)
    return ReplayConfig(**kwargs)


def _make_1min_bars(
    trading_date: date,
    *,
    n_minutes: int = 60,
    start_hour: int = 9,
    start_minute: int = 30,
    base_close: float = 100.0,
    volume_per_bar: int = 1000,
    vwap_per_bar: float | None = None,
) -> pd.DataFrame:
    """Build n_minutes of synthetic 1-min bars indexed by tz-aware ET."""
    start = pd.Timestamp(
        trading_date, tz="America/New_York"
    ) + timedelta(hours=start_hour, minutes=start_minute)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n_minutes)])
    closes = [base_close + i * 0.01 for i in range(n_minutes)]
    vwaps = [v if (vwap_per_bar is None or False) else vwap_per_bar for v in closes]
    if vwap_per_bar is not None:
        vwaps = [vwap_per_bar] * n_minutes
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [volume_per_bar] * n_minutes,
            "vwap": vwaps,
            "trade_count": [5] * n_minutes,
        },
        index=idx,
    )


def _spy_5min_frame(
    trading_date: date,
    *,
    n_bars: int = 78,
    open_price: float = 500.0,
    drift: float = 0.0,
    volume_per_bar: int = 100_000,
) -> pd.DataFrame:
    """Build a SPY 5-min frame UTC-indexed (matches polygon_feed convention)."""
    start_et = pd.Timestamp(
        trading_date, tz="America/New_York"
    ) + timedelta(hours=9, minutes=30)
    idx = pd.DatetimeIndex(
        [(start_et + timedelta(minutes=5 * i)).tz_convert("UTC") for i in range(n_bars)]
    )
    closes = [open_price + drift * i for i in range(n_bars)]
    return pd.DataFrame(
        {
            "open": [open_price + drift * (i - 0.5) for i in range(n_bars)],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [volume_per_bar] * n_bars,
            "vwap": closes,
            "trade_count": [50] * n_bars,
        },
        index=idx,
    )


def _ticker_state(
    *,
    ticker: str = "AAPL",
    trading_date: date | None = None,
    daily_ctx_regime: str = "neutral",
    has_pm_ctx: bool = True,
    minute_bars: pd.DataFrame | None = None,
    news_items: list[HistoricalNewsItem] | None = None,
    last_5: tuple[float, ...] | None = None,
) -> TickerDayState:
    """Build a TickerDayState with realistic-ish synthetic state."""
    td = trading_date or date(2026, 4, 15)
    daily_ctx = DailyContext(
        ticker=ticker,
        last_close=100.0,
        sma_200=95.0,
        adx_14=22.0,
        regime=daily_ctx_regime,
        is_trending=True,
        daily_atr_14=2.0,
    )
    pm_ctx = (
        PremarketContext(
            ticker=ticker,
            prior_close=99.0,
            prior_high=101.0,
            prior_low=98.0,
            premarket_high=100.5,
            premarket_low=99.5,
            premarket_volume=500_000,
            premarket_rvol=2.0,
            is_unusual_volume=False,
            gap_pct=1.01,
            gap_atr_ratio=0.5,
        )
        if has_pm_ctx
        else None
    )
    return TickerDayState(
        ticker=ticker,
        minute_bars=minute_bars
        if minute_bars is not None
        else _make_1min_bars(td, n_minutes=120),
        daily_bars=pd.DataFrame(),  # not used by build_tick_context
        daily_context=daily_ctx,
        premarket_context=pm_ctx,
        ticker_metadata=TickerMetadata(
            ticker=ticker,
            sector="Information Technology",
            market_cap_bucket="mega",
            avg_daily_volume=50_000_000,
        ),
        news_items=news_items if news_items is not None else [],
        last_5_daily_closes=last_5 if last_5 is not None else (95.0, 96.0, 97.0, 98.0, 99.0),
    )


def _day_state(
    *,
    trading_date: date | None = None,
    tickers: dict[str, TickerDayState] | None = None,
    vix_level: float | None = 18.5,
    market_regime_label: str = "bull",
    sentiment_lookup: dict | None = None,
    failed_tickers: dict[str, str] | None = None,
    has_earnings_today: dict[str, bool] | None = None,
    has_earnings_within_3d: dict[str, bool] | None = None,
) -> DayState:
    td = trading_date or date(2026, 4, 15)
    tx = tickers or {"AAPL": _ticker_state(trading_date=td)}
    return DayState(
        trading_date=td,
        vix_level=vix_level,
        market_regime_label=market_regime_label,
        sentiment_lookup=sentiment_lookup if sentiment_lookup is not None else {},
        tickers=tx,
        failed_tickers=failed_tickers or {},
        has_earnings_today=has_earnings_today or {t: False for t in tx},
        has_earnings_within_3d=has_earnings_within_3d or {t: False for t in tx},
    )


def _market_ctx(
    *, spy_5min: pd.DataFrame | None = None, vix_daily=None
) -> MarketContextBundle:
    return MarketContextBundle(
        spy_5min=spy_5min if spy_5min is not None else _spy_5min_frame(date(2026, 4, 15)),
        spy_daily=pd.DataFrame(),  # not used by build_tick_context
        vix_daily=vix_daily,
    )


def _tick(h: int, m: int, day: date | None = None) -> datetime:
    return datetime(
        (day or date(2026, 4, 15)).year,
        (day or date(2026, 4, 15)).month,
        (day or date(2026, 4, 15)).day,
        h, m,
        tzinfo=ET,
    )


# ===========================================================================
# _aggregate_5min
# ===========================================================================


def test_aggregate_5min_empty_returns_empty():
    out = _aggregate_5min(pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap", "trade_count"]))
    assert out.empty


def test_aggregate_5min_basic_ohlcv():
    """Five 1-min bars should aggregate to one 5-min bar with standard reductions."""
    bars = _make_1min_bars(date(2026, 4, 15), n_minutes=5, base_close=100.0)
    # base_close=100.0, drift=0.01, so closes are 100.00, 100.01, ..., 100.04.
    out = _aggregate_5min(bars)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["open"] == pytest.approx(100.0)
    assert row["close"] == pytest.approx(100.04)
    assert row["high"] == pytest.approx(100.04 + 0.05)
    assert row["low"] == pytest.approx(100.0 - 0.05)
    assert row["volume"] == 5 * 1000
    assert row["trade_count"] == 5 * 5


def test_aggregate_5min_volume_weighted_vwap():
    """vwap aggregation is sum(vwap * volume) / sum(volume)."""
    # Two 1-min bars with different vwaps and volumes; check the
    # weighted mean math.
    start = pd.Timestamp(date(2026, 4, 15), tz="America/New_York") + timedelta(hours=9, minutes=30)
    df = pd.DataFrame(
        {
            "open": [100.0, 110.0],
            "high": [101.0, 111.0],
            "low": [99.0, 109.0],
            "close": [100.5, 110.5],
            "volume": [100, 200],
            "vwap": [100.0, 110.0],
            "trade_count": [1, 1],
        },
        index=pd.DatetimeIndex([start, start + timedelta(minutes=1)]),
    )
    out = _aggregate_5min(df)
    assert len(out) == 1
    # Weighted: (100*100 + 110*200) / (100+200) = 32000/300 ≈ 106.667
    assert out.iloc[0]["vwap"] == pytest.approx(106.6666666667)


def test_aggregate_5min_multi_window():
    """Ten 1-min bars produce two 5-min bars."""
    bars = _make_1min_bars(date(2026, 4, 15), n_minutes=10, base_close=100.0)
    out = _aggregate_5min(bars)
    assert len(out) == 2


# ===========================================================================
# _spy_change_pct
# ===========================================================================


def test_spy_change_pct_empty_returns_zero():
    assert _spy_change_pct(pd.DataFrame()) == 0.0


def test_spy_change_pct_positive():
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=10, open_price=500.0, drift=0.5)
    out = _spy_change_pct(spy)
    # First open ~499.75 (i=0 -> open_price + drift*(0-0.5)= -0.25), last close = 500 + 0.5*9 = 504.5
    # change ~= (504.5 - 499.75) / 499.75 * 100 ~= 0.95%
    assert out > 0.5
    assert out < 1.5


def test_spy_change_pct_negative():
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=10, open_price=500.0, drift=-0.5)
    out = _spy_change_pct(spy)
    assert out < 0


def test_spy_change_pct_zero_when_flat():
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=10, open_price=500.0, drift=0.0)
    # first_open == 500 - 0.5*drift == 500; last_close == 500.
    out = _spy_change_pct(spy)
    assert out == 0.0


def test_spy_change_pct_zero_when_first_open_nonpositive():
    """Defensive: degenerate open=0 returns 0.0 not div-by-zero."""
    start = pd.Timestamp(date(2026, 4, 15), tz="America/New_York") + timedelta(hours=9, minutes=30)
    spy = pd.DataFrame(
        {
            "open": [0.0],
            "high": [0.0],
            "low": [0.0],
            "close": [5.0],
            "volume": [100],
            "vwap": [0.0],
            "trade_count": [1],
        },
        index=pd.DatetimeIndex([start.tz_convert("UTC")]),
    )
    assert _spy_change_pct(spy) == 0.0


# ===========================================================================
# _spy_rvol
# ===========================================================================


def test_spy_rvol_empty_today_returns_one():
    assert _spy_rvol(pd.DataFrame(), pd.DataFrame()) == 1.0


def test_spy_rvol_normal_case():
    # All 20 trailing bars have volume=100; current bar has volume=200.
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=20, volume_per_bar=100)
    # Today slice: just the last bar with volume=200.
    today = spy.iloc[-1:].copy()
    today.iloc[0, today.columns.get_loc("volume")] = 200
    out = _spy_rvol(spy, today)
    assert out == pytest.approx(2.0)


def test_spy_rvol_baseline_zero_returns_one():
    """If the SPY frame has zero baseline volume, fall back to 1.0."""
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=5, volume_per_bar=0)
    today = spy.iloc[-1:].copy()
    today.iloc[0, today.columns.get_loc("volume")] = 100
    assert _spy_rvol(spy, today) == 1.0


def test_spy_rvol_partial_baseline_uses_mean_of_available():
    """Fewer than 20 bars in the trailing window; mean over what exists."""
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=5, volume_per_bar=100)
    today = spy.iloc[-1:].copy()
    today.iloc[0, today.columns.get_loc("volume")] = 150
    out = _spy_rvol(spy, today)
    assert out == pytest.approx(1.5)


# ===========================================================================
# _spy_slice_today_to_as_of
# ===========================================================================


def test_spy_slice_returns_bars_at_or_before_tick():
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=78)
    # Tick at 10:00 ET. SPY bars from 9:30, 9:35, ..., 9:55, 10:00.
    # That's 7 bars.
    out = _spy_slice_today_to_as_of(spy, _tick(10, 0))
    assert len(out) == 7


def test_spy_slice_empty_when_no_bars_for_date():
    spy = _spy_5min_frame(date(2026, 4, 15), n_bars=10)
    out = _spy_slice_today_to_as_of(spy, _tick(10, 0, day=date(2026, 4, 16)))
    assert out.empty


def test_spy_slice_empty_input_returns_empty():
    assert _spy_slice_today_to_as_of(pd.DataFrame(), _tick(10, 0)).empty


# ===========================================================================
# minutes_since_open / minutes_until_close
# ===========================================================================


def test_minutes_since_open_at_930_is_zero():
    assert _minutes_since_open(_tick(9, 30)) == 0


def test_minutes_since_open_at_1030_is_60():
    assert _minutes_since_open(_tick(10, 30)) == 60


def test_minutes_since_open_at_1200_is_150():
    assert _minutes_since_open(_tick(12, 0)) == 150


def test_minutes_since_open_at_1555_is_385():
    assert _minutes_since_open(_tick(15, 55)) == 385


def test_minutes_since_open_before_open_clamps_to_zero():
    assert _minutes_since_open(_tick(8, 0)) == 0


def test_minutes_since_open_after_close_clamps_to_390():
    assert _minutes_since_open(_tick(18, 0)) == 390


def test_minutes_until_close_at_930_is_390():
    assert _minutes_until_close(_tick(9, 30)) == 390


def test_minutes_until_close_at_1200_is_240():
    assert _minutes_until_close(_tick(12, 0)) == 240


def test_minutes_until_close_at_1555_is_5():
    assert _minutes_until_close(_tick(15, 55)) == 5


def test_minutes_until_close_after_close_clamps_to_zero():
    assert _minutes_until_close(_tick(17, 0)) == 0


# ===========================================================================
# build_tick_context: KeyError on dropped ticker
# ===========================================================================


def test_build_tick_context_keyerror_when_ticker_dropped():
    ds = _day_state(
        tickers={"AAPL": _ticker_state(ticker="AAPL")},
        failed_tickers={"NVDA": "minute bars 503"},
    )
    with pytest.raises(KeyError):
        build_tick_context(
            day_state=ds,
            market_ctx=_market_ctx(),
            config=_config(),
            ticker="NVDA",
            tick_et=_tick(10, 0),
        )


# ===========================================================================
# build_tick_context: happy path
# ===========================================================================


def test_build_tick_context_happy_path_basic():
    ds = _day_state()
    mc = _market_ctx()
    cfg = _config()
    ctx = build_tick_context(
        day_state=ds,
        market_ctx=mc,
        config=cfg,
        ticker="AAPL",
        tick_et=_tick(10, 0),
    )
    # Identity fields
    assert ctx.ticker == "AAPL"
    assert ctx.prompt_version == "v-test"
    assert "10:00" in ctx.timestamp_et
    # Day-level fields wired from DayState
    assert ctx.vix_level == 18.5
    assert ctx.market_regime_label == "bull"
    # Per-ticker fundamentals from TickerMetadata
    assert ctx.sector == "Information Technology"
    assert ctx.market_cap_bucket == "mega"
    assert ctx.avg_daily_volume == 50_000_000
    # Time-of-day
    assert ctx.minutes_since_open == 30
    assert ctx.minutes_until_close == 360
    assert ctx.in_gap_and_go_window is True
    # last_5_daily_closes passthrough (the new build_llm_context kwarg)
    assert ctx.last_5_daily_closes == (95.0, 96.0, 97.0, 98.0, 99.0)
    # Premarket fields wired from PremarketContext
    assert ctx.pm_rvol == pytest.approx(2.0)
    assert ctx.gap_pct == pytest.approx(1.01)


# ===========================================================================
# build_tick_context: in_gap_and_go_window boundaries
# ===========================================================================


def test_in_gap_and_go_window_at_930_true():
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(9, 30),
    )
    assert ctx.in_gap_and_go_window is True
    assert ctx.minutes_since_open == 0


def test_in_gap_and_go_window_at_1030_true_boundary():
    """60 minutes after open is inclusive of the gap-and-go window."""
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 30),
    )
    assert ctx.in_gap_and_go_window is True
    assert ctx.minutes_since_open == GAP_AND_GO_WINDOW_MINUTES


def test_in_gap_and_go_window_at_1031_false():
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 31),
    )
    assert ctx.in_gap_and_go_window is False


def test_in_gap_and_go_window_at_1555_false():
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(15, 55),
    )
    assert ctx.in_gap_and_go_window is False


# ===========================================================================
# build_tick_context: news filtering + cap
# ===========================================================================


def _news(ticker: str, ts_et: datetime, article_id: str, headline: str = "h") -> HistoricalNewsItem:
    return HistoricalNewsItem(
        ts_et=ts_et,
        ticker=ticker,
        headline=headline,
        source="polygon",
        polygon_article_id=article_id,
    )


def test_news_filtered_by_lag_seconds():
    """Item published less than news_lag_seconds before tick is hidden."""
    tick = _tick(10, 0)
    visible_item = _news("AAPL", tick - timedelta(seconds=30), "a-visible")
    hidden_item = _news("AAPL", tick - timedelta(seconds=10), "a-hidden")
    state = _ticker_state(news_items=[visible_item, hidden_item])
    ds = _day_state(tickers={"AAPL": state})
    cfg = _config()
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=cfg, ticker="AAPL", tick_et=tick,
    )
    # cfg.news_lag_seconds default is 30. visible is at exactly -30s
    # (ts+lag == as_of, boundary inclusive). hidden at -10s is excluded.
    headlines = [n["headline"] for n in ctx.news_items]
    assert "h" in headlines
    assert len(ctx.news_items) == 1


def test_news_filtered_by_lookback_24h():
    """Item older than 24h is excluded from the LLM context."""
    tick = _tick(10, 0)
    too_old = _news(
        "AAPL", tick - timedelta(hours=25), "old",
        headline="too old",
    )
    in_window = _news(
        "AAPL", tick - timedelta(hours=23), "fresh",
        headline="fresh",
    )
    state = _ticker_state(news_items=[too_old, in_window])
    ds = _day_state(tickers={"AAPL": state})
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=tick,
    )
    headlines = [n["headline"] for n in ctx.news_items]
    assert "fresh" in headlines
    assert "too old" not in headlines


def test_news_capped_at_top_5_most_recent():
    """8 visible items -> only the 5 most recent appear in news_items."""
    tick = _tick(15, 0)
    items = []
    for i in range(8):
        items.append(_news(
            "AAPL",
            tick - timedelta(hours=8 - i),  # oldest first per build_day_state's sort
            f"id-{i}",
            headline=f"h{i}",
        ))
    state = _ticker_state(news_items=items)
    ds = _day_state(tickers={"AAPL": state})
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=tick,
    )
    assert len(ctx.news_items) == LLM_NEWS_MAX_ITEMS
    # Top 5 most recent = items 3..7. Newest (h7) should be in the
    # output; oldest in-window (h0) should NOT.
    headlines = [n["headline"] for n in ctx.news_items]
    assert "h7" in headlines
    assert "h0" not in headlines


def test_news_sentiment_lookup_wired():
    """items_to_context_dicts receives day_state.sentiment_lookup."""
    tick = _tick(10, 0)
    item = _news("AAPL", tick - timedelta(minutes=10), "a1")
    state = _ticker_state(news_items=[item])
    ds = _day_state(
        tickers={"AAPL": state},
        sentiment_lookup={("a1", "AAPL"): 7.0},
    )
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=tick,
    )
    assert ctx.news_items[0]["sentiment_score"] == 7.0


# ===========================================================================
# build_tick_context: defensive None handling
# ===========================================================================


def test_vix_level_none_flows_through():
    ds = _day_state(vix_level=None)
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
    )
    assert ctx.vix_level is None


def test_daily_context_none_uses_defaults():
    state = _ticker_state()
    state_none = TickerDayState(
        ticker=state.ticker,
        minute_bars=state.minute_bars,
        daily_bars=state.daily_bars,
        daily_context=None,
        premarket_context=state.premarket_context,
        ticker_metadata=state.ticker_metadata,
        news_items=state.news_items,
        last_5_daily_closes=state.last_5_daily_closes,
    )
    ds = _day_state(tickers={"AAPL": state_none})
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
    )
    # build_llm_context defaults: daily_regime=neutral, sma_200=0
    assert ctx.daily_regime == "neutral"
    assert ctx.sma_200 == 0.0


def test_premarket_context_none_uses_defaults():
    state = _ticker_state(has_pm_ctx=False)
    ds = _day_state(tickers={"AAPL": state})
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
    )
    # build_llm_context defaults: gap_pct=0, pm_rvol=0
    assert ctx.gap_pct == 0.0
    assert ctx.pm_rvol == 0.0


# ===========================================================================
# build_tick_context: position passthrough
# ===========================================================================


def test_position_dict_mapped_to_flat_fields():
    pos = {
        "qty": 100,
        "avg_price": 175.5,
        "unrealized_pl_pct": 1.2,
        "has_active_stop": True,
    }
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
        position=pos,
    )
    assert ctx.currently_holding is True
    assert ctx.position_qty == 100
    assert ctx.position_avg_price == 175.5
    assert ctx.position_unrealized_pl_pct == 1.2
    assert ctx.has_active_stop is True


def test_position_none_yields_flat_defaults():
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
        position=None,
    )
    assert ctx.currently_holding is False
    assert ctx.position_qty == 0


# ===========================================================================
# build_tick_context: prior decisions + earnings flags + prompt version
# ===========================================================================


def test_todays_prior_decisions_passed_through():
    decisions = (
        {"ts": "2026-04-15T09:35:00", "action": "Hold", "setup_label": "wait", "confidence": 30},
    )
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
        todays_prior_decisions=decisions,
    )
    assert ctx.todays_prior_decisions == decisions


def test_prompt_version_from_config():
    cfg = _config(llm_prompt_version="v9.99-custom")
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=cfg, ticker="AAPL", tick_et=_tick(10, 0),
    )
    assert ctx.prompt_version == "v9.99-custom"


def test_earnings_flags_wired_from_day_state():
    ds = _day_state(
        has_earnings_today={"AAPL": True},
        has_earnings_within_3d={"AAPL": True},
    )
    ctx = build_tick_context(
        day_state=ds, market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
    )
    assert ctx.has_earnings_today is True
    assert ctx.has_earnings_within_3d is True


def test_catalyst_flags_hardcoded_empty():
    """Until a dedicated catalyst classifier lands, the LLM context
    never carries catalyst_flags."""
    ctx = build_tick_context(
        day_state=_day_state(), market_ctx=_market_ctx(),
        config=_config(), ticker="AAPL", tick_et=_tick(10, 0),
    )
    assert ctx.catalyst_flags == ()


# ===========================================================================
# build_llm_context kwarg-flow sanity test
# ===========================================================================


def test_build_llm_context_last_5_daily_closes_kwarg_flows_through():
    """Direct test of the new kwarg on build_llm_context. Defaults to ()
    when unspecified; populated when passed."""
    df = pd.DataFrame()  # empty df_ind is OK for this assertion
    ctx_default = build_llm_context(
        ticker="AAPL",
        timestamp_et="2026-04-15T10:00:00",
        prompt_version="v-test",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
    )
    assert ctx_default.last_5_daily_closes == ()

    ctx_passed = build_llm_context(
        ticker="AAPL",
        timestamp_et="2026-04-15T10:00:00",
        prompt_version="v-test",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
        last_5_daily_closes=(100.0, 101.0, 102.0, 103.0, 104.0),
    )
    assert ctx_passed.last_5_daily_closes == (100.0, 101.0, 102.0, 103.0, 104.0)
