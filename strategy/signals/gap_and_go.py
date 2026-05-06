"""Gap-and-go technical signal.

Momentum strategy: enter when a stock has unusual pre-market volume,
a meaningful gap, and price is holding the gap direction in the
9:35-10:00 ET window. Fires regardless of daily regime — news-driven
volume gaps override technical regime classification.

Triggers when ALL are true:
  - is_unusual_volume (premarket RVOL > the per-ticker threshold;
    Phase C 2026-05-06 makes this per-ticker rather than a global 5x)
  - |gap_pct| >= 1.0 (meaningful gap, not noise)
  - within 9:35-10:00 ET window (after opening volatility, before
    gap-and-go momentum has typically faded)
  - For Buy: gap up AND price holding above pre-market low
  - For Sell: gap down AND price holding below pre-market high

Extracted 2026-05-06 from analysis/indicators.py to enable model-fork
plug-in.
"""
from __future__ import annotations

import pandas as pd

from analysis.indicators import (
    DailyContext,
    EXTREME_RVOL_THRESHOLD,
    PremarketContext,
    TechnicalSignal,
)


def check_gap_and_go(
    intraday_df: pd.DataFrame,
    daily_ctx: DailyContext | None,
    premarket_ctx: PremarketContext,
) -> TechnicalSignal | None:
    """Returns a TechnicalSignal if the gap-and-go conditions match,
    or None if the caller should fall through to the next signal."""
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
