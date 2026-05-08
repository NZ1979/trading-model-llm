"""Verification for strategy.llm.prompts.render_messages (B.2).

Confirms:
- Output shape: dict with 'system' and 'messages' keys
- Two cache_control breakpoints in the right positions: system block
  and the first user content block (market context). The per-ticker
  user block has NO cache_control.
- prompt_version is embedded in the system prompt text (so cache key
  invalidates on prompt bump).
- Ticker fields render with proper formatting (float precision,
  thousands separator on volumes).
- Empty-data fallbacks ('n/a', '(no news)', '(no bars yet)', etc.).
- Position block: flat vs long vs short + unrealized P&L formatting.
- News block + catalyst line concatenation.
- Snapshot dump: print full rendered prompt for a sample context so
  we can eyeball it.

Run with:
    cd C:\\trading\\LLM model
    $env:PYTHONPATH = "."
    python scripts/verify_prompts.py
"""
from __future__ import annotations

import sys

from strategy.llm.prompts import render_messages
from strategy.llm.types import LLMContext


def _print(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _full_ctx() -> LLMContext:
    """Production-shaped context with every field populated."""
    return LLMContext(
        ticker="NVDA",
        timestamp_et="2026-05-08 09:42:00 ET",
        prompt_version="v1.0",
        catalyst_flags=("M&A",),
        pm_rvol=4.2,
        gap_pct=3.1,
        pm_high=149.8,
        pm_low=147.2,
        pm_volume=2_500_000,
        spy_change_pct=0.45,
        spy_rvol=1.1,
        vix_level=14.2,
        market_regime_label="trending_up",
        sector="Technology",
        market_cap_bucket="mega",
        avg_daily_volume=280_000_000,
        daily_regime="bull",
        daily_adx_14=28.5,
        daily_atr_14=4.20,
        sma_200=122.10,
        last_5_daily_closes=(144.20, 145.80, 147.10, 144.50, 148.50),
        current_close=148.85,
        current_volume=180_000,
        current_5min_bar_count=2,
        last_10_5min_bars=(
            {"ts": "09:35", "o": 148.50, "h": 149.10, "l": 148.30, "c": 148.85, "v": 95_000},
            {"ts": "09:40", "o": 148.85, "h": 149.30, "l": 148.70, "c": 149.10, "v": 85_000},
        ),
        rsi_14=62.5,
        macd_hist=0.0125,
        macd_hist_3bar_trend="rising",
        vwap=148.62,
        distance_to_vwap_pct=0.15,
        bollinger_position=0.45,
        volume_ratio_vs_20bar=1.8,
        news_items=(
            {
                "ts": "09:15",
                "headline": "NVDA announces strategic AI partnership with Palantir",
                "sentiment_score": 8,
                "source": "Benzinga",
            },
        ),
        has_earnings_today=False,
        has_earnings_within_3d=False,
        currently_holding=False,
        minutes_since_open=12,
        minutes_until_close=348,
        in_gap_and_go_window=True,
    )


def _empty_ctx() -> LLMContext:
    """Minimal context: only the required fields."""
    return LLMContext(
        ticker="AAPL",
        timestamp_et="2026-05-08 09:30:00 ET",
        prompt_version="v0.0-stub",
    )


def _holding_ctx() -> LLMContext:
    """Context with an open long position + prior decisions."""
    return LLMContext(
        ticker="MSFT",
        timestamp_et="2026-05-08 11:30:00 ET",
        prompt_version="v1.0",
        currently_holding=True,
        position_qty=50,
        position_avg_price=415.20,
        position_unrealized_pl_pct=1.85,
        has_active_stop=True,
        todays_prior_decisions=(
            {"ts": "10:15", "action": "Buy", "setup_label": "pullback-in-trend", "confidence": 72},
            {"ts": "10:45", "action": "Hold", "setup_label": "managing", "confidence": 60},
        ),
    )


def _check_shape(out: dict, label: str = "full") -> bool:
    ok = True
    ok &= _print(
        f"{label}: top-level keys = system + messages",
        set(out.keys()) == {"system", "messages"},
        f"keys={sorted(out.keys())}",
    )
    ok &= _print(
        f"{label}: system has 1 block, type=text, cache_control=ephemeral",
        len(out["system"]) == 1
        and out["system"][0]["type"] == "text"
        and out["system"][0]["cache_control"] == {"type": "ephemeral"},
    )
    ok &= _print(
        f"{label}: messages has 1 user message, role=user",
        len(out["messages"]) == 1 and out["messages"][0]["role"] == "user",
    )
    content = out["messages"][0]["content"]
    ok &= _print(
        f"{label}: user content has 2 blocks (market + ticker)",
        len(content) == 2,
        f"got {len(content)} blocks",
    )
    ok &= _print(
        f"{label}: market block has cache_control ephemeral",
        content[0].get("cache_control") == {"type": "ephemeral"}
        and content[0]["type"] == "text",
    )
    ok &= _print(
        f"{label}: ticker block has NO cache_control",
        "cache_control" not in content[1] and content[1]["type"] == "text",
    )
    return ok


def main() -> int:
    all_ok = True

    # ---- Full context ----
    full_out = render_messages(_full_ctx())
    all_ok &= _check_shape(full_out, "full")

    sys_text = full_out["system"][0]["text"]
    market_text = full_out["messages"][0]["content"][0]["text"]
    ticker_text = full_out["messages"][0]["content"][1]["text"]

    all_ok &= _print(
        "full: prompt_version embedded in system prompt",
        "v1.0" in sys_text and "Prompt version:" in sys_text,
    )
    all_ok &= _print(
        "full: market block contains SPY/VIX/regime",
        "+0.45%" in market_text
        and "VIX: 14.2" in market_text
        and "trending_up" in market_text,
    )
    all_ok &= _print(
        "full: ticker block contains ticker, timestamp, sector",
        "NVDA" in ticker_text
        and "2026-05-08 09:42:00 ET" in ticker_text
        and "Technology" in ticker_text,
    )
    all_ok &= _print(
        "full: ADV uses thousands separator",
        "280,000,000" in ticker_text,
    )
    all_ok &= _print(
        "full: bars table renders with all bars",
        "09:35" in ticker_text
        and "O=148.50" in ticker_text
        and "V=95,000" in ticker_text,
    )
    all_ok &= _print(
        "full: news headline renders with sentiment",
        "Palantir" in ticker_text and "sentiment: 8" in ticker_text,
    )
    all_ok &= _print(
        "full: catalyst flag rendered",
        "Catalysts flagged: M&A" in ticker_text,
    )
    all_ok &= _print(
        "full: 5 daily closes formatted",
        "144.20, 145.80, 147.10, 144.50, 148.50" in ticker_text,
    )
    all_ok &= _print(
        "full: position block shows flat",
        "Flat (no position)" in ticker_text,
    )
    all_ok &= _print(
        "full: time-of-day rendered",
        "12 min since open" in ticker_text and "348 until close" in ticker_text,
    )
    all_ok &= _print(
        "full: gap-and-go window flag",
        "In gap-and-go window (09:35-10:00 ET): True" in ticker_text,
    )

    # ---- Empty (defaults) context ----
    empty_out = render_messages(_empty_ctx())
    all_ok &= _check_shape(empty_out, "empty")
    empty_market = empty_out["messages"][0]["content"][0]["text"]
    empty_ticker = empty_out["messages"][0]["content"][1]["text"]

    all_ok &= _print(
        "empty: VIX shows n/a when None",
        "VIX: n/a" in empty_market,
    )
    all_ok &= _print(
        "empty: PM range shows n/a when high/low None",
        "PM range: n/a" in empty_ticker,
    )
    all_ok &= _print(
        "empty: bars table shows '(no bars yet)'",
        "(no bars yet)" in empty_ticker,
    )
    all_ok &= _print(
        "empty: news block shows '(no news in last 24h)'",
        "(no news in last 24h)" in empty_ticker,
    )
    all_ok &= _print(
        "empty: no catalyst line when flags empty",
        "Catalysts flagged:" not in empty_ticker,
    )
    all_ok &= _print(
        "empty: prior decisions shows '(none)'",
        "(none)" in empty_ticker,
    )
    all_ok &= _print(
        "empty: 5 closes shows n/a when empty",
        "Last 5 daily closes: n/a" in empty_ticker,
    )
    all_ok &= _print(
        "empty: regime shows 'unknown'",
        "Regime: unknown" in empty_market,
    )

    # ---- Holding context ----
    hold_out = render_messages(_holding_ctx())
    hold_ticker = hold_out["messages"][0]["content"][1]["text"]
    all_ok &= _print(
        "holding: long position rendered",
        "LONG 50 @ $415.20" in hold_ticker
        and "unrealized +1.85%" in hold_ticker
        and "stop: active" in hold_ticker,
    )
    all_ok &= _print(
        "holding: prior decisions rendered",
        "Buy" in hold_ticker
        and "pullback-in-trend" in hold_ticker
        and "conf=72" in hold_ticker,
    )

    # ---- Length sanity check ----
    sys_chars = len(sys_text)
    market_chars = len(market_text)
    ticker_chars = len(ticker_text)
    total_chars = sys_chars + market_chars + ticker_chars
    print()
    print(f"Rendered length (full ctx, chars):")
    print(f"  system    : {sys_chars:>6}  (~{sys_chars // 4} tokens)")
    print(f"  market    : {market_chars:>6}  (~{market_chars // 4} tokens)")
    print(f"  ticker    : {ticker_chars:>6}  (~{ticker_chars // 4} tokens)")
    print(f"  TOTAL     : {total_chars:>6}  (~{total_chars // 4} tokens)")
    print(f"  (prompt caching saves ~{(sys_chars + market_chars) // 4} tokens per call after first hit)")

    print()
    print("=" * 72)
    print("FULL RENDERED PROMPT (full context) — eyeball check below:")
    print("=" * 72)
    print()
    print("--- SYSTEM ---")
    print(sys_text)
    print("--- MARKET (cache breakpoint 2) ---")
    print(market_text)
    print("--- TICKER (variable) ---")
    print(ticker_text)
    print("=" * 72)

    print()
    print("ALL OK" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
