"""Pluggable signal modules for model-specific trading strategies.

Each model (gap-and-go, large-cap-mean-reversion, options-OI, etc.)
can register its own signal modules in this directory. The dispatcher
in `strategy/signal_engine.py` (and the legacy `analysis.indicators.
generate_signal` shim) picks them up by name.

A signal module exports a single public function:

    def check_NAME(
        intraday_df: pd.DataFrame,
        daily_ctx: DailyContext | None,
        premarket_ctx: PremarketContext | None,
    ) -> TechnicalSignal | None: ...

Returning None means "this signal doesn't apply to the current state —
move on to the next signal in the dispatcher's chain." Returning a
TechnicalSignal (Buy / Sell / Hold) is the final answer for that bar.

Extracted 2026-05-06 from analysis/indicators.py as part of preparing
the base codebase for model-specific forks.
"""
from strategy.signals.gap_and_go import check_gap_and_go
from strategy.signals.pullback import check_pullback

__all__ = ["check_gap_and_go", "check_pullback"]
