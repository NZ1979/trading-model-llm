"""Real Haiku end-to-end smoke test (B.4).

Makes two REAL Haiku API calls with shared system prompt and market
context to exercise both cache breakpoints end-to-end. Total cost
~$0.002 at current Haiku 4.5 rates.

Two contexts share the same market context (SPY/VIX/regime) so cache
breakpoint 2 hits on the second call. The system prompt is identical
across calls so cache breakpoint 1 hits on both calls after the first.

What it confirms:
- Real network path works (no SDK shape mismatch hiding behind mocks)
- Haiku actually uses the submit_decision tool (vs returning prose)
- cache_creation_input_tokens / cache_read_input_tokens populate as
  expected — proves the breakpoints are correctly placed
- LLMDecision schema validation accepts what Haiku actually returns
- The ANTHROPIC_API_KEY in this env has tool-use scope

Skips with a clear message if ANTHROPIC_API_KEY is unset.

Run with:
    cd C:\\trading\\LLM model
    $env:PYTHONPATH = "."
    python scripts/smoke_test_haiku.py
"""
from __future__ import annotations

import asyncio
import os
import sys

from strategy.llm.clients import (
    AnthropicClient,
    APIUnavailableError,
    SchemaInvalidError,
)
from strategy.llm.types import LLMContext, LLMDecision


def _ctx_nvda() -> LLMContext:
    """NVDA gap-and-go setup with M&A catalyst — should be a Buy candidate."""
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
        # Market context shared with _ctx_aapl below — cache breakpoint 2 should hit
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
        minutes_since_open=12,
        minutes_until_close=348,
        in_gap_and_go_window=True,
    )


def _ctx_aapl() -> LLMContext:
    """AAPL choppy mid-day setup with no catalyst — should be a Hold candidate.

    Same market context as _ctx_nvda so cache breakpoint 2 hits.
    """
    return LLMContext(
        ticker="AAPL",
        timestamp_et="2026-05-08 09:42:00 ET",
        prompt_version="v1.0",
        catalyst_flags=(),
        pm_rvol=1.1,
        gap_pct=0.15,
        pm_high=215.40,
        pm_low=214.20,
        pm_volume=320_000,
        # Market context — IDENTICAL to _ctx_nvda for cache hit
        spy_change_pct=0.45,
        spy_rvol=1.1,
        vix_level=14.2,
        market_regime_label="trending_up",
        sector="Technology",
        market_cap_bucket="mega",
        avg_daily_volume=55_000_000,
        daily_regime="neutral",
        daily_adx_14=18.2,
        daily_atr_14=3.10,
        sma_200=210.50,
        last_5_daily_closes=(214.10, 215.20, 213.80, 215.10, 214.90),
        current_close=215.05,
        current_volume=42_000,
        current_5min_bar_count=2,
        last_10_5min_bars=(
            {"ts": "09:35", "o": 214.90, "h": 215.20, "l": 214.70, "c": 215.10, "v": 22_000},
            {"ts": "09:40", "o": 215.10, "h": 215.30, "l": 214.95, "c": 215.05, "v": 20_000},
        ),
        rsi_14=51.2,
        macd_hist=0.0008,
        macd_hist_3bar_trend="flat",
        vwap=215.02,
        distance_to_vwap_pct=0.01,
        bollinger_position=0.05,
        volume_ratio_vs_20bar=0.95,
        news_items=(),
        minutes_since_open=12,
        minutes_until_close=348,
        in_gap_and_go_window=True,
    )


def _print_decision(label: str, decision: LLMDecision) -> None:
    print(f"\n--- {label} ---")
    print(f"  action          : {decision.action}")
    print(f"  confidence      : {decision.confidence}")
    print(f"  setup_label     : {decision.setup_label}")
    print(f"  reasoning       : {decision.reasoning}")
    print(f"  stop_loss_atr   : {decision.stop_loss_atr_multiple}")
    print(f"  take_profit_atr : {decision.take_profit_atr_multiple}")
    print(f"  time_horizon    : {decision.time_horizon}")
    if decision.concerns:
        print(f"  concerns        : {decision.concerns}")
    if decision.alternative_view:
        print(f"  alt view        : {decision.alternative_view}")

    raw = decision.raw_response or {}
    print(f"  --- usage ---")
    print(f"  input_tokens               : {raw.get('input_tokens')}")
    print(f"  output_tokens              : {raw.get('output_tokens')}")
    print(f"  cache_creation_input_tokens: {raw.get('cache_creation_input_tokens')}")
    print(f"  cache_read_input_tokens    : {raw.get('cache_read_input_tokens')}")


