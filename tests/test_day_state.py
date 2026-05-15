"""Tests for data/replay/day_state.py (M2.2 sub-task #9).

Covers:
  - Happy path (2 tickers, all loaders return valid data, all fields
    populated correctly)
  - Per-ticker drop-and-continue: bars failure / daily failure /
    metadata failure each drop the ticker into DayState.failed_tickers
    with a WARNING; other tickers unaffected
  - All-tickers-fail raises RuntimeError
  - News batch failure raises RuntimeError (news is a universal
    LLMContext input)
  - vix_daily=None on the MarketContextBundle -> DayState.vix_level=None
  - VIX .asof() picks the most recent prior close
  - market_regime_label thresholds: bull when SPY close > sma_200*1.005,
    bear when < sma_200*0.995, neutral in between, unknown when SPY
    history < 200 bars
  - DailyContext is None when per-ticker daily bars < 200
  - PremarketContext is None when no PM bars on trading_date
  - PM volume baseline absence logs WARNING (premarket_rvol falls to 0)
  - last_5_daily_closes correctness (oldest first, from trailing daily)
  - Sentiment lookup called once with union of all surviving tickers'
    news items
  - news_items on TickerDayState are unfiltered (lag is the tick loop's
    concern)
  - Earnings flags hardcoded False (defensive)
  - sentiment_conn is NOT closed by build_day_state
  - trading_date wired through to the returned DayState
  - Concurrency cap is honored
  - Validation: empty tickers raises ValueError; concurrency<1 raises
  - Helper-level tests for _derive_pm_volume_history and _resolve_vix_level
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, '.')

from data.replay.config import ReplayConfig
from data.replay import day_state as ds
from data.replay.day_state import (
    DEFAULT_DAY_PREP_CONCURRENCY,
    DayState,
    TickerDayState,
    _derive_pm_volume_history,
    _last_5_daily_closes,
    _resolve_vix_level,
    build_day_state,
)
from data.replay.historical_news import HistoricalNewsItem
from data.replay.market_context import MarketContextBundle
from data.replay.ticker_metadata import TickerMetadata


ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------


def _config() -> ReplayConfig:
    """Minimal ReplayConfig for tests."""
    return ReplayConfig(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        tickers=("AAPL", "NVDA"),
        llm_prompt_version="v-test",
    )


def _daily_bars(
    end_date: date,
    *,
    n_days: int = 220,
    base_close: float = 100.0,
    drift: float = 0.0,
) -> pd.DataFrame:
    """Build daily OHLCV bars indexed by ET midnight.

    n_days bars ending on end_date inclusive. ``drift`` controls per-bar
    close drift; with drift=0 the closes are flat (lets compute_daily_context
    return regime='neutral').
    """
    idx_dates = [end_date - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize("America/New_York") for d in idx_dates]
    )
    closes = np.array([base_close + drift * i for i in range(n_days)])
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": [1_000_000] * n_days,
            "vwap": closes,
            "trade_count": [100] * n_days,
        },
        index=idx,
    )


def _minute_bars(
    trading_date: date,
    *,
    n_cal_days_back: int = 25,
    include_pm: bool = True,
    pm_volume_per_bar: int = 500,
) -> pd.DataFrame:
    """Build synthetic 1-min bars covering n_cal_days_back through trading_date.

    Each trading day (Mon-Fri) gets PM bars from 04:00-09:29 ET (if
    include_pm), RTH bars 09:30-15:59 ET, no AH. Weekends absent.
    """
    start = trading_date - timedelta(days=n_cal_days_back)
    timestamps: list[pd.Timestamp] = []
    volumes: list[int] = []
    closes: list[float] = []
    cur = start
    while cur <= trading_date:
        if cur.weekday() < 5:  # Mon-Fri
            day_dt = pd.Timestamp(cur, tz="America/New_York")
            if include_pm:
                # PM session 04:00-09:29 (5.5 hours * 60 = 330 minutes)
                for minute in range(4 * 60, 9 * 60 + 30):
                    ts = day_dt + timedelta(minutes=minute)
                    timestamps.append(ts)
                    volumes.append(pm_volume_per_bar)
                    closes.append(100.0)
            # RTH session 09:30-15:59 (6.5 hours * 60 = 390 minutes)
            for minute in range(9 * 60 + 30, 16 * 60):
                ts = day_dt + timedelta(minutes=minute)
                timestamps.append(ts)
                volumes.append(2000)
                closes.append(100.0)
        cur += timedelta(days=1)
    if not timestamps:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "vwap", "trade_count"]
        )
    idx = pd.DatetimeIndex(timestamps)
    n = len(timestamps)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": volumes,
            "vwap": closes,
            "trade_count": [10] * n,
        },
        index=idx,
    )


def _vix_daily(end_date: date, *, n_days: int = 220, level: float = 18.0) -> pd.DataFrame:
    """Build a synthetic FRED VIXCLS daily frame with one column ``vix_close``."""
    idx_dates = [end_date - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize("UTC") for d in idx_dates]
    )
    return pd.DataFrame({"vix_close": [level] * n_days}, index=idx)


def _market_ctx(
    *,
    spy_daily: pd.DataFrame | None = None,
    vix_daily: pd.DataFrame | None = None,
    spy_5min: pd.DataFrame | None = None,
) -> MarketContextBundle:
    """Build a MarketContextBundle for tests."""
    return MarketContextBundle(
        spy_5min=spy_5min if spy_5min is not None else pd.DataFrame(),
        spy_daily=spy_daily if spy_daily is not None else _daily_bars(date(2026, 4, 14)),
        vix_daily=vix_daily,
    )


def _sentiment_conn() -> sqlite3.Connection:
    """In-memory sentiment fixture mirroring the live schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE sentiment ("
        "news_id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, "
        "sentiment INTEGER NOT NULL, reasoning TEXT, headline TEXT, "
        "scored_at REAL NOT NULL)"
    )
    return conn


