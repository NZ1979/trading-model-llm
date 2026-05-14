"""Replay-harness data layer.

Modules in this package are used only by the M2 replay harness
(``scripts/replay_with_llm.py``). The live signal path does not import
from here.

Submodule layout:

- ``config``: ``ReplayConfig`` dataclass + ``cache_key`` helper.
- ``historical_bars``: point-in-time bar loader (Polygon).
- ``historical_news``: point-in-time news loader (Polygon News).
- ``historical_sentiment``: read from one-time curated fixture (Rule 26
  forbids querying trader-prod's live DB at runtime).
- ``market_context``: SPY + VIX context per replay tick.
- ``ticker_metadata``: sector / market-cap-bucket lookup, cached locally.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md``.
"""
