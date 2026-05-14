"""Tests for strategy/llm/context_builder.py.

Covers the four pure builders that turn orchestrator state into
policy.py / signal_engine inputs:

  - build_llm_context: assembles LLMContext from per-symbol state +
    indicator DataFrame + market context. Defensive against missing
    columns, missing daily/premarket context, and None position.
  - build_market_features: turns df_ind + daily_atr + optional quote
    + optional position into MarketFeatures with composite has_red_flag.
  - build_account_state: equity + open positions → AccountState with
    total_exposure_pct.
  - synthesize_default_analysis: bridge LLMAnalysis until the LLM tier
    emits real classifications.

These are pure functions — no I/O, no async, no global state. Goal is
high coverage of the field-mapping logic so the eventual main.py call
site has a tested foundation.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, ".")

from strategy.llm.analysis import (
    CatalystQuality,
    PositionAction,
    SetupType,
    TradeReadiness,
)
from strategy.llm.context_builder import (
    DEFAULT_SPREAD_BPS,
    _last_n_bars_dicts,
    _macd_trend_label,
    _safe_last,
    _volume_to_percentile,
    build_account_state,
    build_llm_context,
    build_market_features,
    synthesize_default_analysis,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _indicator_df(
    n_bars: int = 10,
    *,
    close: float = 100.0,
    volume: int = 100_000,
    rsi: float = 60.0,
    macd_hist: float = 0.05,
    vwap: float = 99.0,
    boll: float = 0.3,
    volume_ratio: float = 1.0,
) -> pd.DataFrame:
    """Build a realistic-looking df_ind with the columns build_llm_context
    reads. Uses recent timestamps so .iterrows() produces real datetimes."""
    times = pd.date_range(
        end=datetime(2026, 5, 13, 14, 30, tzinfo=timezone.utc),
        periods=n_bars,
        freq="5min",
    )
    return pd.DataFrame(
        {
            "open": [close - 0.5] * n_bars,
            "high": [close + 0.3] * n_bars,
            "low": [close - 0.7] * n_bars,
            "close": [close] * n_bars,
            "volume": [volume] * n_bars,
            "rsi_14": [rsi] * n_bars,
            "macd_hist": [macd_hist] * n_bars,
            "vwap": [vwap] * n_bars,
            "bollinger_position": [boll] * n_bars,
            "volume_ratio_vs_20bar": [volume_ratio] * n_bars,
        },
        index=times,
    )


@dataclass
class _FakeDailyContext:
    """Duck-typed substitute for analysis.indicators.DailyContext."""
    regime: str = "bull"
    adx_14: float = 30.0
    daily_atr_14: float = 2.5
    sma_200: float = 95.0


@dataclass
class _FakePremarketContext:
    gap_pct: float = 2.5
    premarket_high: float = 101.5
    premarket_low: float = 99.0
    premarket_volume: int = 500_000
    premarket_rvol: float = 4.2


@dataclass
class _FakePosition:
    """Duck-typed substitute for an Alpaca position object."""
    ticker: str
    quantity: int
    current_price: float


# ---------------------------------------------------------------------------
# Internal helpers — direct unit tests
# ---------------------------------------------------------------------------


def test_safe_last_returns_default_on_empty():
    df = pd.DataFrame()
    assert _safe_last(df, "anything", 42.0) == 42.0


def test_safe_last_returns_default_on_missing_column():
    df = _indicator_df()
    assert _safe_last(df, "nonexistent_column", 42.0) == 42.0


def test_safe_last_returns_default_on_nan():
    df = _indicator_df()
    df.loc[df.index[-1], "rsi_14"] = float("nan")
    assert _safe_last(df, "rsi_14", 50.0) == 50.0


def test_safe_last_returns_real_value_when_present():
    df = _indicator_df(rsi=72.5)
    assert _safe_last(df, "rsi_14", 0.0) == 72.5


def test_macd_trend_rising():
    df = _indicator_df(n_bars=5)
    df["macd_hist"] = [0.0, 0.1, 0.2, 0.3, 0.4]
    assert _macd_trend_label(df) == "rising"


def test_macd_trend_falling():
    df = _indicator_df(n_bars=5)
    df["macd_hist"] = [0.4, 0.3, 0.2, 0.1, 0.0]
    assert _macd_trend_label(df) == "falling"


def test_macd_trend_flat_when_not_monotone():
    df = _indicator_df(n_bars=5)
    df["macd_hist"] = [0.1, 0.2, 0.1, 0.3, 0.2]  # non-monotone
    assert _macd_trend_label(df) == "flat"


def test_macd_trend_flat_when_too_few_bars():
    df = _indicator_df(n_bars=2)
    assert _macd_trend_label(df) == "flat"


def test_last_n_bars_dicts_returns_tuple_of_shape():
    df = _indicator_df(n_bars=15)
    bars = _last_n_bars_dicts(df, n=5)
    assert isinstance(bars, tuple)
    assert len(bars) == 5
    for bar in bars:
        assert set(bar.keys()) == {"ts", "o", "h", "l", "c", "v"}


def test_last_n_bars_dicts_empty_when_df_empty():
    df = pd.DataFrame()
    assert _last_n_bars_dicts(df, n=5) == ()


def test_volume_to_percentile_thin_volume_low_percentile():
    assert _volume_to_percentile(0.3) == 10.0


def test_volume_to_percentile_average_volume_mid_percentile():
    assert _volume_to_percentile(1.0) == 50.0


def test_volume_to_percentile_heavy_volume_high_percentile():
    assert _volume_to_percentile(5.0) == 95.0


def test_volume_to_percentile_below_red_flag_threshold():
    """The policy's default rvol_percentile_red_flag is 20. The
    stand-in's 20-pct cutoff is volume_ratio ≈ 0.625. Verify."""
    # ratio = 0.625 → percentile ≈ 20
    pct = _volume_to_percentile(0.625)
    assert 18.0 <= pct <= 22.0


