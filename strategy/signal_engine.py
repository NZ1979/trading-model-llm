"""Trade decision engine.

Combines three independent signals into a single Buy/Sell/Hold decision:
  1. Sentiment score (from analysis/sentiment.py, scored by Claude Haiku)
  2. Technical signal (from analysis/indicators.py, with setup type)
  3. Futures walls (from analysis/futures_walls.py, ES order book context)

Two paths through the logic, selected by technical_signal.setup:

PULLBACK (mean-reversion in trend) — strict per original spec:
  BUY  requires: technical_signal=Buy AND sentiment >= +5
                 AND ES support wall within 1% below mid
                 AND no ES resistance within 0.3% above mid (overhead block)
  SELL requires: technical_signal=Sell AND sentiment <= -5
                 AND ES resistance wall within 1% above mid
                 AND no ES support within 0.3% below mid (underfoot block)

GAP_AND_GO (news-driven momentum) — relaxed:
  BUY  requires: technical_signal=Buy AND sentiment >= +3
                 walls do NOT need to align (single-stock news drives the move,
                 not index direction); but overhead resistance still blocks
  SELL: symmetric

If futures_walls is None (Databento not running, warmup, or disabled), the
behavior is configurable via require_walls_for_pullback. Default True =
strict spec; False = walls become confirming-only.

Why the asymmetry between setups?
  Pullbacks fade entries; you need every confluence aligned because you're
  fighting the current 5-min direction. Gap-and-go is a momentum trade
  riding a news event; the news IS the catalyst and ES walls are less
  relevant since the gap is idiosyncratic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from analysis.futures_walls import Wall
from analysis.indicators import TechnicalSignal

Action = Literal["Buy", "Sell", "Hold"]
WallsStatus = Literal["aligned", "blocking", "absent", "n/a"]


# Thresholds. Override via config.yaml in production.
PULLBACK_SENTIMENT_MIN_BUY = 5
PULLBACK_SENTIMENT_MIN_SELL = -5
GAP_AND_GO_SENTIMENT_MIN_BUY = 3
GAP_AND_GO_SENTIMENT_MIN_SELL = -3
WALL_NEARBY_PCT = 1.0      # support/resistance must be within this % of ES mid
WALL_OVERHEAD_PCT = 0.3    # immediate-blocking distance for opposite-side wall


@dataclass(frozen=True, slots=True)
class TradeDecision:
    action: Action
    ticker: str
    setup: str  # "pullback" | "gap_and_go" | "none"
    sentiment_score: int | None
    technical_confidence: int
    walls_status: WallsStatus
    reasons: tuple[str, ...]


def evaluate_trade(
    ticker: str,
    sentiment_score: int | None,
    technical_signal: TechnicalSignal,
    futures_walls: tuple[list[Wall], list[Wall]] | None,
    require_walls_for_pullback: bool = True,
) -> TradeDecision:
    """Make a final Buy/Sell/Hold call by combining three signals.

    Args:
        ticker: equity symbol.
        sentiment_score: -10..+10 from latest sentiment row for this ticker
            within the last hour, or None if no recent news.
        technical_signal: result of analysis.indicators.generate_signal().
        futures_walls: (support_walls, resistance_walls) from
            FuturesWallMonitor.walls(), or None if monitor isn't running.
        require_walls_for_pullback: if True (default, matches spec), pullback
            setups need wall alignment to fire. If False, walls become
            confirming-only and a setup can fire without them.

    Returns:
        TradeDecision with the action and a tuple of reasons for journaling.
    """
    base_kwargs = dict(
        ticker=ticker,
        setup=technical_signal.setup,
        sentiment_score=sentiment_score,
        technical_confidence=technical_signal.confidence,
    )

    # Technical-side Hold short-circuits everything.
    if technical_signal.signal == "Hold":
        return TradeDecision(
            action="Hold",
            walls_status="n/a",
            reasons=("technical_hold",) + technical_signal.reasons,
            **base_kwargs,
        )

    direction = technical_signal.signal  # "Buy" or "Sell"
    setup = technical_signal.setup
    is_buy = direction == "Buy"

    # Pick sentiment thresholds per setup type.
    if setup == "pullback":
        thr_buy, thr_sell = PULLBACK_SENTIMENT_MIN_BUY, PULLBACK_SENTIMENT_MIN_SELL
    elif setup == "gap_and_go":
        thr_buy, thr_sell = GAP_AND_GO_SENTIMENT_MIN_BUY, GAP_AND_GO_SENTIMENT_MIN_SELL
    else:
        return TradeDecision(
            action="Hold", walls_status="n/a",
            reasons=("unknown_setup", setup), **base_kwargs,
        )

    # Sentiment gate.
    threshold = thr_buy if is_buy else thr_sell
    if is_buy:
        sentiment_ok = sentiment_score is not None and sentiment_score >= threshold
    else:
        sentiment_ok = sentiment_score is not None and sentiment_score <= threshold

    walls_status, walls_reasons = _wall_status(futures_walls, is_buy)
    reasons: list[str] = list(technical_signal.reasons) + walls_reasons

    if not sentiment_ok:
        op = "<" if is_buy else ">"
        reasons.append(f"sentiment={sentiment_score}{op}{threshold}")
        return TradeDecision(
            action="Hold", walls_status=walls_status,
            reasons=tuple(reasons), **base_kwargs,
        )

    # Walls block on opposite-side overhead/underfoot walls regardless of setup.
    if walls_status == "blocking":
        return TradeDecision(
            action="Hold", walls_status=walls_status,
            reasons=tuple(reasons), **base_kwargs,
        )

    # Pullback strict mode requires walls aligned (per original spec).
    if setup == "pullback" and require_walls_for_pullback and walls_status != "aligned":
        reasons.append("walls_required_for_pullback_not_aligned")
        return TradeDecision(
            action="Hold", walls_status=walls_status,
            reasons=tuple(reasons), **base_kwargs,
        )

    # All gates passed.
    op = ">=" if is_buy else "<="
    reasons.append(f"sentiment={sentiment_score}{op}{threshold}")
    return TradeDecision(
        action=direction, walls_status=walls_status,
        reasons=tuple(reasons), **base_kwargs,
    )


def _wall_status(
    futures_walls: tuple[list[Wall], list[Wall]] | None,
    is_buy: bool,
) -> tuple[WallsStatus, list[str]]:
    """Determine if ES walls support, block, or are absent for this direction.

    For a BUY:
      - Aligned: ES support within WALL_NEARBY_PCT below mid (market floor nearby).
      - Blocking: ES resistance within WALL_OVERHEAD_PCT above mid (overhead lid).
      - Absent: neither (or no walls available at all).

    For a SELL: symmetric. Aligned = resistance close above; Blocking = support
    close below; Absent = neither.

    Note Wall.distance_pct convention: support is negative (below mid), resistance
    is positive (above mid).
    """
    if futures_walls is None:
        return ("absent", ["walls_unavailable"])

    support, resistance = futures_walls

    if is_buy:
        has_overhead = any(
            0 < w.distance_pct <= WALL_OVERHEAD_PCT for w in resistance
        )
        if has_overhead:
            return ("blocking", [f"overhead_resistance_within_{WALL_OVERHEAD_PCT}%"])
        has_close_support = any(
            -WALL_NEARBY_PCT <= w.distance_pct <= 0 for w in support
        )
        if has_close_support:
            return ("aligned", [f"support_within_{WALL_NEARBY_PCT}%"])
        return ("absent", [f"no_support_within_{WALL_NEARBY_PCT}%"])

    # is_sell
    has_underfoot = any(
        -WALL_OVERHEAD_PCT <= w.distance_pct < 0 for w in support
    )
    if has_underfoot:
        return ("blocking", [f"underfoot_support_within_{WALL_OVERHEAD_PCT}%"])
    has_close_resistance = any(
        0 <= w.distance_pct <= WALL_NEARBY_PCT for w in resistance
    )
    if has_close_resistance:
        return ("aligned", [f"resistance_within_{WALL_NEARBY_PCT}%"])
    return ("absent", [f"no_resistance_within_{WALL_NEARBY_PCT}%"])
