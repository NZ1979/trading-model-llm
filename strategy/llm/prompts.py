"""Prompt templates for the LLM signal generator.

Version-pinned Python constants. To update a prompt, copy the v1
constants to v2 with the new text and bump ``prompt_version`` in
``config/settings.yaml``; the cache keys include the version, so v1
cached responses don't get reused against v2 queries.

Cache-friendly structure (see ``docs/LLM_SIGNAL_INTERFACE.md`` §
"Prompt caching strategy"):

- **System prompt** (breakpoint 1, ephemeral): role + decision
  criteria + tool reminder. Stable across cycles within a
  prompt_version. Cache invalidates when prompt_version bumps.
- **Market context** (breakpoint 2, ephemeral): SPY/VIX/regime —
  cycle-stable, refreshed each 5-min bar. Cache invalidates each
  cycle but hits across all candidates within a cycle.
- **Ticker block** (no cache_control): per-candidate variable.
  Always sent fresh.

The render function returns a ``dict`` with ``system`` and ``messages``
keys, ready to **-unpack into ``client.messages.create(...)``.
"""
from __future__ import annotations

from typing import Any

from .types import LLMContext


# ============================================================================
# v1.0 prompt templates
# ============================================================================

_SYSTEM_PROMPT_V1 = """\
You are an intraday equity trader evaluating a single candidate per
call. You make conservative, high-conviction decisions. You prefer
Hold over forcing a marginal trade.

Decision criteria:
- Hold is the default. Only return Buy or Sell if you see a setup
  worth ~0.5% account risk.
- Confidence below 40 should always be Hold. Low-conviction trades
  lose money historically.
- Counter-trend trades (Buy in bear regime, Sell in bull regime) need
  exceptional justification, typically a clear catalyst.
- Stops must be at least 1.0 ATR (tighter gets noised out) and at
  most 3.0 ATR (wider blows the per-trade risk budget).
- Stop and take-profit are expressed as multiples of daily ATR(14);
  the platform applies them at execution.

Forward predictions (Buy/Sell only — leave at default 0 for Hold):
- expected_move_pct: your point estimate of the price move from
  entry over the trade horizon, as a signed percent. Positive for
  Buy, negative for Sell. Be honest; the calibrator will catch
  systematic over/under-prediction over time. Typical intraday
  range is +/- 1% to +/- 5%; +/- 10% is exceptional.
- expected_holding_minutes: how long you expect the trade to take
  to play out. 0 means "no opinion." 30-120 min is typical for
  intraday momentum; 5-15 min for scalps; >180 only if time_horizon
  is overnight or multi_day.

Output discipline:
- ALWAYS submit your decision via the submit_decision tool.
- NEVER respond with prose outside the tool call.
- Keep reasoning under 280 characters; concerns to <=5 short tags.

Prompt version: {prompt_version}
"""

_MARKET_CONTEXT_TEMPLATE_V1 = """\
# Market context (cycle-shared)
SPY today: {spy_change_pct:+.2f}% on {spy_rvol:.1f}x normal volume.
VIX: {vix_str}.
Regime: {market_regime_label}.
"""

_TICKER_BLOCK_TEMPLATE_V1 = """\
# Evaluating {ticker} at {timestamp_et}
{sector} / {market_cap_bucket} cap (ADV ~{avg_daily_volume:,} sh)

## Daily regime
{daily_regime} (close ${current_close:.2f} vs SMA200 ${sma_200:.2f}, \
ADX {daily_adx_14:.1f}, ATR ${daily_atr_14:.2f})
Last 5 daily closes: {last_5_closes_str}

## Pre-market
Gap: {gap_pct:+.2f}% on {pm_rvol:.1f}x normal premarket volume.
PM range: {pm_range_str}.
PM volume: {pm_volume:,}.

## Intraday (current 5-min bar)
Close ${current_close:.2f}, volume {volume_ratio_vs_20bar:.1f}x recent average.
RSI(14) {rsi_14:.1f}. MACD histogram {macd_hist_3bar_trend} ({macd_hist:+.4f}).
VWAP ${vwap:.2f} ({distance_to_vwap_pct:+.2f}%).
Bollinger position {bollinger_position:+.2f} (-1=lower band, +1=upper band).
Bars warmed: {current_5min_bar_count}/50.

## Last 10 5-min bars
{bars_table}

## Recent news (last 24h, sentiment-scored)
{news_block}{catalyst_line}

## Earnings flags
Today: {has_earnings_today}. Within 3d: {has_earnings_within_3d}.

## Position state
{position_block}

## Today's prior decisions on {ticker}
{prior_decisions_block}

## Time-of-day
{minutes_since_open} min since open, {minutes_until_close} until close.
In gap-and-go window (09:35-10:00 ET): {in_gap_and_go_window}.

# Decision
Apply the criteria from the system prompt. Submit one decision via the
submit_decision tool.
"""


# ============================================================================
# Per-field formatters
# ============================================================================


def _format_vix(ctx: LLMContext) -> str:
    return f"{ctx.vix_level:.1f}" if ctx.vix_level is not None else "n/a"


def _format_pm_range(ctx: LLMContext) -> str:
    if ctx.pm_high is None or ctx.pm_low is None:
        return "n/a"
    return f"{ctx.pm_low:.2f} - {ctx.pm_high:.2f}"