# ---------------------------------------------------------------------------
# build_llm_context — happy path + edge cases
# ---------------------------------------------------------------------------


def test_build_llm_context_happy_path():
    df = _indicator_df()
    daily = _FakeDailyContext()
    pm = _FakePremarketContext()
    ctx = build_llm_context(
        ticker="AAPL",
        timestamp_et="2026-05-13T10:00:00",
        prompt_version="v0.1-ev-fields",
        df_ind=df,
        daily_ctx=daily,
        premarket_ctx=pm,
        sentiment=0.3,
        market_regime_label="trending_up",
        market_cap_bucket="mega",
        sector="Technology",
        minutes_since_open=30,
        avg_daily_volume=5_000_000,
    )
    assert ctx.ticker == "AAPL"
    assert ctx.prompt_version == "v0.1-ev-fields"
    assert ctx.market_regime_label == "trending_up"
    assert ctx.market_cap_bucket == "mega"
    assert ctx.daily_regime == "bull"
    assert ctx.daily_atr_14 == 2.5
    assert ctx.sma_200 == 95.0
    assert ctx.gap_pct == 2.5
    assert ctx.pm_high == 101.5
    assert ctx.pm_volume == 500_000
    assert ctx.rsi_14 == 60.0
    assert ctx.macd_hist == 0.05
    assert ctx.vwap == 99.0
    assert ctx.current_close == 100.0
    # distance_to_vwap_pct = (100 - 99) / 99 * 100 ≈ 1.01
    assert abs(ctx.distance_to_vwap_pct - 1.0101) < 0.01
    assert ctx.minutes_since_open == 30
    assert ctx.currently_holding is False
    assert ctx.position_qty == 0


