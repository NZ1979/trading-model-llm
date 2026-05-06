"""Technical indicators and signal generation for intraday equity trading.

Two-timeframe design:
- DAILY bars compute the regime filter (200 SMA, 14-period ADX). Computed once
  per day at open and cached. This answers "is the stock in a bull/bear regime?
  is it trending or chopping?"
- 5-MINUTE bars compute the live indicators that fire signals. Recomputed every
  bar close.

Why split it this way? A 200-period SMA on 5-min bars covers ~16 hours of
trading, which is meaningless for an intraday signal. The daily 200 SMA is the
classic institutional regime line; we use it the way it was meant to be used.

All Wilder-smoothed indicators (RSI, ADX) use ewm(alpha=1/period, adjust=False),
which is mathematically equivalent to Wilder's RMA and matches the values you'd
see on TradingView, ThinkOrSwim, and most charting platforms.

Inputs: pandas DataFrames with columns ['open', 'high', 'low', 'close', 'volume']
and a DatetimeIndex. Alpaca's bar API returns this shape directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

Signal = Literal["Buy", "Sell", "Hold"]
Regime = Literal["bull", "bear", "neutral"]


# ---------------------------------------------------------------------------
# Core indicator primitives
# ---------------------------------------------------------------------------

def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA). Equivalent to ewm with alpha=1/period."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Returns values in [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)  # neutral on division-by-zero


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower) bands."""
    mid = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std(ddof=0)
    return mid + num_std * std, mid, mid - num_std * std


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def adx_dmi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder's ADX and Directional Indicators.

    Returns (adx, plus_di, minus_di). All in [0, 100].
    ADX > 25 typically indicates a strong trend; < 20 is chop.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    atr = _wilder(true_range(high, low, close), period)
    # Guard against zero ATR (all-flat windows)
    atr_safe = atr.replace(0, np.nan)

    plus_di = 100 * _wilder(plus_dm, period) / atr_safe
    minus_di = 100 * _wilder(minus_dm, period) / atr_safe

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = _wilder(dx.fillna(0), period)

    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