def _metadata(ticker: str = "AAPL") -> TickerMetadata:
    return TickerMetadata(
        ticker=ticker,
        sector="Information Technology",
        market_cap_bucket="mega",
        avg_daily_volume=50_000_000,
    )


def _install_default_loader_stubs(
    monkeypatch,
    *,
    daily_bars: pd.DataFrame | None = None,
    minute_bars: pd.DataFrame | None = None,
    metadata: TickerMetadata | None = None,
    news_by_ticker: dict[str, list[HistoricalNewsItem]] | None = None,
    sentiment_lookup_result: dict[tuple[str, str], float] | None = None,
    per_ticker_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list]:
    """Patch every loader build_day_state imports. Returns a calls dict.

    ``per_ticker_overrides`` maps ticker -> {loader_name: value-or-exception}
    so individual tests can rig one ticker's loader to raise without
    affecting the others.
    """
    overrides = per_ticker_overrides or {}
    calls: dict[str, list] = {
        "minute": [],
        "daily": [],
        "metadata": [],
        "news": [],
        "sentiment": [],
    }

    default_daily = daily_bars if daily_bars is not None else _daily_bars(date(2026, 4, 14))
    default_minute = minute_bars if minute_bars is not None else _minute_bars(date(2026, 4, 15))
    default_meta = metadata if metadata is not None else _metadata()

    async def fake_load_minute(ticker, start_date, end_date):
        calls["minute"].append((ticker, start_date, end_date))
        ov = overrides.get(ticker, {}).get("minute")
        if isinstance(ov, BaseException):
            raise ov
        if ov is not None:
            return ov
        return default_minute

    async def fake_load_daily(ticker, start_date, end_date):
        calls["daily"].append((ticker, start_date, end_date))
        ov = overrides.get(ticker, {}).get("daily")
        if isinstance(ov, BaseException):
            raise ov
        if ov is not None:
            return ov
        return default_daily

    async def fake_get_metadata(ticker, as_of, *, cache_path=None):
        calls["metadata"].append((ticker, as_of))
        ov = overrides.get(ticker, {}).get("metadata")
        if isinstance(ov, BaseException):
            raise ov
        if ov is not None:
            return ov
        return _metadata(ticker=ticker)

    async def fake_load_news(tickers, start_date, end_date, **kw):
        calls["news"].append((tuple(tickers), start_date, end_date))
        ov = news_by_ticker
        if isinstance(ov, BaseException):
            raise ov
        if ov is not None:
            return ov
        return {t: [] for t in tickers}

    def fake_lookup_sentiment(conn, items):
        calls["sentiment"].append((conn, list(items)))
        if sentiment_lookup_result is not None:
            return sentiment_lookup_result
        return {}

    monkeypatch.setattr(
        "data.replay.day_state.load_historical_bars_1min", fake_load_minute
    )
    monkeypatch.setattr(
        "data.replay.day_state.load_historical_bars_daily", fake_load_daily
    )
    monkeypatch.setattr(
        "data.replay.day_state.get_ticker_metadata", fake_get_metadata
    )
    monkeypatch.setattr(
        "data.replay.day_state.load_historical_news", fake_load_news
    )
    monkeypatch.setattr(
        "data.replay.day_state.lookup_article_sentiments",
        fake_lookup_sentiment,
    )
    return calls