def test_build_llm_context_with_position():
    df = _indicator_df()
    position = {
        "qty": 100,
        "avg_price": 95.0,
        "unrealized_pl_pct": 5.26,
        "has_active_stop": True,
    }
    ctx = build_llm_context(
        ticker="AAPL",
        timestamp_et="2026-05-13T10:00:00",
        prompt_version="v0.1-ev-fields",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
        position=position,
    )
    assert ctx.currently_holding is True
    assert ctx.position_qty == 100
    assert ctx.position_avg_price == 95.0
    assert ctx.position_unrealized_pl_pct == 5.26
    assert ctx.has_active_stop is True


def test_build_llm_context_short_position():
    df = _indicator_df()
    position = {"qty": -100, "avg_price": 105.0}
    ctx = build_llm_context(
        ticker="X",
        timestamp_et="t",
        prompt_version="v",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
        position=position,
    )
    assert ctx.currently_holding is True
    assert ctx.position_qty == -100


def test_build_llm_context_handles_missing_daily_ctx():
    df = _indicator_df()
    ctx = build_llm_context(
        ticker="X",
        timestamp_et="t",
        prompt_version="v",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
    )
    assert ctx.daily_regime == "neutral"
    assert ctx.daily_atr_14 == 0.0
    assert ctx.sma_200 == 0.0


def test_build_llm_context_handles_empty_df():
    """Bars haven't arrived yet — context still constructs without raising."""
    ctx = build_llm_context(
        ticker="X",
        timestamp_et="t",
        prompt_version="v",
        df_ind=pd.DataFrame(),
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
    )
    assert ctx.current_close == 0.0
    assert ctx.rsi_14 == 50.0
    assert ctx.current_5min_bar_count == 0
    assert ctx.last_10_5min_bars == ()


def test_build_llm_context_caps_last_10_bars():
    """df with more than 10 bars: only last 10 in the tuple."""
    df = _indicator_df(n_bars=25)
    ctx = build_llm_context(
        ticker="X",
        timestamp_et="t",
        prompt_version="v",
        df_ind=df,
        daily_ctx=None,
        premarket_ctx=None,
        sentiment=None,
    )
    assert len(ctx.last_10_5min_bars) == 10


# ---------------------------------------------------------------------------
# build_market_features
# ---------------------------------------------------------------------------


def test_build_market_features_uses_default_spread_when_none():
    df = _indicator_df()
    features = build_market_features(df_ind=df, daily_atr=2.0)
    assert features.spread_bps == DEFAULT_SPREAD_BPS


def test_build_market_features_uses_provided_spread():
    df = _indicator_df()
    features = build_market_features(df_ind=df, daily_atr=2.0, spread_bps=85.0)
    assert features.spread_bps == 85.0


def test_build_market_features_red_flag_fires_on_wide_spread():
    df = _indicator_df()
    features = build_market_features(df_ind=df, daily_atr=2.0, spread_bps=100.0)
    assert features.has_red_flag is True


def test_build_market_features_red_flag_fires_on_thin_volume():
    df = _indicator_df(volume_ratio=0.5)
    features = build_market_features(df_ind=df, daily_atr=2.0)
    assert features.has_red_flag is True


def test_build_market_features_no_red_flag_on_clean_inputs():
    df = _indicator_df(volume_ratio=1.5)  # rvol 1.5 → percentile ~73
    features = build_market_features(df_ind=df, daily_atr=2.0, spread_bps=5.0)
    assert features.has_red_flag is False


def test_build_market_features_distance_to_vwap_atr():
    df = _indicator_df(close=100.0, vwap=98.0)
    features = build_market_features(df_ind=df, daily_atr=2.0)
    # (100 - 98) / 2.0 = 1.0
    assert features.distance_to_vwap_atr == 1.0


def test_build_market_features_distance_to_stop_target_when_holding():
    df = _indicator_df(close=100.0)
    position = {"qty": 100}
    features = build_market_features(
        df_ind=df,
        daily_atr=2.0,
        position=position,
        stop_price=96.0,
        target_price=104.0,
    )
    # (100 - 96) / 2.0 = 2.0 ATRs above stop
    assert features.distance_to_stop_atr == 2.0
    # (104 - 100) / 2.0 = 2.0 ATRs below target
    assert features.distance_to_target_atr == 2.0


