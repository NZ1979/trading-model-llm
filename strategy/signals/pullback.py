"""Pullback (mean-reversion-in-trend) technical signal.

Classic setup: in a trending daily regime, wait for an intraday
pullback that hits oversold (or overbought, for shorts), confirms
with MACD crossing back, and is holding above (or below) VWAP.

Pre-market overlay:
  - HARD BLOCK on counter-trend gaps: a gap-down >0.5*ATR while
    attempting a Buy (or gap-up >0.5*ATR while attempting a Sell)
    overrides the technical setup and produces a Hold.
  - CONFIDENCE BOOST: trend-aligned gaps holding direction.

Hard requirements (returned as Hold rather than None when not met):
  - daily_ctx must exist (for regime/SMA200/ADX)
  - At least 50 RTH bars (for indicator warmup)
  - All required intraday indicators non-NaN

Extracted 2026-05-06 from analysis/indicators.py.
"""
from __future__ import annotations

import pandas as pd

from analysis.indicators import (
    ADX_TREND_MIN,
    DailyContext,
    INTRADAY_COLUMNS,
    PremarketContext,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    TechnicalSignal,
)


def check_pullback(
    intraday_df: pd.DataFrame,
    daily_ctx: DailyContext | None,
    premarket_ctx: PremarketContext | None = None,
) -> TechnicalSignal | None:
    """Returns a TechnicalSignal if the pullback path applies (with Buy,
    Sell, or Hold for warmup/blocks), or None if no setup matched and the
    caller should fall through."""
    # ---- Warmup gates (Hold short-circuits) ----
    if daily_ctx is None or len(intraday_df) < 50:
        return TechnicalSignal("Hold", 0, "none", ("insufficient_data",))

    last = intraday_df.iloc[-1]
    if last[INTRADAY_COLUMNS].isna().any():
        return TechnicalSignal("Hold", 0, "none", ("indicators_warming_up",))

    # ---- Pullback indicator state ----
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

    # No buy setup, no sell setup, but conditions weren't blocked.
    # Fall through to the dispatcher's no_setup default.
    return None