def _install_news_failure(monkeypatch, exc: Exception) -> None:
    """Patch only the news loader to raise."""
    async def fake_load_news(tickers, start_date, end_date, **kw):
        raise exc
    monkeypatch.setattr(
        "data.replay.day_state.load_historical_news", fake_load_news
    )


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


def test_last_5_daily_closes_empty_returns_empty_tuple():
    assert _last_5_daily_closes(pd.DataFrame()) == ()


def test_last_5_daily_closes_returns_oldest_first():
    df = _daily_bars(date(2026, 4, 14), n_days=10, base_close=100.0, drift=1.0)
    out = _last_5_daily_closes(df)
    assert isinstance(out, tuple)
    assert len(out) == 5
    # n_days=10, drift=1.0: closes 100, 101, ..., 109. Last 5 are 105-109.
    assert out == (105.0, 106.0, 107.0, 108.0, 109.0)


def test_last_5_daily_closes_fewer_than_5_returns_all():
    df = _daily_bars(date(2026, 4, 14), n_days=3, base_close=100.0, drift=2.0)
    out = _last_5_daily_closes(df)
    assert out == (100.0, 102.0, 104.0)


def test_derive_pm_volume_history_empty_returns_empty():
    assert _derive_pm_volume_history(pd.DataFrame(), date(2026, 4, 15)) == []


def test_derive_pm_volume_history_excludes_trading_date():
    """Today's PM bars must not be counted as a historical baseline."""
    minute_df = _minute_bars(date(2026, 4, 15), n_cal_days_back=5, pm_volume_per_bar=600)
    out = _derive_pm_volume_history(minute_df, date(2026, 4, 15))
    # 5 cal days back + trading_date itself. Today excluded. Expect
    # >0 entries from the trailing weekdays.
    assert all(v > 0 for v in out)
    # Each weekday's PM session is 330 min * 600 vol = 198000. So every
    # entry should equal that.
    assert all(v == 198_000 for v in out)


def test_derive_pm_volume_history_filters_to_pm_window():
    """RTH bars must NOT be counted in the PM baseline."""
    minute_df = _minute_bars(date(2026, 4, 15), n_cal_days_back=3, include_pm=False)
    # No PM bars at all -> empty baseline.
    out = _derive_pm_volume_history(minute_df, date(2026, 4, 15))
    assert out == []


def test_resolve_vix_level_none_when_bundle_none():
    assert _resolve_vix_level(None, date(2026, 4, 15)) is None


