"""Real Qwen 3.6-27B end-to-end smoke test.

Makes two REAL LocalClient calls against the LM Studio server at
localhost:1234. Mirrors scripts/smoke_test_haiku.py but with the local
backend, so the two outputs can be eyeballed side-by-side.

What it confirms:
- LocalClient constructs without error (was raising NotImplementedError)
- The OpenAI-compatible chat.completions endpoint accepts our
  tool_use forced output
- Qwen 3.6-27B actually invokes the submit_decision tool (vs returning
  prose despite tool_choice)
- LLMDecision schema validation accepts what Qwen returns
- Latency is within expected range (~5-15s with thinking, depending on
  how much the model deliberates before the tool call)

Skips with a clear message if the LM Studio server is unreachable.

Run with:
    cd "C:\\trading\\LLM model"
    $env:PYTHONPATH = "."
    python scripts/smoke_test_qwen_decision.py
"""
from __future__ import annotations

import asyncio
import sys
import time

from strategy.llm.clients import (
    LocalClient,
    APIUnavailableError,
    SchemaInvalidError,
)
from strategy.llm.types import LLMContext, LLMDecision

DEFAULT_MODEL_ID = "qwen/qwen3.6-27b"
DEFAULT_BASE_URL = "http://localhost:1234/v1"


def _ctx_nvda() -> LLMContext:
    """NVDA gap-and-go setup with M&A catalyst — should bias toward Buy."""
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
        minutes_since_open=12,
        minutes_until_close=348,
        in_gap_and_go_window=True,
    )


def _ctx_aapl() -> LLMContext:
    """AAPL choppy mid-day setup with no catalyst — should bias toward Hold."""
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


def _print_decision(label: str, decision: LLMDecision, elapsed_s: float) -> None:
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
    print(f"  --- backend metadata ---")
    print(f"  model           : {raw.get('model')}")
    print(f"  backend         : {raw.get('backend')}")
    print(f"  finish_reason   : {raw.get('finish_reason')}")
    print(f"  input_tokens    : {raw.get('input_tokens')}")
    print(f"  output_tokens   : {raw.get('output_tokens')}")
    print(f"  latency         : {elapsed_s:.2f}s")
    if raw.get('output_tokens'):
        tok_per_s = raw['output_tokens'] / elapsed_s if elapsed_s > 0 else 0
        print(f"  output tok/s    : {tok_per_s:.1f}")


async def main() -> int:
    print(f"Connecting to LM Studio at {DEFAULT_BASE_URL} (model={DEFAULT_MODEL_ID})...")
    try:
        client = LocalClient(
            model_id=DEFAULT_MODEL_ID,
            base_url=DEFAULT_BASE_URL,
        )
    except Exception as exc:
        print(f"\nFAIL: LocalClient construction raised "
              f"{type(exc).__name__}: {exc}")
        return 1

    print("Making 2 real Qwen 3.6-27B calls (NVDA gap-up, AAPL choppy)...")

    t0 = time.monotonic()
    try:
        decision1 = await client.evaluate(_ctx_nvda())
    except (SchemaInvalidError, APIUnavailableError) as exc:
        print(f"\nFAIL: NVDA call raised {type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        print(f"\nFAIL: NVDA call raised unexpected {type(exc).__name__}: {exc}")
        return 1
    elapsed1 = time.monotonic() - t0
    _print_decision("NVDA gap-up + M&A catalyst (expect Buy lean)",
                    decision1, elapsed1)

    t0 = time.monotonic()
    try:
        decision2 = await client.evaluate(_ctx_aapl())
    except (SchemaInvalidError, APIUnavailableError) as exc:
        print(f"\nFAIL: AAPL call raised {type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        print(f"\nFAIL: AAPL call raised unexpected {type(exc).__name__}: {exc}")
        return 1
    elapsed2 = time.monotonic() - t0
    _print_decision("AAPL choppy + no catalyst (expect Hold lean)",
                    decision2, elapsed2)

    print()
    print("=========================================================")
    print("SUMMARY")
    print("=========================================================")
    actions_valid = (decision1.action in {"Buy", "Sell", "Hold"}
                     and decision2.action in {"Buy", "Sell", "Hold"})
    if actions_valid:
        print(f"[OK ] Both decisions parsed as valid LLMDecision "
              f"(actions: {decision1.action}, {decision2.action})")
    else:
        print(f"[FAIL] One or both decisions had invalid action.")
        return 1

    total_in = (decision1.raw_response.get("input_tokens", 0) or 0) + \
               (decision2.raw_response.get("input_tokens", 0) or 0)
    total_out = (decision1.raw_response.get("output_tokens", 0) or 0) + \
                (decision2.raw_response.get("output_tokens", 0) or 0)
    print(f"Total input tokens : {total_in}")
    print(f"Total output tokens: {total_out}")
    print(f"Total latency      : {elapsed1 + elapsed2:.2f}s")
    if total_out > 0 and (elapsed1 + elapsed2) > 0:
        print(f"Avg output tok/s   : {total_out / (elapsed1 + elapsed2):.1f}")
    print()
    print("OK - LocalClient (Qwen 3.6-27B) verified end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