def test_build_market_features_no_distance_when_flat():
    df = _indicator_df()
    features = build_market_features(df_ind=df, daily_atr=2.0)
    assert features.distance_to_stop_atr is None
    assert features.distance_to_target_atr is None


# ---------------------------------------------------------------------------
# build_account_state
# ---------------------------------------------------------------------------


def test_build_account_state_no_positions():
    state = build_account_state(equity=100_000.0, open_positions=[])
    assert state.equity == 100_000.0
    assert state.total_exposure_pct == 0.0
    assert state.open_position_count == 0


def test_build_account_state_with_positions():
    positions = [
        _FakePosition(ticker="AAPL", quantity=100, current_price=200.0),
        _FakePosition(ticker="MSFT", quantity=50, current_price=400.0),
    ]
    state = build_account_state(equity=100_000.0, open_positions=positions)
    # Notional: 100×200 + 50×400 = 20000 + 20000 = 40000
    # 40000 / 100000 = 40%
    assert state.total_exposure_pct == 40.0
    assert state.open_position_count == 2


def test_build_account_state_short_position_uses_absolute_notional():
    """A -100 share short still adds |qty|×price to exposure."""
    positions = [_FakePosition(ticker="X", quantity=-100, current_price=200.0)]
    state = build_account_state(equity=100_000.0, open_positions=positions)
    # |−100| × 200 = 20000 → 20% exposure
    assert state.total_exposure_pct == 20.0


def test_build_account_state_zero_equity_safe():
    """Equity = 0 → don't divide-by-zero; exposure defaults to 0."""
    positions = [_FakePosition(ticker="X", quantity=100, current_price=50.0)]
    state = build_account_state(equity=0.0, open_positions=positions)
    assert state.total_exposure_pct == 0.0
    assert state.open_position_count == 1


def test_build_account_state_price_override():
    """current_price_lookup overrides the position's stale price."""
    positions = [_FakePosition(ticker="AAPL", quantity=100, current_price=200.0)]
    state = build_account_state(
        equity=100_000.0,
        open_positions=positions,
        current_price_lookup={"AAPL": 250.0},  # fresh
    )
    # 100 × 250 = 25000 → 25%
    assert state.total_exposure_pct == 25.0


# ---------------------------------------------------------------------------
# synthesize_default_analysis
# ---------------------------------------------------------------------------


def test_synthesize_default_analysis_defaults_are_permissive():
    """Defaults must NOT trigger policy's classification gates:
      - TradeReadiness.READY (not AVOID)
      - PositionAction.NO_OPINION (not EXIT/SCALE_UP)"""
    a = synthesize_default_analysis()
    assert a.trade_readiness == TradeReadiness.READY
    assert a.position_action == PositionAction.NO_OPINION
    assert a.catalyst_quality == CatalystQuality.NONE
    assert a.setup_type == SetupType.NO_SETUP


def test_synthesize_default_analysis_accepts_overrides():
    """Tests that exercise specific policy paths can override defaults."""
    a = synthesize_default_analysis(
        catalyst_quality=CatalystQuality.MAJOR,
        setup_type=SetupType.GAP_AND_GO,
        trade_readiness=TradeReadiness.AVOID,
        position_action=PositionAction.EXIT,
    )
    assert a.catalyst_quality == CatalystQuality.MAJOR
    assert a.trade_readiness == TradeReadiness.AVOID
    assert a.position_action == PositionAction.EXIT


def test_synthesize_default_analysis_has_bridge_marker_text():
    """The bridge analysis carries text that makes its origin visible
    in post-hoc audit — searching for 'bridge' in decisions.reasons
    should surface every pre-LLMOutput-refactor row."""
    a = synthesize_default_analysis()
    assert "bridge" in a.invalid_if or "bridge" in a.counter_thesis