def test_resolve_vix_level_returns_asof_match():
    vix = _vix_daily(date(2026, 4, 14), n_days=10, level=22.5)
    assert _resolve_vix_level(vix, date(2026, 4, 15)) == 22.5


def test_resolve_vix_level_returns_none_when_no_data_before():
    """A trading_date before all vix data -> None (.asof returns NaN)."""
    vix = _vix_daily(date(2026, 6, 1), n_days=5, level=20.0)
    assert _resolve_vix_level(vix, date(2026, 1, 1)) is None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_day_state_empty_tickers_raises():
    cfg = _config()
    ctx = _market_ctx()
    conn = _sentiment_conn()
    with pytest.raises(ValueError, match="at least one ticker"):
        await build_day_state(cfg, date(2026, 4, 15), (), ctx, conn)


@pytest.mark.asyncio
async def test_build_day_state_concurrency_zero_raises():
    cfg = _config()
    ctx = _market_ctx()
    conn = _sentiment_conn()
    with pytest.raises(ValueError, match="concurrency"):
        await build_day_state(
            cfg, date(2026, 4, 15), ("AAPL",), ctx, conn, concurrency=0
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_day_state_happy_path_basic(monkeypatch):
    cfg = _config()
    trading_date = date(2026, 4, 15)
    calls = _install_default_loader_stubs(monkeypatch)
    ctx = _market_ctx()
    conn = _sentiment_conn()

    state = await build_day_state(
        cfg, trading_date, ("AAPL", "NVDA"), ctx, conn
    )

    assert isinstance(state, DayState)
    assert state.trading_date == trading_date
    assert set(state.tickers.keys()) == {"AAPL", "NVDA"}
    assert state.failed_tickers == {}
    for t in ("AAPL", "NVDA"):
        tds = state.tickers[t]
        assert isinstance(tds, TickerDayState)
        assert tds.ticker == t
        assert not tds.minute_bars.empty
        assert not tds.daily_bars.empty
        # The synthetic daily bars are flat-priced -> DailyContext.regime=neutral
        assert tds.daily_context is not None
        assert tds.daily_context.regime == "neutral"
        # PremarketContext should construct successfully on synthetic data
        assert tds.premarket_context is not None
        assert tds.ticker_metadata.ticker == t
        assert tds.news_items == []
        assert len(tds.last_5_daily_closes) == 5
    # Sentiment lookup was called once for the union (here, empty union).
    assert len(calls["sentiment"]) == 1
    # News was called once for the union of surviving tickers.
    assert len(calls["news"]) == 1


# ---------------------------------------------------------------------------
# Per-ticker drop-and-continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minute_bars_failure_drops_only_that_ticker(monkeypatch, caplog):
    cfg = _config()
    trading_date = date(2026, 4, 15)
    _install_default_loader_stubs(
        monkeypatch,
        per_ticker_overrides={
            "NVDA": {"minute": RuntimeError("polygon 503 simulated")}
        },
    )
    ctx = _market_ctx()
    conn = _sentiment_conn()

    with caplog.at_level("WARNING", logger="data.replay.day_state"):
        state = await build_day_state(
            cfg, trading_date, ("AAPL", "NVDA"), ctx, conn
        )

    assert set(state.tickers.keys()) == {"AAPL"}
    assert "NVDA" in state.failed_tickers
    assert "polygon 503" in state.failed_tickers["NVDA"]
    assert any("dropping NVDA" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_daily_bars_failure_drops_only_that_ticker(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(
        monkeypatch,
        per_ticker_overrides={
            "AAPL": {"daily": RuntimeError("daily fetch failed")}
        },
    )
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "NVDA"), _market_ctx(), _sentiment_conn()
    )
    assert "AAPL" not in state.tickers
    assert "NVDA" in state.tickers
    assert "AAPL" in state.failed_tickers


@pytest.mark.asyncio
async def test_metadata_failure_drops_only_that_ticker(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(
        monkeypatch,
        per_ticker_overrides={
            "TSLA": {"metadata": RuntimeError("ref lookup failed")}
        },
    )
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "TSLA"), _market_ctx(), _sentiment_conn()
    )
    assert "TSLA" not in state.tickers
    assert "AAPL" in state.tickers
    assert "TSLA" in state.failed_tickers