def _format_last_5_closes(ctx: LLMContext) -> str:
    if not ctx.last_5_daily_closes:
        return "n/a"
    return ", ".join(f"{c:.2f}" for c in ctx.last_5_daily_closes)


def _format_bars_table(ctx: LLMContext) -> str:
    if not ctx.last_10_5min_bars:
        return "(no bars yet)"
    lines = []
    for bar in ctx.last_10_5min_bars:
        lines.append(
            f"  {bar.get('ts', '?')}: "
            f"O={float(bar.get('o', 0)):.2f} "
            f"H={float(bar.get('h', 0)):.2f} "
            f"L={float(bar.get('l', 0)):.2f} "
            f"C={float(bar.get('c', 0)):.2f} "
            f"V={int(bar.get('v', 0)):,}"
        )
    return "\n".join(lines)


def _format_news_block(ctx: LLMContext) -> str:
    if not ctx.news_items:
        return "(no news in last 24h)"
    lines = []
    for item in ctx.news_items:
        lines.append(
            f"  [{item.get('ts', '?')}] "
            f"{item.get('headline', '')} "
            f"(sentiment: {item.get('sentiment_score', 0)}, "
            f"src: {item.get('source', '?')})"
        )
    return "\n".join(lines)


def _format_catalyst_line(ctx: LLMContext) -> str:
    if not ctx.catalyst_flags:
        return ""
    return f"\nCatalysts flagged: {', '.join(ctx.catalyst_flags)}."


def _format_position_block(ctx: LLMContext) -> str:
    if not ctx.currently_holding:
        return "Flat (no position)."
    side = "LONG" if ctx.position_qty > 0 else "SHORT"
    pl = (
        f"{ctx.position_unrealized_pl_pct:+.2f}%"
        if ctx.position_unrealized_pl_pct is not None
        else "n/a"
    )
    avg = (
        f"${ctx.position_avg_price:.2f}"
        if ctx.position_avg_price is not None
        else "n/a"
    )
    stop = "active" if ctx.has_active_stop else "NO STOP"
    return (
        f"{side} {abs(ctx.position_qty)} @ {avg}, "
        f"unrealized {pl}, stop: {stop}."
    )


def _format_prior_decisions_block(ctx: LLMContext) -> str:
    if not ctx.todays_prior_decisions:
        return "(none)"
    lines = []
    for d in ctx.todays_prior_decisions:
        lines.append(
            f"  [{d.get('ts', '?')}] "
            f"{d.get('action', '?')} "
            f"({d.get('setup_label', '?')}, "
            f"conf={d.get('confidence', 0)})"
        )
    return "\n".join(lines)


# ============================================================================
# Public render function
# ============================================================================


def render_messages(ctx: LLMContext) -> dict[str, Any]:
    """Build the ``system`` + ``messages`` args for an Anthropic call.

    Two ephemeral cache breakpoints: one on the system prompt
    (cross-cycle stable within a prompt_version), one on the
    market-context user block (cycle-stable). The per-ticker user
    block carries no ``cache_control``.

    Returns:
        dict with keys ``system`` and ``messages``, ready to
        ``**``-unpack into ``client.messages.create(...)``.
    """
    system_text = _SYSTEM_PROMPT_V1.format(prompt_version=ctx.prompt_version)

    market_text = _MARKET_CONTEXT_TEMPLATE_V1.format(
        spy_change_pct=ctx.spy_change_pct,
        spy_rvol=ctx.spy_rvol,
        vix_str=_format_vix(ctx),
        market_regime_label=ctx.market_regime_label,
    )

    ticker_text = _TICKER_BLOCK_TEMPLATE_V1.format(
        ticker=ctx.ticker,
        timestamp_et=ctx.timestamp_et,
        sector=ctx.sector,
        market_cap_bucket=ctx.market_cap_bucket,
        avg_daily_volume=ctx.avg_daily_volume,
        daily_regime=ctx.daily_regime,
        current_close=ctx.current_close,
        sma_200=ctx.sma_200,
        daily_adx_14=ctx.daily_adx_14,
        daily_atr_14=ctx.daily_atr_14,
        last_5_closes_str=_format_last_5_closes(ctx),
        gap_pct=ctx.gap_pct,
        pm_rvol=ctx.pm_rvol,
        pm_range_str=_format_pm_range(ctx),
        pm_volume=ctx.pm_volume,
        volume_ratio_vs_20bar=ctx.volume_ratio_vs_20bar,
        rsi_14=ctx.rsi_14,
        macd_hist_3bar_trend=ctx.macd_hist_3bar_trend,
        macd_hist=ctx.macd_hist,
        vwap=ctx.vwap,
        distance_to_vwap_pct=ctx.distance_to_vwap_pct,
        bollinger_position=ctx.bollinger_position,
        current_5min_bar_count=ctx.current_5min_bar_count,
        bars_table=_format_bars_table(ctx),
        news_block=_format_news_block(ctx),
        catalyst_line=_format_catalyst_line(ctx),
        has_earnings_today=ctx.has_earnings_today,
        has_earnings_within_3d=ctx.has_earnings_within_3d,
        position_block=_format_position_block(ctx),
        prior_decisions_block=_format_prior_decisions_block(ctx),
        minutes_since_open=ctx.minutes_since_open,
        minutes_until_close=ctx.minutes_until_close,
        in_gap_and_go_window=ctx.in_gap_and_go_window,
    )

    return {
        "system": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": market_text,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": ticker_text,
                    },
                ],
            },
        ],
    }