def vwap(df: pd.DataFrame, session_only: bool = True) -> pd.Series:
    """Session-anchored VWAP. Resets each trading day.

    Args:
        df: 5-min bars with DatetimeIndex (timezone-aware, UTC from Alpaca).
        session_only: if True, only regular trading hours (9:30-16:00 ET) bars
            contribute to the VWAP. This is what most institutional traders use.
            If False, includes pre-market and after-hours volume too.

    Returns a Series aligned with df.index. Bars outside RTH (when session_only)
    get the most recent in-session VWAP via forward-fill.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]

    if session_only:
        # Mask out non-RTH bars before accumulating
        et = df.index.tz_convert("America/New_York")
        in_rth = (
            (et.hour > 9) | ((et.hour == 9) & (et.minute >= 30))
        ) & (et.hour < 16)
        pv = pv.where(in_rth, 0)
        vol = df["volume"].where(in_rth, 0)
    else:
        vol = df["volume"]

    session = df.index.normalize()
    cum_pv = pv.groupby(session).cumsum()
    cum_vol = vol.groupby(session).cumsum().replace(0, np.nan)
    return (cum_pv / cum_vol).ffill()


# ---------------------------------------------------------------------------
# Daily regime context (computed once per day, cached by caller)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DailyContext:
    ticker: str
    last_close: float
    sma_200: float
    adx_14: float
    regime: Regime
    is_trending: bool
    daily_atr_14: float = 0.0  # Wilder's ATR(14), in price units. 0 if unavailable.


def compute_daily_context(daily_df: pd.DataFrame, ticker: str) -> DailyContext | None:
    """Compute regime filter from daily bars.

    Needs at least 200 daily bars. Returns None if data is insufficient.
    Call this once per ticker per day at market open.

    Bug H 2026-05-06: also computes Wilder's ATR(14) for ATR-based stops.
    """
    if len(daily_df) < 200:
        return None

    close = daily_df["close"]
    sma200 = sma(close, 200).iloc[-1]
    adx14, _, _ = adx_dmi(daily_df["high"], daily_df["low"], close, period=14)
    last_adx = adx14.iloc[-1]
    last_close = close.iloc[-1]

    # Wilder's ATR(14) for ATR-aware stop placement
    atr14_series = _wilder(
        true_range(daily_df["high"], daily_df["low"], daily_df["close"]),
        14,
    )
    last_atr14 = atr14_series.iloc[-1]
    if pd.isna(last_atr14) or last_atr14 < 0:
        last_atr14 = 0.0

    if pd.isna(sma200) or pd.isna(last_close):
        return None

    if last_close > sma200 * 1.005:  # 0.5% buffer prevents flip-flop near the line
        regime: Regime = "bull"
    elif last_close < sma200 * 0.995:
        regime = "bear"
    else:
        regime = "neutral"

    return DailyContext(
        ticker=ticker,
        last_close=float(last_close),
        sma_200=float(sma200),
        adx_14=float(last_adx),
        regime=regime,
        is_trending=last_adx > 20,
        daily_atr_14=float(last_atr14),
    )


# ---------------------------------------------------------------------------
# Pre-market context (computed at 9:30 AM ET handoff, refreshed once)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PremarketContext:
    ticker: str
    prior_close: float          # yesterday's regular-session close
    prior_high: float           # yesterday's RTH high
    prior_low: float            # yesterday's RTH low
    premarket_high: float       # today 4:00-9:30 AM ET high (NaN if no data)
    premarket_low: float
    premarket_volume: int       # today's pre-market total
    premarket_rvol: float       # today's pre-market vol / 20-day avg
    is_unusual_volume: bool     # True if premarket_rvol > UNUSUAL_RVOL_THRESHOLD
    gap_pct: float              # (today_open - prior_close) / prior_close * 100
    gap_atr_ratio: float        # |gap_pct| in ATR units; >1.0 = abnormally large


# RVOL thresholds (relative volume vs 20-day average pre-market volume).
# 5x is the desk-standard threshold for "unusual" on liquid US equities;
# 10x is news-driven extreme.
UNUSUAL_RVOL_THRESHOLD = 5.0
EXTREME_RVOL_THRESHOLD = 10.0


def _filter_to_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars during regular trading hours (9:30-16:00 ET)."""
    et = df.index.tz_convert("America/New_York")
    in_rth = (
        (et.hour > 9) | ((et.hour == 9) & (et.minute >= 30))
    ) & (et.hour < 16)
    return df[in_rth]