def _summary(d1: LLMDecision, d2: LLMDecision) -> int:
    """Return 0 if cache behavior matches expectations, 1 otherwise."""
    r1 = d1.raw_response or {}
    r2 = d2.raw_response or {}

    write_1 = r1.get("cache_creation_input_tokens") or 0
    read_1 = r1.get("cache_read_input_tokens") or 0
    write_2 = r2.get("cache_creation_input_tokens") or 0
    read_2 = r2.get("cache_read_input_tokens") or 0

    print("\n=========================================================")
    print("CACHE BEHAVIOR SUMMARY")
    print("=========================================================")
    print(f"Call 1 (NVDA, cold):  write={write_1:>5}  read={read_1:>5}")
    print(f"Call 2 (AAPL, warm):  write={write_2:>5}  read={read_2:>5}")
    print()

    ok = True
    if write_1 > 0:
        print(f"[OK ] Call 1 wrote {write_1} tokens to cache (system + market block).")
    else:
        print(f"[?  ] Call 1 wrote 0 cache tokens. Possible: prompt below cache "
              "minimum (~1024 tokens) or first-call rate-limit deferral.")
    if read_2 > 0:
        print(f"[OK ] Call 2 hit cache for {read_2} tokens (savings ~{read_2 * 0.9 / 1_000_000:.5f}$).")
        ok = ok and True
    else:
        print(f"[FAIL] Call 2 did not hit cache. Breakpoint placement may be wrong.")
        ok = False

    print()
    print("Decision shape sanity:")
    if d1.action in {"Buy", "Sell", "Hold"} and d2.action in {"Buy", "Sell", "Hold"}:
        print(f"[OK ] Both decisions parsed as valid LLMDecision (actions: {d1.action}, {d2.action}).")
    else:
        print(f"[FAIL] One or both decisions had invalid action.")
        ok = False

    # Quick cost estimate (rough Haiku 4.5 rates: $1/MTok in, $5/MTok out;
    # cached read 0.1x = $0.10/MTok; cached write 1.25x = $1.25/MTok)
    in_1 = (r1.get("input_tokens") or 0)
    in_2 = (r2.get("input_tokens") or 0)
    out_1 = (r1.get("output_tokens") or 0)
    out_2 = (r2.get("output_tokens") or 0)
    cost = (
        in_1 * 1.0 + in_2 * 1.0
        + out_1 * 5.0 + out_2 * 5.0
        + write_1 * 1.25 + write_2 * 1.25
        + read_1 * 0.1 + read_2 * 0.1
    ) / 1_000_000
    print(f"\nEstimated total cost for these 2 calls: ${cost:.6f}")

    return 0 if ok else 1


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in this shell. Skipping real-call test.")
        print("To run this test, set the env var and re-run:")
        print("    $env:ANTHROPIC_API_KEY = '<your-key>'")
        print("    python scripts/smoke_test_haiku.py")
        return 0

    print("Making 2 real Haiku 4.5 calls (estimated cost ~$0.002)...")

    client = AnthropicClient(model_id="claude-haiku-4-5")

    try:
        decision1 = await client.evaluate(_ctx_nvda())
    except (SchemaInvalidError, APIUnavailableError) as exc:
        print(f"\nFAIL: Call 1 raised {type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        print(f"\nFAIL: Call 1 raised unexpected {type(exc).__name__}: {exc}")
        return 1

    _print_decision("Call 1: NVDA (gap-up + M&A catalyst, expect Buy lean)", decision1)

    try:
        decision2 = await client.evaluate(_ctx_aapl())
    except (SchemaInvalidError, APIUnavailableError) as exc:
        print(f"\nFAIL: Call 2 raised {type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        print(f"\nFAIL: Call 2 raised unexpected {type(exc).__name__}: {exc}")
        return 1

    _print_decision("Call 2: AAPL (no catalyst, choppy, expect Hold lean)", decision2)

    return _summary(decision1, decision2)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
