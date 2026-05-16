"""Replay-harness configuration.

``ReplayConfig`` is the single source of truth for one replay run. It is
constructed from CLI flags (``scripts/replay_with_llm.py``) or
programmatically in tests; it mirrors the live ``llm:`` settings.yaml
section's shape so a replay can be configured "match production
exactly" or "match production except <field>" without per-field
threading.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § Inputs. Field bounds and
meanings are pinned in that doc. Diffs from the original spec, added
during M2.1 (2026-05-14):

- ``sentiment_fixture_path``: Rule 26 forbids querying trader-prod's
  sentiment table at replay runtime. The harness reads from a one-time
  curated SQLite fixture instead. Export procedure documented at
  ``data/replay/fixtures/README.md`` (created alongside M2.1.b).
- ``t3_max_dollars_per_run``: hard budget cap on Tier 3 (Opus) labeling
  per the design doc's Open Question #6 resolution. Default $500.
- ``max_candidates_per_tick``: pre-filter output cap referenced in the
  design doc's pre_filter pseudocode but not declared in the original
  config dataclass.
- ``news_lag_seconds``: news availability buffer (publication time +
  this many seconds = "visible to LLM") per design doc § News-data
  caveats. Default 30s.

Frozen + slots so ReplayConfig is hashable and can appear in the
``.replay_cache`` key namespace without surprises.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """One replay run's full configuration.

    See module docstring for field provenance and bounds. ``__post_init__``
    fails loud (Rule 18) on out-of-range numerics, bad date ordering,
    and unknown enum values so a typo in a CLI flag or fixture doesn't
    silently degrade the run.
    """

    # ---- Required (no defaults) ----
    start_date: date
    end_date: date
    tickers: tuple[str, ...] | Literal["watchlist"]
    llm_prompt_version: str

    # ---- Tier configuration ----
    t1_backend: str = "qwen_local"
    t1_model_id: str = "qwen3.6-27b-instruct-q4"
    t2_enabled: bool = True
    t2_model_id: str = "claude-sonnet-4-5"
    t2_max_per_day: int = 25
    t3_enabled: bool = True
    t3_model_id: str = "claude-opus-4-6"
    t3_sample_rate: float = 1.0
    t3_max_dollars_per_run: float = 500.0

    # ---- Pre-filter ----
    pre_filter_min_pm_rvol: float = 2.0
    pre_filter_min_gap_pct: float = 1.0
    pre_filter_news_lookback_hours: int = 2
    max_candidates_per_tick: int = 30

    # ---- Simulation ----
    starting_cash: float = 100_000.0
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 20.0
    slippage_bps: float = 5.0
    fill_at: Literal["next_bar_open", "current_close"] = "next_bar_open"
    news_lag_seconds: int = 30

    # ---- Data sources ----
    # Rule 26: harness reads sentiment from this one-time export fixture,
    # NOT from trader-prod's live DB. See data/replay/fixtures/README.md
    # for the export procedure. The path defaults to a location that is
    # gitignored; a missing file at this path is a loud error at load
    # time, not a silent fallback to empty-sentiment.
    sentiment_fixture_path: Path = Path("data/replay/fixtures/sentiment.sqlite")

    # ---- Output ----
    output_dir: Path = Path("docs/reports")
    cache_dir: Path = Path(".replay_cache")

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError(
                f"end_date {self.end_date} is before start_date {self.start_date}"
            )
        if not 0.0 <= self.t3_sample_rate <= 1.0:
            raise ValueError(
                f"t3_sample_rate must be in [0.0, 1.0]; got {self.t3_sample_rate}"
            )
        if self.t3_max_dollars_per_run < 0:
            raise ValueError(
                "t3_max_dollars_per_run must be >= 0; "
                f"got {self.t3_max_dollars_per_run}"
            )
        if self.t2_max_per_day < 0:
            raise ValueError(
                f"t2_max_per_day must be >= 0; got {self.t2_max_per_day}"
            )
        if self.fill_at not in ("next_bar_open", "current_close"):
            raise ValueError(
                "fill_at must be 'next_bar_open' or 'current_close'; "
                f"got {self.fill_at!r}"
            )
        if self.slippage_bps < 0:
            raise ValueError(f"slippage_bps must be >= 0; got {self.slippage_bps}")
        if self.starting_cash <= 0:
            raise ValueError(f"starting_cash must be > 0; got {self.starting_cash}")
        if self.news_lag_seconds < 0:
            raise ValueError(
                f"news_lag_seconds must be >= 0; got {self.news_lag_seconds}"
            )
        if self.max_position_pct <= 0 or self.max_position_pct > 100:
            raise ValueError(
                f"max_position_pct must be in (0, 100]; got {self.max_position_pct}"
            )
        if self.risk_per_trade_pct <= 0:
            raise ValueError(
                f"risk_per_trade_pct must be > 0; got {self.risk_per_trade_pct}"
            )
        if self.max_candidates_per_tick <= 0:
            raise ValueError(
                "max_candidates_per_tick must be > 0; "
                f"got {self.max_candidates_per_tick}"
            )
        if not self.llm_prompt_version:
            raise ValueError("llm_prompt_version must be non-empty")
        # tickers can be a tuple of symbols OR the literal "watchlist";
        # empty tuple is invalid (would mean replay does nothing).
        if isinstance(self.tickers, tuple) and len(self.tickers) == 0:
            raise ValueError("tickers tuple must not be empty")

    @property
    def replay_db_path(self) -> Path:
        """Filesystem path for ``replay_results.db`` (M2.2 sub-task #16).

        Derived from ``output_dir`` so the SQLite database lives next to
        the markdown comparison report for any given run. Callers pass
        this to ``data.replay.persistence.init_replay_db``. Directory
        creation is the caller's responsibility (``mkdir(parents=True,
        exist_ok=True)`` before ``init_replay_db`` is the documented
        idiom).
        """
        return self.output_dir / "replay_results.db"

    @property
    def tickers_tuple(self) -> tuple[str, ...]:
        """Return tickers as a tuple regardless of original spec.

        For ``tickers="watchlist"``, raises — the caller must resolve
        the watchlist (via ``data/watchlist_builder.py``) before this is
        called. The replay loop will do that once at run start; tests
        can also pass an explicit tuple to skip the resolution step.
        """
        if self.tickers == "watchlist":
            raise ValueError(
                "ReplayConfig.tickers is 'watchlist'; resolve via "
                "watchlist_builder before calling tickers_tuple"
            )
        # Type narrowed: must be tuple[str, ...]
        return tuple(self.tickers)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cache-key helper
# ---------------------------------------------------------------------------


def cache_key(
    *,
    prompt: str,
    prompt_version: str,
    backend: str,
    model_id: str,
    cache_dir: Path,
) -> Path:
    """Build the .replay_cache filesystem path for one LLM call.

    Layout per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § LLM caching::

        <cache_dir>/<backend>/<model_id>/<sha256>.json

    The hash is over ``(prompt_version, prompt)`` so:

    - Bumping ``prompt_version`` invalidates only the affected tier's
      cache files (other tiers' caches survive — they hash a different
      version string).
    - Two tiers with different prompts but the same backend + model_id
      get distinct cache files.

    Returns the ``Path``. Does NOT check existence and does NOT create
    parents — callers own filesystem I/O so tests can stub-write the
    cache without this function side-effecting the disk.

    Raises:
        ValueError: any of ``backend``, ``model_id``, ``prompt_version``
            is empty. (``prompt`` is allowed to be empty for the
            degenerate "no input" cache slot, though that should be
            unreachable in practice.)
    """
    if not backend:
        raise ValueError("backend must be non-empty")
    if not model_id:
        raise ValueError("model_id must be non-empty")
    if not prompt_version:
        raise ValueError("prompt_version must be non-empty")

    payload = f"{prompt_version}|{prompt}".encode("utf-8")
    sha = hashlib.sha256(payload).hexdigest()
    # Make the model_id filesystem-safe (slashes appear in some HF
    # model IDs e.g. "meta-llama/Llama-3.3-70B").
    safe_model = model_id.replace("/", "_").replace("\\", "_")
    return cache_dir / backend / safe_model / f"{sha}.json"
