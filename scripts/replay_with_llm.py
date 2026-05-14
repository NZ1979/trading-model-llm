"""CLI entry point for the M2 replay harness.

Usage examples in ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § CLI. The flag
shape mirrors the spec; defaults match ``ReplayConfig``.

Status: M2.1 scaffolding. This CLI parses arguments into a
``ReplayConfig`` and echoes the config back as JSON. The replay loop
itself — build ``LLMContext`` per tick, call ``signal_engine.evaluate``,
simulate fills via ``sim/``, write the comparison report — lands in
M2.2 / M2.3. The point of M2.1's CLI is to confirm the flag shape and
config wiring so M2.2 can layer the loop on top without re-litigating
arg parsing.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

# Make the script invokable both as ``python -m scripts.replay_with_llm``
# (module mode) and as ``python scripts/replay_with_llm.py`` (direct
# invocation; this is the form the design doc CLI examples use). In the
# direct-invocation case Python does NOT put the repo root on sys.path
# automatically, only the scripts/ dir, so the ``from data.replay...``
# import below would otherwise raise ModuleNotFoundError.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from data.replay.config import ReplayConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> date:
    """Parse a YYYY-MM-DD string into a date. Raises argparse-friendly error."""
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}; expected YYYY-MM-DD"
        ) from exc


def _parse_tickers(s: str) -> tuple[str, ...] | str:
    """Parse a comma-separated ticker list or the literal 'watchlist'."""
    if s == "watchlist":
        return "watchlist"
    tickers = tuple(t.strip().upper() for t in s.split(",") if t.strip())
    if not tickers:
        raise argparse.ArgumentTypeError(
            "tickers must be 'watchlist' or a non-empty comma-separated list"
        )
    return tickers


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse.ArgumentParser for the replay CLI.

    Factored out so tests can construct the parser without invoking
    ``parse_args(sys.argv[1:])``.
    """
    p = argparse.ArgumentParser(
        prog="replay_with_llm",
        description=(
            "M2 replay harness — runs the LLM signal engine against historical "
            "bars/news, simulates fills, and compares the resulting decisions "
            "and P&L against the base rule-based strategy over the same period."
        ),
    )

    # ---- Date range ----
    p.add_argument(
        "--start", type=_parse_date, required=True, metavar="YYYY-MM-DD",
        help="first date in the replay window (inclusive)",
    )
    p.add_argument(
        "--end", type=_parse_date, required=True, metavar="YYYY-MM-DD",
        help="last date in the replay window (inclusive)",
    )

    # ---- Tickers + prompt version ----
    p.add_argument(
        "--tickers", type=_parse_tickers, default="watchlist",
        help="comma-separated symbols or 'watchlist' (default: watchlist)",
    )
    p.add_argument(
        "--prompt-version", required=True,
        help="LLM prompt version string (e.g. 'v0.1-ev-fields'); part of "
             "the per-tier cache key so prompt changes invalidate cleanly",
    )

    # ---- Tier 1 ----
    p.add_argument(
        "--t1-backend", default="qwen_local",
        choices=["qwen_local", "anthropic", "haiku_stand_in"],
    )
    p.add_argument(
        "--t1-model", default="qwen3.6-27b-instruct-q4",
        help="model_id for Tier 1 backend",
    )

    # ---- Tier 2 ----
    p.add_argument(
        "--t2-enabled", action=argparse.BooleanOptionalAction, default=True,
        help="enable/disable Tier 2 Sonnet escalation",
    )
    p.add_argument("--t2-model", default="claude-sonnet-4-5")
    p.add_argument("--t2-max-per-day", type=int, default=25)

    # ---- Tier 3 ----
    p.add_argument(
        "--t3-enabled", action=argparse.BooleanOptionalAction, default=True,
        help="enable/disable Tier 3 Opus gold-standard labeling",
    )
    p.add_argument("--t3-model", default="claude-opus-4-6")
    p.add_argument("--t3-sample-rate", type=float, default=1.0)
    p.add_argument(
        "--t3-max-dollars", type=float, default=500.0,
        help="hard budget cap on Tier 3 cost per run; the harness aborts "
             "the Opus pass if exceeded (no silent throttling, Rule 18)",
    )

    # ---- Simulation ----
    p.add_argument("--starting-cash", type=float, default=100_000.0)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument(
        "--fill-at", choices=["next_bar_open", "current_close"],
        default="next_bar_open",
    )
    p.add_argument("--news-lag-seconds", type=int, default=30)

    # ---- Paths ----
    p.add_argument("--output-dir", type=Path, default=Path("docs/reports"))
    p.add_argument("--cache-dir", type=Path, default=Path(".replay_cache"))
    p.add_argument(
        "--sentiment-fixture", type=Path,
        default=Path("data/replay/fixtures/sentiment.sqlite"),
        help="path to the Rule-26-compliant sentiment fixture (see "
             "data/replay/fixtures/README.md for the export procedure)",
    )

    # ---- Pre-filter ----
    p.add_argument("--pre-filter-min-pm-rvol", type=float, default=2.0)
    p.add_argument("--pre-filter-min-gap-pct", type=float, default=1.0)
    p.add_argument("--pre-filter-news-lookback-hours", type=int, default=2)
    p.add_argument("--max-candidates-per-tick", type=int, default=30)

    # ---- Behavior toggles ----
    p.add_argument(
        "--echo-only", action="store_true",
        help="parse + print config + exit cleanly (no replay loop). "
             "M2.1 default behavior is echo-only regardless of this flag; "
             "M2.2 will make this flag meaningful.",
    )

    return p