@pytest.mark.asyncio
async def test_all_tickers_fail_raises(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(
        monkeypatch,
        per_ticker_overrides={
            "AAPL": {"minute": RuntimeError("a fail")},
            "NVDA": {"minute": RuntimeError("b fail")},
        },
    )
    with pytest.raises(RuntimeError, match="every ticker failed"):
        await build_day_state(
            cfg, date(2026, 4, 15), ("AAPL", "NVDA"),
            _market_ctx(), _sentiment_conn(),
        )


# ---------------------------------------------------------------------------
# News batch failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_batch_failure_raises_runtime_error(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    _install_news_failure(monkeypatch, RuntimeError("polygon news outage"))

    with pytest.raises(RuntimeError, match="news batch failed"):
        await build_day_state(
            cfg, date(2026, 4, 15), ("AAPL",),
            _market_ctx(), _sentiment_conn(),
        )


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vix_daily_none_yields_none_vix_level(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    ctx = _market_ctx(vix_daily=None)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.vix_level is None


@pytest.mark.asyncio
async def test_vix_level_populated_when_bundle_has_vix(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    ctx = _market_ctx(vix_daily=_vix_daily(date(2026, 4, 14), level=27.5))
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.vix_level == 27.5


# ---------------------------------------------------------------------------
# market_regime_label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_regime_bull(monkeypatch):
    """SPY close 10% above its 200 SMA -> bull."""
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    # 220 bars rising; last close well above the 200-bar average.
    spy = _daily_bars(date(2026, 4, 14), n_days=220, base_close=100.0, drift=0.5)
    ctx = _market_ctx(spy_daily=spy)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.market_regime_label == "bull"


@pytest.mark.asyncio
async def test_market_regime_bear(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    # 220 bars falling.
    spy = _daily_bars(date(2026, 4, 14), n_days=220, base_close=200.0, drift=-0.5)
    ctx = _market_ctx(spy_daily=spy)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.market_regime_label == "bear"


@pytest.mark.asyncio
async def test_market_regime_neutral(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    # Flat closes -> last_close == sma_200; lands in neutral band.
    spy = _daily_bars(date(2026, 4, 14), n_days=220, base_close=100.0, drift=0.0)
    ctx = _market_ctx(spy_daily=spy)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.market_regime_label == "neutral"


@pytest.mark.asyncio
async def test_market_regime_unknown_when_spy_short(monkeypatch):
    """SPY with <200 daily bars before trading_date -> regime=unknown."""
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    spy = _daily_bars(date(2026, 4, 14), n_days=50)
    ctx = _market_ctx(spy_daily=spy)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), ctx, _sentiment_conn()
    )
    assert state.market_regime_label == "unknown"


# ---------------------------------------------------------------------------
# Daily / Premarket context None paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_context_none_when_few_daily_bars(monkeypatch):
    """Per-ticker DailyContext is None when <200 daily bars."""
    cfg = _config()
    short_daily = _daily_bars(date(2026, 4, 14), n_days=50)
    _install_default_loader_stubs(monkeypatch, daily_bars=short_daily)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",),
        _market_ctx(), _sentiment_conn(),
    )
    assert state.tickers["AAPL"].daily_context is None


@pytest.mark.asyncio
async def test_premarket_context_none_when_no_minute_bars_on_date(monkeypatch):
    """When the minute frame has no rows for trading_date,
    compute_premarket_context returns None (today_open undefined)."""
    cfg = _config()
    # Minute bars only for days BEFORE trading_date.
    minute_df = _minute_bars(date(2026, 4, 10), n_cal_days_back=10)
    _install_default_loader_stubs(monkeypatch, minute_bars=minute_df)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",),
        _market_ctx(), _sentiment_conn(),
    )
    # No bars on trading_date 2026-04-15 means today's open can't be
    # determined and pm_volume baseline derivation also has nothing
    # for today. compute_premarket_context returns None.
    assert state.tickers["AAPL"].premarket_context is None


@pytest.mark.asyncio
async def test_premarket_pm_volume_baseline_zero_with_warning(monkeypatch, caplog):
    """No PM bars in the trailing window -> WARNING logged, premarket_rvol=0."""
    cfg = _config()
    # Minute bars cover the period but have no PM session (RTH only).
    minute_df = _minute_bars(date(2026, 4, 15), n_cal_days_back=25, include_pm=False)
    _install_default_loader_stubs(monkeypatch, minute_bars=minute_df)
    with caplog.at_level("WARNING", logger="data.replay.day_state"):
        state = await build_day_state(
            cfg, date(2026, 4, 15), ("AAPL",),
            _market_ctx(), _sentiment_conn(),
        )
    assert any("PM volume baseline" in r.message for r in caplog.records)
    # premarket_context can still construct (today's RTH bars exist),
    # but premarket_rvol is 0.0.
    pm = state.tickers["AAPL"].premarket_context
    assert pm is not None
    assert pm.premarket_rvol == 0.0


# ---------------------------------------------------------------------------
# News / sentiment wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_items_wired_per_ticker_unfiltered(monkeypatch):
    """build_day_state passes the full day's news through to TickerDayState
    without applying the tick-level lag filter (that's the tick loop's job)."""
    cfg = _config()
    aapl_items = [
        HistoricalNewsItem(
            ts_et=datetime(2026, 4, 15, 9, 35, tzinfo=ET),
            ticker="AAPL", headline="aapl 0935", source="polygon",
            polygon_article_id="a1",
        ),
        HistoricalNewsItem(
            ts_et=datetime(2026, 4, 15, 14, 0, tzinfo=ET),
            ticker="AAPL", headline="aapl 1400", source="polygon",
            polygon_article_id="a2",
        ),
    ]
    nvda_items = [
        HistoricalNewsItem(
            ts_et=datetime(2026, 4, 15, 7, 0, tzinfo=ET),
            ticker="NVDA", headline="nvda 0700 pm", source="polygon",
            polygon_article_id="n1",
        ),
    ]
    _install_default_loader_stubs(
        monkeypatch,
        news_by_ticker={"AAPL": aapl_items, "NVDA": nvda_items},
    )
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "NVDA"),
        _market_ctx(), _sentiment_conn(),
    )
    assert [it.headline for it in state.tickers["AAPL"].news_items] == [
        "aapl 0935", "aapl 1400"
    ]
    assert [it.headline for it in state.tickers["NVDA"].news_items] == [
        "nvda 0700 pm"
    ]


