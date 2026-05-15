"""Builders that turn per-cycle state into policy.py / signal_engine inputs.

The orchestrator (`main.py`) maintains a lot of state — per-symbol bars,
daily/premarket contexts, indicator DataFrames, sentiment scores, news,
positions. The LLM signal engine and the deterministic policy consume
specific shapes (``LLMContext``, ``MarketFeatures``, ``AccountState``,
``LLMAnalysis``). This module is the thin adapter between those.

Why a separate module:
  1. Pure functions, no async, no I/O. Unit-testable without spinning
     up the full orchestrator.
  2. Keeps ``main.py`` thin — the wiring step that adds the call site
     stays small and easy to review.
  3. Lets the M2 replay harness use the same builders that production
     uses, so replay produces identical LLMContexts given the same
     historical state. Per
     ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § "Same code path as live."

What this module does NOT do:
  - It does NOT call the LLM. ``signal_engine.evaluate`` does that.
  - It does NOT make any policy decisions. ``policy.decide`` does that.
  - It does NOT execute orders.

A note on the synthesized ``LLMAnalysis``:

The v2 design has the LLM emit both ``LLMAnalysis`` (classifications)
and ``LLMDecision`` (advisory action) as ``LLMOutput``. As of
2026-05-13 the LLM tier still emits only ``LLMDecision`` — the
``LLMOutput`` refactor is parked as a separate task. Until that lands,
``synthesize_default_analysis()`` here returns a permissive analysis
(``TradeReadiness.READY``, ``PositionAction.NO_OPINION``) so the
policy's classification-aware gates (step 2 AVOID, step 4b position
management) don't fire spuriously and the policy still routes through
its bucket / features / advisory paths. This is the "bridge" until the
LLM tier provides real classifications.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from strategy.llm.analysis import (
    CatalystQuality,
    LLMAnalysis,
    PositionAction,
    SetupType,
    TradeReadiness,
)
from strategy.llm.policy import (
    AccountState,
    BucketStats,
    MarketFeatures,
)
from strategy.llm.types import LLMContext


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fallback spread when no quote-level data is available. 10 bps is a
# conservative mid-cap estimate; mega-caps are tighter, micros wider.
# Once a real quotes feed is wired in, this default is unused.
DEFAULT_SPREAD_BPS = 10.0


# ---------------------------------------------------------------------------
# LLMContext builder
# ---------------------------------------------------------------------------


def _safe_last(df: pd.DataFrame, column: str, default: float) -> float:
    """Read the last value of a column from df_ind defensively.

    Returns the column's final value as a float, or the default when:
      - the column doesn't exist (indicator not warmed up)
      - the value is NaN
      - the DataFrame is empty
    """
    if df.empty or column not in df.columns:
        return default
    val = df[column].iloc[-1]
    if pd.isna(val):
        return default
    return float(val)


def _macd_trend_label(df: pd.DataFrame, column: str = "macd_hist") -> str:
    """Classify the last 3 MACD histogram values as rising/falling/flat."""
    if df.empty or column not in df.columns or len(df) < 3:
        return "flat"
    last3 = df[column].iloc[-3:]
    if last3.isna().any():
        return "flat"
    a, b, c = last3.iloc[0], last3.iloc[1], last3.iloc[2]
    if c > b > a:
        return "rising"
    if c < b < a:
        return "falling"
    return "flat"


def _last_n_bars_dicts(df: pd.DataFrame, n: int = 10) -> tuple[dict[str, Any], ...]:
    """Extract the trailing N rows of df as bar dicts the LLM can read.

    Shape matches ``LLMContext.last_10_5min_bars``: tuple of
    ``{ts, o, h, l, c, v}`` dicts. Empty tuple if the DataFrame has no
    OHLCV columns or is empty."""
    required = {"open", "high", "low", "close", "volume"}
    if df.empty or not required.issubset(df.columns):
        return ()
    tail = df.tail(n)
    rows: list[dict[str, Any]] = []
    for ts, row in tail.iterrows():
        rows.append({
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": int(row["volume"]),
        })
    return tuple(rows)


def build_llm_context(
    *,
    ticker: str,
    timestamp_et: str,
    prompt_version: str,
    df_ind: pd.DataFrame,
    daily_ctx: Any | None,
    premarket_ctx: Any | None,
    sentiment: float | None,
    news_items: tuple[dict[str, Any], ...] = (),
    has_earnings_today: bool = False,
    has_earnings_within_3d: bool = False,
    position: dict[str, Any] | None = None,
    spy_change_pct: float = 0.0,
    spy_rvol: float = 1.0,
    vix_level: float | None = None,
    market_regime_label: str = "unknown",
    sector: str = "Unknown",
    market_cap_bucket: str = "unknown",
    avg_daily_volume: int = 0,
    minutes_since_open: int = 0,
    minutes_until_close: int = 390,
    in_gap_and_go_window: bool = False,
    todays_prior_decisions: tuple[dict[str, Any], ...] = (),
    catalyst_flags: tuple[str, ...] = (),
    last_5_daily_closes: tuple[float, ...] = (),
) -> LLMContext:
    """Assemble an LLMContext from the orchestrator's per-cycle state.

    All inputs are explicit + keyword-only — the orchestrator's call
    site is the one place that has to know which sources feed which
    field, and that knowledge stays out of this builder. Defaults match
    LLMContext's own defaults so a partial caller still produces a
    valid context (e.g., tests can omit most fields).

    The ``position`` dict (when not None) is shaped:
        {
          "qty": int,            # positive long, negative short
          "avg_price": float,
          "unrealized_pl_pct": float | None,
          "has_active_stop": bool,
        }
    None → flat. The function maps the dict to LLMContext's flat fields.

    The ``daily_ctx`` / ``premarket_ctx`` arguments are duck-typed: we
    read attributes that ``analysis.indicators.DailyContext`` /
    ``PremarketContext`` happen to expose, with defaults when the
    object is None. This keeps this module from importing from
    ``analysis.indicators`` (circular-import risk in test fixtures).
    """
    # Position fields
    currently_holding = bool(position and position.get("qty", 0) != 0)
    position_qty = int(position.get("qty", 0)) if position else 0
    position_avg_price = position.get("avg_price") if position else None
    position_unrealized_pl_pct = (
        position.get("unrealized_pl_pct") if position else None
    )
    has_active_stop = bool(position and position.get("has_active_stop", False))

    # Daily-context fields
    daily_regime = (
        getattr(daily_ctx, "regime", "neutral") if daily_ctx else "neutral"
    )
    daily_adx_14 = float(getattr(daily_ctx, "adx_14", 0.0) or 0.0) if daily_ctx else 0.0
    daily_atr_14 = float(getattr(daily_ctx, "daily_atr_14", 0.0) or 0.0) if daily_ctx else 0.0
    sma_200 = float(getattr(daily_ctx, "sma_200", 0.0) or 0.0) if daily_ctx else 0.0

    # Premarket fields
    gap_pct = float(getattr(premarket_ctx, "gap_pct", 0.0) or 0.0) if premarket_ctx else 0.0
    pm_high = getattr(premarket_ctx, "premarket_high", None) if premarket_ctx else None
    pm_low = getattr(premarket_ctx, "premarket_low", None) if premarket_ctx else None
    pm_volume = int(getattr(premarket_ctx, "premarket_volume", 0) or 0) if premarket_ctx else 0
    pm_rvol = float(getattr(premarket_ctx, "premarket_rvol", 0.0) or 0.0) if premarket_ctx else 0.0

    # Intraday indicator fields — defensive reads of df_ind's last row
    current_close = _safe_last(df_ind, "close", 0.0)
    current_volume = int(_safe_last(df_ind, "volume", 0))
    rsi_14 = _safe_last(df_ind, "rsi_14", 50.0)
    macd_hist = _safe_last(df_ind, "macd_hist", 0.0)
    macd_hist_3bar_trend = _macd_trend_label(df_ind)
    vwap = _safe_last(df_ind, "vwap", 0.0)
    bollinger_position = _safe_last(df_ind, "bollinger_position", 0.0)
    volume_ratio_vs_20bar = _safe_last(df_ind, "volume_ratio_vs_20bar", 1.0)

    distance_to_vwap_pct = (
        ((current_close - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    )

    # Bar count — how warm are the indicators?
    current_5min_bar_count = len(df_ind) if not df_ind.empty else 0

    # last_5_daily_closes — carried via the kwarg of the same name.
    # DailyContext does not carry the close series directly; live's
    # orchestrator has the series on state.daily_df, and the M2 replay
    # harness pre-computes the tuple in TickerDayState.last_5_daily_closes.
    # Both paths pass it through this kwarg; callers that don't have it
    # leave the default empty tuple and the prompt template renders "n/a".

    return LLMContext(
        ticker=ticker,
        timestamp_et=timestamp_et,
        prompt_version=prompt_version,
        catalyst_flags=catalyst_flags,
        pm_rvol=pm_rvol,
        gap_pct=gap_pct,
        pm_high=pm_high,
        pm_low=pm_low,
        pm_volume=pm_volume,
        spy_change_pct=spy_change_pct,
        spy_rvol=spy_rvol,
        vix_level=vix_level,
        market_regime_label=market_regime_label,
        sector=sector,
        market_cap_bucket=market_cap_bucket,
        avg_daily_volume=avg_daily_volume,
        daily_regime=daily_regime,
        daily_adx_14=daily_adx_14,
        daily_atr_14=daily_atr_14,
        sma_200=sma_200,
        last_5_daily_closes=last_5_daily_closes,
        current_close=current_close,
        current_volume=current_volume,
        current_5min_bar_count=current_5min_bar_count,
        last_10_5min_bars=_last_n_bars_dicts(df_ind, n=10),
        rsi_14=rsi_14,
        macd_hist=macd_hist,
        macd_hist_3bar_trend=macd_hist_3bar_trend,
        vwap=vwap,
        distance_to_vwap_pct=distance_to_vwap_pct,
        bollinger_position=bollinger_position,
        volume_ratio_vs_20bar=volume_ratio_vs_20bar,
        news_items=news_items,
        has_earnings_today=has_earnings_today,
        has_earnings_within_3d=has_earnings_within_3d,
        currently_holding=currently_holding,
        position_qty=position_qty,
        position_avg_price=position_avg_price,
        position_unrealized_pl_pct=position_unrealized_pl_pct,
        has_active_stop=has_active_stop,
        todays_prior_decisions=todays_prior_decisions,
        minutes_since_open=minutes_since_open,
        minutes_until_close=minutes_until_close,
        in_gap_and_go_window=in_gap_and_go_window,
    )


# ---------------------------------------------------------------------------
# MarketFeatures builder
# ---------------------------------------------------------------------------


def _volume_to_percentile(
    current_volume_ratio: float,
    distribution_floor: float = 0.5,
    distribution_ceiling: float = 3.0,
) -> float:
    """Coarse RVOL → percentile mapping in the absence of a real
    rolling-distribution table.

    Maps:
        volume_ratio_vs_20bar <= 0.5  →  ~10 (very thin)
        volume_ratio_vs_20bar == 1.0  →  ~50 (average)
        volume_ratio_vs_20bar >= 3.0  →  ~95 (very heavy)

    Linear interpolation in between, clamped to [0, 100]. Stand-in
    until the orchestrator can supply a real per-symbol percentile.
    The policy's red-flag gate uses ``rvol_percentile_red_flag``
    (default 20) — anything thinner than that triggers a downgrade,
    which under this stand-in means ``current_volume_ratio < 0.60``.
    """
    if current_volume_ratio <= distribution_floor:
        return 10.0
    if current_volume_ratio >= distribution_ceiling:
        return 95.0
    # Linear: 0.5 → 10, 1.0 → 50, 3.0 → 95
    if current_volume_ratio <= 1.0:
        # Map [0.5, 1.0] → [10, 50]
        return 10.0 + (current_volume_ratio - 0.5) / 0.5 * 40.0
    # Map [1.0, 3.0] → [50, 95]
    return 50.0 + (current_volume_ratio - 1.0) / 2.0 * 45.0


def build_market_features(
    *,
    df_ind: pd.DataFrame,
    daily_atr: float,
    spread_bps: float | None = None,
    position: dict[str, Any] | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> MarketFeatures:
    """Assemble a MarketFeatures from indicator state + optional quote
    + optional position.

    ``spread_bps``: pass the real top-of-book spread when a quote feed
    is available; defaults to ``DEFAULT_SPREAD_BPS`` (10 bps) when None.
    The default is conservative — it implies a tight liquid name, so
    the spread red-flag gate won't fire spuriously.

    ``rvol_percentile`` is derived from ``volume_ratio_vs_20bar`` on
    df_ind via the linear stand-in in ``_volume_to_percentile``. When
    a real percentile table is wired in (M2 replay produces one as a
    side effect), the orchestrator can compute it directly and pass
    via this function's params or override.

    ``has_red_flag`` is the composite per the design doc:
        has_red_flag = (
            features.spread_bps > spread_bps_red_flag
            OR features.rvol_percentile < rvol_percentile_red_flag
            OR features.volume_ratio_vs_20bar < 0.7
        )

    We compute it here with the *configured* thresholds inlined as
    defaults — the same values are in PolicyConfig. The policy's red-
    flag gate will then evaluate these features against PolicyConfig's
    thresholds again, which means a tighter PolicyConfig will still
    trigger the gate even if has_red_flag is False here. This double-
    check is intentional: the gate composition can use info the policy
    doesn't see directly (e.g., spread expansion deltas).
    """
    spread_bps_resolved = spread_bps if spread_bps is not None else DEFAULT_SPREAD_BPS
    volume_ratio = _safe_last(df_ind, "volume_ratio_vs_20bar", 1.0)
    rvol_percentile = _volume_to_percentile(volume_ratio)

    # Composite red-flag uses the design-doc default thresholds (the
    # actual policy gate uses PolicyConfig's possibly-tuned values).
    has_red_flag = (
        spread_bps_resolved > 50.0
        or rvol_percentile < 20.0
        or volume_ratio < 0.7
    )

    # Distance to VWAP in ATR units (signed)
    current_close = _safe_last(df_ind, "close", 0.0)
    vwap = _safe_last(df_ind, "vwap", 0.0)
    if daily_atr > 0 and vwap > 0:
        distance_to_vwap_atr = (current_close - vwap) / daily_atr
    else:
        distance_to_vwap_atr = 0.0

    # Distance to stop / target — None when not holding
    distance_to_stop_atr: float | None = None
    distance_to_target_atr: float | None = None
    if position and position.get("qty", 0) != 0 and daily_atr > 0:
        if stop_price is not None:
            distance_to_stop_atr = (current_close - stop_price) / daily_atr
        if target_price is not None:
            distance_to_target_atr = (target_price - current_close) / daily_atr

    return MarketFeatures(
        rvol_percentile=rvol_percentile,
        spread_bps=spread_bps_resolved,
        distance_to_vwap_atr=distance_to_vwap_atr,
        distance_to_stop_atr=distance_to_stop_atr,
        distance_to_target_atr=distance_to_target_atr,
        has_red_flag=has_red_flag,
    )


# ---------------------------------------------------------------------------
# AccountState builder
# ---------------------------------------------------------------------------


def build_account_state(
    *,
    equity: float,
    open_positions: list[Any],
    current_price_lookup: dict[str, float] | None = None,
) -> AccountState:
    """Assemble an AccountState from broker-reported positions.

    ``open_positions`` is a list of position objects (anything with
    ``ticker``, ``quantity``, ``current_price`` attributes). The
    builder is duck-typed so production code can pass Alpaca's
    Position objects directly, and tests can pass simple dataclasses.

    ``current_price_lookup``: optional override of position prices
    when the broker-reported ``current_price`` is stale. Pass a
    ``{ticker: price}`` dict; missing tickers fall back to the
    position's own ``current_price``.

    ``total_exposure_pct`` is computed as the sum of |qty| × price
    across all positions, divided by equity. Defaults to 0.0 when
    equity is 0 or negative (the orchestrator should already have
    surfaced a warning before this point per Rule 18)."""
    if equity <= 0:
        return AccountState(
            equity=equity,
            total_exposure_pct=0.0,
            open_position_count=len(open_positions),
        )

    total_notional = 0.0
    for pos in open_positions:
        qty = abs(int(getattr(pos, "quantity", 0)))
        ticker = getattr(pos, "ticker", None)
        price = (
            current_price_lookup.get(ticker, getattr(pos, "current_price", 0.0))
            if current_price_lookup
            else getattr(pos, "current_price", 0.0)
        )
        total_notional += qty * float(price)

    total_exposure_pct = total_notional / equity * 100.0

    return AccountState(
        equity=equity,
        total_exposure_pct=total_exposure_pct,
        open_position_count=len(open_positions),
    )


# ---------------------------------------------------------------------------
# Default LLMAnalysis (bridge until the LLM tier emits it)
# ---------------------------------------------------------------------------


def synthesize_default_analysis(
    *,
    catalyst_quality: CatalystQuality = CatalystQuality.NONE,
    setup_type: SetupType = SetupType.NO_SETUP,
    trade_readiness: TradeReadiness = TradeReadiness.READY,
    position_action: PositionAction = PositionAction.NO_OPINION,
) -> LLMAnalysis:
    """Return a permissive LLMAnalysis used until the LLM tier emits
    a real one (parked as a separate followup task).

    Defaults are chosen so the policy's classification-aware gates
    don't fire:
      - ``TradeReadiness.READY`` → step 2 AVOID gate doesn't fire
      - ``PositionAction.NO_OPINION`` → step 4b returns Hold without
        triggering position management

    The bucket-key derivation still reads ``catalyst_quality``, so the
    default of ``NONE`` is meaningful: it routes the candidate to the
    "no catalyst" bucket. Callers that DO have catalyst info available
    can override.

    Override-able so tests can exercise specific policy paths without
    constructing an LLMAnalysis from scratch.
    """
    return LLMAnalysis(
        catalyst_quality=catalyst_quality,
        setup_type=setup_type,
        trade_readiness=trade_readiness,
        invalid_if="no LLM analysis available; default bridge analysis in use",
        counter_thesis="bridge analysis is a placeholder until LLMOutput refactor",
        position_action=position_action,
        position_action_reasoning="bridge analysis: no real classification yet",
    )


__all__ = [
    "DEFAULT_SPREAD_BPS",
    "build_llm_context",
    "build_market_features",
    "build_account_state",
    "synthesize_default_analysis",
]