# ---------------------------------------------------------------------------
# Config construction
# ---------------------------------------------------------------------------


def args_to_config(args: argparse.Namespace) -> ReplayConfig:
    """Convert parsed argparse Namespace to a ReplayConfig."""
    return ReplayConfig(
        start_date=args.start,
        end_date=args.end,
        tickers=args.tickers,
        llm_prompt_version=args.prompt_version,
        t1_backend=args.t1_backend,
        t1_model_id=args.t1_model,
        t2_enabled=args.t2_enabled,
        t2_model_id=args.t2_model,
        t2_max_per_day=args.t2_max_per_day,
        t3_enabled=args.t3_enabled,
        t3_model_id=args.t3_model,
        t3_sample_rate=args.t3_sample_rate,
        t3_max_dollars_per_run=args.t3_max_dollars,
        starting_cash=args.starting_cash,
        slippage_bps=args.slippage_bps,
        fill_at=args.fill_at,
        news_lag_seconds=args.news_lag_seconds,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        sentiment_fixture_path=args.sentiment_fixture,
        pre_filter_min_pm_rvol=args.pre_filter_min_pm_rvol,
        pre_filter_min_gap_pct=args.pre_filter_min_gap_pct,
        pre_filter_news_lookback_hours=args.pre_filter_news_lookback_hours,
        max_candidates_per_tick=args.max_candidates_per_tick,
    )


# ---------------------------------------------------------------------------
# Echo helpers
# ---------------------------------------------------------------------------


def _jsonify(obj: Any) -> Any:
    """Convert dates and Paths in a nested structure to JSON-friendly types."""
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def config_to_printable(cfg: ReplayConfig) -> dict[str, Any]:
    """Render a ReplayConfig as a JSON-printable dict."""
    return _jsonify(asdict(cfg))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    cfg = args_to_config(args)

    # Always echo the parsed config; this is the M2.1 deliverable.
    print(json.dumps(config_to_printable(cfg), indent=2, sort_keys=True))

    if args.echo_only:
        return 0

    # M2.1 scaffolding: the replay loop is not yet implemented. The
    # CLI returns 0 with a banner so it's usable for config-only
    # verification. M2.2 replaces this branch with the replay-loop
    # invocation.
    print(
        "\n[M2.1] replay loop not yet implemented; exiting after config "
        "echo. Use --echo-only to suppress this banner.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