@pytest.mark.asyncio
async def test_sentiment_lookup_called_once_with_union(monkeypatch):
    """One sentiment lookup over the union of all surviving tickers'
    news items, returned at DayState level."""
    cfg = _config()
    aapl_items = [
        HistoricalNewsItem(
            ts_et=datetime(2026, 4, 15, 9, 35, tzinfo=ET),
            ticker="AAPL", headline="h", source="polygon",
            polygon_article_id="a1",
        ),
    ]
    nvda_items = [
        HistoricalNewsItem(
            ts_et=datetime(2026, 4, 15, 7, 0, tzinfo=ET),
            ticker="NVDA", headline="h", source="polygon",
            polygon_article_id="n1",
        ),
    ]
    calls = _install_default_loader_stubs(
        monkeypatch,
        news_by_ticker={"AAPL": aapl_items, "NVDA": nvda_items},
        sentiment_lookup_result={
            ("a1", "AAPL"): 4.0,
            ("n1", "NVDA"): -2.0,
        },
    )
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "NVDA"),
        _market_ctx(), _sentiment_conn(),
    )
    # Sentiment was called exactly once.
    assert len(calls["sentiment"]) == 1
    # The single call's items list is the union (2 items).
    assert len(calls["sentiment"][0][1]) == 2
    # DayState carries the lookup at the day level.
    assert state.sentiment_lookup == {
        ("a1", "AAPL"): 4.0,
        ("n1", "NVDA"): -2.0,
    }


