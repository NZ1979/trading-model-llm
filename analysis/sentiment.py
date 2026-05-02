"""Sentiment scoring via Claude Haiku 4.5.

Headlines are batched (10-20 per call) to amortize the system prompt cost
across many items. With prompt caching on the system prompt, effective input
cost drops to ~$0.10 per million tokens after the first call.

Output contract: list of {ticker, sentiment (-10..+10), reasoning}, in the
same order as the input headlines. If parsing fails, returns []. Caller is
responsible for handling empty results (e.g., retry or skip).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

logger = logging.getLogger(__name__)

SENTIMENT_MODEL = "claude-haiku-4-5-20251001"

SENTIMENT_SYSTEM_PROMPT = """You are a quantitative trading sentiment analyzer for an intraday equity trading system.

For each numbered headline, output a JSON object with exactly these fields:
- "ticker": the primary stock ticker most directly affected (uppercase, e.g., "AAPL"). If multiple companies, pick the one with the largest expected price impact. If none clearly dominant, use "".
- "sentiment": integer from -10 (extremely bearish) to +10 (extremely bullish), measuring expected same-day price impact.
- "reasoning": ONE concise sentence (under 25 words) stating the actionable trading insight.

Scoring guide (calibrate carefully, most news is mildly directional):
- +/-9 to +/-10: Major surprise. Big earnings beat or miss with guidance change, FDA approval or rejection, confirmed acquisition, fraud disclosure.
- +/-5 to +/-8: Material news. Significant guidance change, top-tier analyst upgrade or downgrade, regulatory action, key executive departure.
- +/-2 to +/-4: Notable but not market-moving alone. Minor analyst note, product announcement, secondary regulatory item.
- -1 to +1: Neutral, ambiguous, or already priced in.

Be skeptical of hype language ("revolutionary", "breakthrough") without substance. Score the news, not the rhetoric.

Respond with ONLY a JSON array of objects, in the same order as the input. No prose, no markdown fences, no commentary."""


@dataclass(frozen=True, slots=True)
class SentimentResult:
    ticker: str
    sentiment: int
    reasoning: str
    headline: str
    news_id: int  # Alpaca news ID, for dedup and journaling


class SentimentScorer:
    """Batches headlines and scores them with Claude Haiku.

    At S&P 500 scale with the keyword pre-filter applied upstream, expect
    50-150 scoring calls per session totaling roughly $2-4/day on Haiku 4.5.
    """

    def __init__(self, api_key: str, model: str = SENTIMENT_MODEL) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    def score_batch(self, items: list[dict[str, Any]]) -> list[SentimentResult]:
        """Score a batch of news items in one Claude call.

        Args:
            items: list of dicts with keys: id (int), headline (str),
                summary (str), symbols (list[str]).

        Returns:
            list of SentimentResult, same length and order as items.
            Empty list on parse error (caller decides whether to retry).
        """
        if not items:
            return []

        # Build a numbered prompt. Truncate summaries to keep tokens predictable.
        lines = []
        for i, item in enumerate(items, start=1):
            symbols = ", ".join(item.get("symbols", [])) or "n/a"
            summary = (item.get("summary") or "")[:300]
            line = f"{i}. [tickers: {symbols}] {item['headline']}"
            if summary:
                line += f" || {summary}"
            lines.append(line)
        user_msg = "Score each headline:\n\n" + "\n".join(lines)

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                system=[{
                    "type": "text",
                    "text": SENTIMENT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except Exception:
            logger.exception("Claude API call failed")
            return []

        # Defensive: strip accidental markdown fences if model adds them.
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip().rstrip("`").strip()

        try:
            scores = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Failed to parse sentiment JSON. Raw[:500]: %s", raw[:500])
            return []

        if not isinstance(scores, list) or len(scores) != len(items):
            logger.error(
                "Sentiment count mismatch: got %d, expected %d",
                len(scores) if isinstance(scores, list) else -1,
                len(items),
            )
            return []

        results: list[SentimentResult] = []
        for item, score in zip(items, scores):
            try:
                results.append(SentimentResult(
                    ticker=str(score.get("ticker", "")).upper().strip(),
                    sentiment=max(-10, min(10, int(score.get("sentiment", 0)))),
                    reasoning=str(score.get("reasoning", "")).strip(),
                    headline=item["headline"],
                    news_id=int(item["id"]),
                ))
            except (TypeError, ValueError) as e:
                logger.warning("Skipping malformed score %s: %s", score, e)

        # Log usage for cost monitoring
        usage = response.usage
        logger.info(
            "Scored %d headlines | input=%d output=%d cache_read=%d",
            len(results),
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0),
        )
        return results