def _filter_to_premarket(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars during pre-market (4:00-9:30 ET) for today only."""
    et = df.index.tz_convert("America/New_York")
    in_pm = (et.hour >= 4) & (
        (et.hour < 9) | ((et.hour == 9) & (et.minute < 30))
    )
    return df[in_pm]


def compute_premarket_context(
    daily_df: pd.DataFrame,
    today_full_session_df: pd.DataFrame,
    ticker: str,
    historical_pm_volumes: list[int] | None = None,
) -> PremarketContext | None:
    """Compute pre-market levels, gap metrics, and RVOL.

    Args:
        daily_df: at least 15 daily bars to compute 14-period ATR. Must include
            yesterday as the last row.
        today_full_session_df: 5-min bars from today, INCLUDING pre-market
            (4:00 AM ET onward). Must include the 9:30 AM bar (the open).
        ticker: symbol.
        historical_pm_volumes: list of daily pre-market volume totals for the
            last ~20 trading days, from data.polygon_feed.backfill_premarket_baselines.
            If None or empty, RVOL defaults to 0 and is_unusual_volume to False
            (the gap-and-go signal will not fire). For paper testing without
            historical data, you can stub this with a fixed list.

    Returns None if data is insufficient. Call once at 9:30 AM ET; the result
    is static for the rest of the day.
    """
    if len(daily_df) < 15:
        return None

    prior = daily_df.iloc[-1]
    prior_close = float(prior["close"])
    prior_high = float(prior["high"])
    prior_low = float(prior["low"])

    # 14-period ATR for gap normalization
    atr14 = _wilder(
        true_range(daily_df["high"], daily_df["low"], daily_df["close"]),
        14,
    ).iloc[-1]
    if pd.isna(atr14) or atr14 == 0:
        return None

    # Pre-market session
    pm_df = _filter_to_premarket(today_full_session_df)
    if len(pm_df) > 0:
        pm_high = float(pm_df["high"].max())
        pm_low = float(pm_df["low"].min())
        pm_volume = int(pm_df["volume"].sum())
    else:
        pm_high = float("nan")
        pm_low = float("nan")
        pm_volume = 0

    # Today's open: first RTH bar
    rth_df = _filter_to_rth(today_full_session_df)
    if len(rth_df) == 0:
        if len(pm_df) == 0:
            return None
        today_open = float(pm_df["close"].iloc[-1])
    else:
        today_open = float(rth_df["open"].iloc[0])

    gap_pct = (today_open - prior_close) / prior_close * 100
    gap_atr_ratio = abs(today_open - prior_close) / atr14

    # RVOL: today's pre-market volume vs the 20-day mean
    if historical_pm_volumes:
        baseline = float(np.mean(historical_pm_volumes))
        if baseline > 0:
            rvol = pm_volume / baseline
        else:
            rvol = 0.0
    else:
        rvol = 0.0

    return PremarketContext(
        ticker=ticker,
        prior_close=prior_close,
        prior_high=prior_high,
        prior_low=prior_low,
        premarket_high=pm_high,
        premarket_low=pm_low,
        premarket_volume=pm_volume,
        premarket_rvol=float(rvol),
        is_unusual_volume=rvol >= UNUSUAL_RVOL_THRESHOLD,
        gap_pct=float(gap_pct),
        gap_atr_ratio=float(gap_atr_ratio),
    )


# ---------------------------------------------------------------------------
# Intraday indicator computation (run on every new 5-min bar)
# ---------------------------------------------------------------------------

INTRADAY_COLUMNS = [
    "sma_20", "sma_50", "ema_9",
    "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_mid", "bb_lower",
    "adx_14", "plus_di", "minus_di",
    "vwap",
]


def compute_intraday_indicators(
    df: pd.DataFrame,
    rth_only: bool = True,
) -> pd.DataFrame:
    """Add all intraday indicator columns to a 5-min bar DataFrame.

    Args:
        df: 5-min bars. Can include pre-market and after-hours bars; they will
            be filtered out by default (rth_only=True).
        rth_only: if True (default), indicators are computed on regular trading
            hours (9:30-16:00 ET) bars only. This is the right default: thin
            pre-market volume creates spurious RSI/MACD readings that fire
            phantom signals. Pre-market data should be used as separate context
            via compute_premarket_context(), not folded into the indicator math.

    Returns a new DataFrame with indicator columns. Recommended lookback is
    100+ RTH bars (8+ hours) so SMA50 and ADX have warmed up.
    """
    if rth_only:
        df = _filter_to_rth(df)

    if len(df) < 50:
        out = df.copy()
        for col in INTRADAY_COLUMNS:
            out[col] = np.nan
        return out

    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]

    out["sma_20"] = sma(close, 20)
    out["sma_50"] = sma(close, 50)
    out["ema_9"] = ema(close, 9)
    out["rsi_14"] = rsi(close, 14)

    macd_line, signal_line, hist = macd(close)
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist

    upper, mid, lower = bollinger(close)
    out["bb_upper"] = upper
    out["bb_mid"] = mid
    out["bb_lower"] = lower

    adx, plus_di, minus_di = adx_dmi(high, low, close, period=14)
    out["adx_14"] = adx
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    out["vwap"] = vwap(out)
    return out


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

Setup = Literal["pullback", "gap_and_go", "none"]


@dataclass(frozen=True, slots=True)
class TechnicalSignal:
    signal: Signal
    confidence: int  # 0-100
    setup: Setup
    reasons: tuple[str, ...]


# Thresholds tuned for intraday liquid US equities. Tighten for less-liquid names.
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
ADX_TREND_MIN = 20


def _check_gap_and_go(
    intraday_df: pd.DataFrame,
    daily_ctx: DailyContext | None,
    premarket_ctx: PremarketContext,
) -> TechnicalSignal | None:
    """Gap-and-go on unusual pre-market volume.

    This is a momentum signal that fires INDEPENDENTLY of the daily regime
    filter. News-driven volume gaps override technical regime — that's the
    whole point of trading them. A bear-regime stock that gets bought out
    at +30% on 50x normal pre-market volume is still a buy.

    Triggers when ALL are true:
      - is_unusual_volume (premarket RVOL > 5x)
      - |gap_pct| >= 1.0 (meaningful gap, not noise)
      - within 9:35-10:00 ET window (after opening volatility, before
        gap-and-go momentum has typically faded)
      - For Buy: gap up AND price holding above pre-market low
      - For Sell: gap down AND price holding below pre-market high

    Returns None if not fired (caller falls through to pullback logic).
    """
    if not premarket_ctx.is_unusual_volume:
        return None
    if abs(premarket_ctx.gap_pct) < 1.0:
        return None

    last = intraday_df.iloc[-1]
    last_ts = intraday_df.index[-1].tz_convert("America/New_York")

    # Time window: 9:35-10:00 ET. After this, the gap-and-go has either
    # worked (price extended) or failed (faded back to gap fill).
    in_window = (
        (last_ts.hour == 9 and last_ts.minute >= 35)
        or (last_ts.hour == 10 and last_ts.minute == 0)
    )
    if not in_window:
        return None

    close = float(last["close"])

    # ---- Buy path: gap up + holding ----
    if premarket_ctx.gap_pct >= 1.0:
        if pd.isna(premarket_ctx.premarket_low):
            return None
        # Gap is "holding" if price is at or above 99.8% of pre-market low.
        # Tighter than this and we miss valid setups; looser and we catch
        # gaps that have already failed.
        if close < premarket_ctx.premarket_low * 0.998:
            return None

        confidence = 70
        reasons = [
            f"gap_up_{premarket_ctx.gap_pct:.2f}%",
            f"premarket_rvol={premarket_ctx.premarket_rvol:.1f}x",
            f"close>=pm_low*0.998({premarket_ctx.premarket_low:.2f})",
        ]
        if premarket_ctx.premarket_rvol >= EXTREME_RVOL_THRESHOLD:
            confidence += 10
            reasons.append(f"extreme_rvol>{EXTREME_RVOL_THRESHOLD}x")
        # Breakout of pre-market high in the trigger bar = momentum confirmed
        if (
            not pd.isna(premarket_ctx.premarket_high)
            and close > premarket_ctx.premarket_high
        ):
            confidence += 10
            reasons.append(f"broke_pm_high({premarket_ctx.premarket_high:.2f})")
        # Daily regime alignment is a bonus, not a requirement
        if daily_ctx is not None and daily_ctx.regime == "bull":
            confidence += 10
            reasons.append("daily_regime_aligned")
        return TechnicalSignal(
            "Buy", min(confidence, 100), "gap_and_go", tuple(reasons),
        )

    # ---- Sell path: gap down + holding ----
    if premarket_ctx.gap_pct <= -1.0:
        if pd.isna(premarket_ctx.premarket_high):
            return None
        if close > premarket_ctx.premarket_high * 1.002:
            return None

        confidence = 70
        reasons = [
            f"gap_down_{premarket_ctx.gap_pct:.2f}%",
            f"premarket_rvol={premarket_ctx.premarket_rvol:.1f}x",
            f"close<=pm_high*1.002({premarket_ctx.premarket_high:.2f})",
        ]
        if premarket_ctx.premarket_rvol >= EXTREME_RVOL_THRESHOLD:
            confidence += 10
            reasons.append(f"extreme_rvol>{EXTREME_RVOL_THRESHOLD}x")
        if (
            not pd.isna(premarket_ctx.premarket_low)
            and close < premarket_ctx.premarket_low
        ):
            confidence += 10
            reasons.append(f"broke_pm_low({premarket_ctx.premarket_low:.2f})")
        if daily_ctx is not None and daily_ctx.regime == "bear":
            confidence += 10
            reasons.append("daily_regime_aligned")
        return TechnicalSignal(
            "Sell", min(confidence, 100), "gap_and_go", tuple(reasons),
        )

    return None


def generate_signal(
    intraday_df: pd.DataFrame,
    daily_ctx: DailyContext | None,
    premarket_ctx: PremarketContext | None = None,
) -> TechnicalSignal:
    """Decide Buy/Sell/Hold from the latest intraday bar.

    Two setup paths, checked in order:

    1) GAP-AND-GO (momentum): Unusual pre-market volume + meaningful gap +
       price holding the gap, in the 9:35-10:00 ET window. Fires regardless
       of daily regime. See _check_gap_and_go for details.

    2) PULLBACK (mean reversion in trend): The classic setup. Daily regime
       is bull AND daily ADX > 20 AND intraday RSI oversold AND MACD turning
       up AND price > VWAP - 0.3%. Symmetric for sells.

    Pre-market overlay on the pullback path:
      - HARD BLOCK on counter-trend gaps (gap down >0.5*ATR while attempting
        a Buy, or gap up >0.5*ATR while attempting a Sell). News-driven gaps
        override technical setups.
      - CONFIDENCE BOOST (+15) for trend-aligned gaps holding direction.

    Hard blocks that apply to both paths:
      - First 5 min of RTH (9:30-9:35 ET): too volatile.
      - Insufficient data or warming-up indicators.
    """
    # ---- Hard blocks that apply regardless of warmup ----
    if len(intraday_df) == 0:
        return TechnicalSignal("Hold", 0, "none", ("no_bars",))

    # Opening-volatility hard block (9:30-9:35 ET) — cheapest gate, runs first.
    last_ts = intraday_df.index[-1].tz_convert("America/New_York")
    if last_ts.hour == 9 and last_ts.minute < 35:
        return TechnicalSignal("Hold", 0, "none", ("opening_volatility_window",))

    # ---- Path 1: gap-and-go (priority, NO warmup required) ----
    # Gap-and-go only needs PM context + the last bar's close + the time
    # window. It does NOT need 50 bars of intraday indicator warmup, so we
    # check it before the indicator-warmup gates. This is the fix for the
    # 2026-04-29 audit finding that gap-and-go was structurally unreachable
    # because the warmup gate (~1:40 PM ET) closed after the gap-and-go
    # window (9:35-10:00 ET).
    if premarket_ctx is not None:
        gng = _check_gap_and_go(intraday_df, daily_ctx, premarket_ctx)
        if gng is not None:
            return gng

    # ---- Pullback path requires full indicator warmup ----
    if daily_ctx is None or len(intraday_df) < 50:
        return TechnicalSignal("Hold", 0, "none", ("insufficient_data",))

    last = intraday_df.iloc[-1]
    if last[INTRADAY_COLUMNS].isna().any():
        return TechnicalSignal("Hold", 0, "none", ("indicators_warming_up",))

    # ---- Path 2: pullback in trend ----
    close = last["close"]
    rsi_v = last["rsi_14"]
    sma20 = last["sma_20"]
    adx_v = last["adx_14"]
    plus_di = last["plus_di"]
    minus_di = last["minus_di"]
    vwap_v = last["vwap"]

    recent_hist = intraday_df["macd_hist"].iloc[-3:]
    macd_turned_up = (recent_hist.iloc[0] < 0) and (recent_hist.iloc[-1] > 0)
    macd_turned_down = (recent_hist.iloc[0] > 0) and (recent_hist.iloc[-1] < 0)

    # ----- BUY pullback -----
    buy_setup = (
        daily_ctx.regime == "bull"
        and daily_ctx.is_trending
        and close > sma20
        and rsi_v < RSI_OVERSOLD
        and macd_turned_up
        and close > vwap_v * 0.997
    )
    if buy_setup:
        if premarket_ctx is not None:
            if premarket_ctx.gap_pct < 0 and premarket_ctx.gap_atr_ratio > 0.5:
                return TechnicalSignal(
                    "Hold", 0, "none",
                    (f"counter_trend_gap_down({premarket_ctx.gap_pct:.2f}%)",),
                )

        reasons = [
            f"daily_regime=bull(SMA200={daily_ctx.sma_200:.2f})",
            f"daily_adx={daily_ctx.adx_14:.1f}",
            f"close>SMA20({sma20:.2f})",
            f"rsi={rsi_v:.1f}<{RSI_OVERSOLD}",
            "macd_hist_cross_up",
            f"close>vwap({vwap_v:.2f})",
        ]
        confidence = 60
        if adx_v > ADX_TREND_MIN:
            confidence += 15
            reasons.append(f"intraday_adx={adx_v:.1f}>{ADX_TREND_MIN}")
        if plus_di > minus_di:
            confidence += 15
            reasons.append("plus_di>minus_di")
        if last["volume"] > intraday_df["volume"].iloc[-20:].mean() * 1.2:
            confidence += 10
            reasons.append("volume_spike")
        if premarket_ctx is not None and premarket_ctx.gap_pct > 0.3:
            if (
                not pd.isna(premarket_ctx.premarket_low)
                and close > premarket_ctx.premarket_low
            ):
                confidence += 15
                reasons.append(
                    f"gap_up_{premarket_ctx.gap_pct:.2f}%_holding_pm_low"
                )
        return TechnicalSignal(
            "Buy", min(confidence, 100), "pullback", tuple(reasons),
        )

    # ----- SELL pullback -----
    sell_setup = (
        daily_ctx.regime == "bear"
        and daily_ctx.is_trending
        and close < sma20
        and rsi_v > RSI_OVERBOUGHT
        and macd_turned_down
        and close < vwap_v * 1.003
    )
    if sell_setup:
        if premarket_ctx is not None:
            if premarket_ctx.gap_pct > 0 and premarket_ctx.gap_atr_ratio > 0.5:
                return TechnicalSignal(
                    "Hold", 0, "none",
                    (f"counter_trend_gap_up({premarket_ctx.gap_pct:.2f}%)",),
                )

        reasons = [
            f"daily_regime=bear(SMA200={daily_ctx.sma_200:.2f})",
            f"daily_adx={daily_ctx.adx_14:.1f}",
            f"close<SMA20({sma20:.2f})",
            f"rsi={rsi_v:.1f}>{RSI_OVERBOUGHT}",
            "macd_hist_cross_down",
            f"close<vwap({vwap_v:.2f})",
        ]
        confidence = 60
        if adx_v > ADX_TREND_MIN:
            confidence += 15
            reasons.append(f"intraday_adx={adx_v:.1f}>{ADX_TREND_MIN}")
        if minus_di > plus_di:
            confidence += 15
            reasons.append("minus_di>plus_di")
        if last["volume"] > intraday_df["volume"].iloc[-20:].mean() * 1.2:
            confidence += 10
            reasons.append("volume_spike")
        if premarket_ctx is not None and premarket_ctx.gap_pct < -0.3:
            if (
                not pd.isna(premarket_ctx.premarket_high)
                and close < premarket_ctx.premarket_high
            ):
                confidence += 15
                reasons.append(
                    f"gap_down_{premarket_ctx.gap_pct:.2f}%_holding_pm_high"
                )
        return TechnicalSignal(
            "Sell", min(confidence, 100), "pullback", tuple(reasons),
        )

    return TechnicalSignal("Hold", 0, "none", ("no_setup",))