@pytest.mark.asyncio
async def test_news_loader_called_only_for_surviving_tickers(monkeypatch):
    """If one ticker fails the loader fan-out, news is fetched only for the
    survivors (no point asking for news on a dropped ticker)."""
    cfg = _config()
    calls = _install_default_loader_stubs(
        monkeypatch,
        per_ticker_overrides={
            "NVDA": {"minute": RuntimeError("dropped")},
        },
    )
    await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "NVDA"),
        _market_ctx(), _sentiment_conn(),
    )
    assert len(calls["news"]) == 1
    seen_tickers = calls["news"][0][0]
    assert seen_tickers == ("AAPL",)


# ---------------------------------------------------------------------------
# Connection lifecycle + earnings flags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentiment_conn_not_closed_after_call(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    conn = _sentiment_conn()
    await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL",), _market_ctx(), conn
    )
    # If it were closed, this would raise sqlite3.ProgrammingError.
    conn.execute("SELECT 1").fetchone()


@pytest.mark.asyncio
async def test_earnings_flags_hardcoded_false(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    state = await build_day_state(
        cfg, date(2026, 4, 15), ("AAPL", "NVDA"),
        _market_ctx(), _sentiment_conn(),
    )
    assert state.has_earnings_today == {"AAPL": False, "NVDA": False}
    assert state.has_earnings_within_3d == {"AAPL": False, "NVDA": False}


@pytest.mark.asyncio
async def test_trading_date_wired_through(monkeypatch):
    cfg = _config()
    _install_default_loader_stubs(monkeypatch)
    td = date(2026, 4, 22)
    state = await build_day_state(
        cfg, td, ("AAPL",), _market_ctx(), _sentiment_conn()
    )
    assert state.trading_date == td


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_caps_in_flight(monkeypatch):
    """6 tickers, concurrency=2 -> peak in-flight per-ticker tasks <= 2."""
    import asyncio as _asyncio

    cfg = _config()
    in_flight = {"now": 0, "peak": 0}
    daily = _daily_bars(date(2026, 4, 14))
    minute = _minute_bars(date(2026, 4, 15))

    async def fake_minute(ticker, *a, **kw):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await _asyncio.sleep(0)  # yield so concurrency can race
        in_flight["now"] -= 1
        return minute

    async def fake_daily(ticker, *a, **kw):
        await _asyncio.sleep(0)
        return daily

    async def fake_meta(ticker, *a, **kw):
        await _asyncio.sleep(0)
        return _metadata(ticker=ticker)

    async def fake_news(tickers, *a, **kw):
        return {t: [] for t in tickers}

    def fake_sentiment(conn, items):
        return {}

    monkeypatch.setattr("data.replay.day_state.load_historical_bars_1min", fake_minute)
    monkeypatch.setattr("data.replay.day_state.load_historical_bars_daily", fake_daily)
    monkeypatch.setattr("data.replay.day_state.get_ticker_metadata", fake_meta)
    monkeypatch.setattr("data.replay.day_state.load_historical_news", fake_news)
    monkeypatch.setattr("data.replay.day_state.lookup_article_sentiments", fake_sentiment)

    await build_day_state(
        cfg, date(2026, 4, 15),
        ("A", "B", "C", "D", "E", "F"),
        _market_ctx(), _sentiment_conn(),
        concurrency=2,
    )
    assert in_flight["peak"] <= 2
