"""CLI entry point for the M2 replay harness.

Usage examples in ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § CLI. The flag
shape mirrors the spec; defaults match ``ReplayConfig``.

Flow (M2.2 sub-task #19):

1. argparse -> ``ReplayConfig``.
2. ``open_fixture`` for the sentiment SQLite (Rule-26 compliant; not
   trader-prod).
3. ``build_tier_clients`` from the bridged ``llm_config`` dict;
   wrap T1 + T2 in ``CachedLLMClient`` (T3 left raw -- the Tier-3
   pass through ``run_replay`` is a deferred sub-task).
4. ``init_replay_db`` + ``start_run`` on the persistence DB derived
   from ``config.replay_db_path``. ``repo_sha`` from
   ``git rev-parse HEAD`` (best-effort).
5. Construct ``SimulatedPortfolio`` for the LLM side. When
   ``--base-portfolio`` is set, also construct a base portfolio so
   the parallel base-strategy pass + comparison report has data.
6. ``await run_replay(...)`` with everything threaded.
7. ``complete_run`` with a summary JSON aggregating per-day counts
   and cache hit/miss stats from the wrapped clients.
8. ``generate_report`` writes the markdown comparison report (unless
   ``--skip-report``). Path goes to stdout; everything else to stderr.

Exit codes:
  0 -- success
  2 -- FileNotFoundError (missing sentiment fixture, missing DB path
       parent, etc.)
  3 -- ValueError / NotImplementedError (bad config, watchlist literal
       not yet wired, etc.)

Rule 22: ``_setup_logging`` silences ``httpx`` / ``httpcore`` /
``aiohttp`` / ``anthropic`` / ``urllib3`` loggers to WARNING so the
Polygon API key (passed as a URL query param) cannot leak into stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

# Make the script invokable both as ``python -m scripts.replay_with_llm``
# (module mode) and as ``python scripts/replay_with_llm.py`` (direct
# invocation; this is the form the design doc CLI examples use). In the
# direct-invocation case Python does NOT put the repo root on sys.path
# automatically, only the scripts/ dir, so the ``from data.replay...``
# imports below would otherwise raise ModuleNotFoundError.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from data.replay.config import ReplayConfig  # noqa: E402
from data.replay.driver import DayRunResult, run_replay  # noqa: E402
from data.replay.historical_sentiment import open_fixture  # noqa: E402
from data.replay.persistence import (  # noqa: E402
    complete_run, init_replay_db, start_run,
)
from data.replay.t3_budget import T3Budget  # noqa: E402
from data.replay_cache import CachedLLMClient  # noqa: E402
from sim.comparison import generate_report  # noqa: E402
from sim.portfolio import SimulatedPortfolio  # noqa: E402
from strategy.llm.factory import build_tier_clients  # noqa: E402
from strategy.llm.signal_engine import TierClients  # noqa: E402


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
    p.add_argument(
        "--t3-per-call-estimate", type=float, default=0.05,
        help="pre-call USD estimate per Tier 3 invocation (M2.2 #20 "
             "default 0.05 -- conservative vs the design doc's ~$0.003 "
             "after caching). Refined to actual post-call cost in a "
             "future sub-task once the AnthropicClient exposes usage.",
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

    # ---- Base-strategy parallel evaluation (M2.2 sub-task #17 / #19) ----
    p.add_argument(
        "--base-portfolio", action="store_true",
        help="run base rule-based strategy in parallel on a second "
             "SimulatedPortfolio (no pre-filter; every ticker every "
             "tick). Required for the comparison report's divergence "
             "section to have data. Default: off (LLM-only run).",
    )
    p.add_argument(
        "--base-require-walls-for-pullback",
        action=argparse.BooleanOptionalAction, default=False,
        help="when --base-portfolio is set, base-side pullback signals "
             "Hold unless walls align. Default False because walls are "
             "dormant in this fork (Databento canceled).",
    )

    # ---- Behavior toggles ----
    p.add_argument(
        "--echo-only", action="store_true",
        help="parse + print config + exit cleanly without running the "
             "replay loop. Useful for verifying flag parsing.",
    )
    p.add_argument(
        "--skip-report", action="store_true",
        help="run the replay + persistence but skip generate_report at "
             "the end. The report can be generated separately via "
             "sim.comparison.generate_report(db_path, run_id).",
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
        base_require_walls_for_pullback=args.base_require_walls_for_pullback,
    )


def _replay_config_to_llm_config_dict(cfg: ReplayConfig) -> dict[str, Any]:
    """Bridge ReplayConfig's flat fields to the nested llm_config dict shape
    that ``strategy.llm.factory.build_tier_clients`` expects.

    T2 and T3 backends are hardcoded to ``"anthropic"`` -- per design,
    they're always Anthropic (only T1 is configurable across qwen_local
    / anthropic / haiku_stand_in).
    """
    return {
        "enabled": True,
        "t1": {
            "backend": cfg.t1_backend,
            "model_id": cfg.t1_model_id,
        },
        "t2": {
            "enabled": cfg.t2_enabled,
            "backend": "anthropic",
            "model_id": cfg.t2_model_id,
        },
        "t3": {
            "enabled": cfg.t3_enabled,
            "backend": "anthropic",
            "model_id": cfg.t3_model_id,
        },
    }


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
# Logging + provenance helpers
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    """Initialize root logger; silence credential-leak-prone HTTP loggers.

    Rule 22: httpx (and the anthropic SDK on top of it) logs full
    request URLs at INFO by default. Polygon passes API keys as URL
    query params. Forcing httpx / httpcore / aiohttp / anthropic /
    urllib3 to WARNING is the standing invariant; removing it would
    leak credentials into stderr on the next run.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for name in ("httpx", "httpcore", "aiohttp", "anthropic", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _git_head_sha() -> str | None:
    """Return the 7-char short SHA of HEAD, or None on any failure.

    Best-effort: a missing ``git``, a detached working tree, or any
    other subprocess failure yields None (persisted as NULL in
    ``replay_runs.repo_sha``). Never raises.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return None


_PHASE_KEYS: tuple[str, ...] = (
    "data_prep", "tick_loop", "fill_sim_llm", "t3_labeling", "base_pass",
)


def _build_summary_json(
    results: list[DayRunResult],
    wrapped_clients: TierClients,
    *,
    t3_budget: T3Budget | None = None,
    total_wall_clock_ms: float | None = None,
) -> str:
    """Aggregate per-day counts + cache stats into the run-level summary.

    Stored in ``replay_runs.summary_json`` for the comparison report
    to pluck out at section 1. Cache hits/misses come from the
    wrapped CachedLLMClient instances; if a tier wasn't wrapped (T3
    today) its block is omitted. T3 budget stats (call count, skips,
    estimated cost) are included when ``t3_budget`` is supplied.

    Per-phase wall-clock timings (M2.2 sub-task #23): for each phase
    present in any DayRunResult.phase_durations_ms, emit three keys
    -- ``phase_<name>_total_ms``, ``phase_<name>_mean_ms``,
    ``phase_<name>_n_days``. The mean denominator is the number of
    days that actually ran that phase (T3 disabled days don't drag
    the t3_labeling mean down with zeros). Run-level total wall-clock
    is emitted as ``total_wall_clock_ms`` when supplied.
    """
    n_days_total = len(results)
    n_days_skipped = sum(1 for r in results if r.skipped)
    n_llm_decisions = sum(len(r.decisions) for r in results)
    n_base_decisions = sum(len(r.base_decisions) for r in results)
    n_t3_decisions = sum(len(r.t3_decisions) for r in results)
    n_llm_fills = sum(len(r.fills) for r in results)
    n_base_fills = sum(len(r.base_fills) for r in results)
    n_llm_rejections = sum(len(r.rejections) for r in results)
    n_base_rejections = sum(len(r.base_rejections) for r in results)
    n_t2_escalations = sum(r.t2_escalations_used for r in results)

    payload: dict[str, Any] = {
        "n_days_total": n_days_total,
        "n_days_skipped": n_days_skipped,
        "n_llm_decisions": n_llm_decisions,
        "n_base_decisions": n_base_decisions,
        "n_t3_decisions": n_t3_decisions,
        "n_llm_fills": n_llm_fills,
        "n_base_fills": n_base_fills,
        "n_llm_rejections": n_llm_rejections,
        "n_base_rejections": n_base_rejections,
        "n_t2_escalations": n_t2_escalations,
    }

    # Cache stats: only present for tiers wrapped in CachedLLMClient.
    if isinstance(wrapped_clients.t1, CachedLLMClient):
        payload["cache_t1_hits"] = wrapped_clients.t1.hits
        payload["cache_t1_misses"] = wrapped_clients.t1.misses
    if isinstance(wrapped_clients.t2, CachedLLMClient):
        payload["cache_t2_hits"] = wrapped_clients.t2.hits
        payload["cache_t2_misses"] = wrapped_clients.t2.misses

    # T3 budget stats (M2.2 sub-task #20).
    if t3_budget is not None:
        payload["t3_calls"] = t3_budget.n_calls
        payload["t3_skipped_budget"] = t3_budget.n_skipped_budget
        payload["t3_skipped_sample"] = t3_budget.n_skipped_sample
        payload["t3_estimated_cost_usd"] = round(t3_budget.used_dollars, 4)
        payload["t3_cap_dollars"] = t3_budget.cap_dollars

    # Per-phase wall-clock timings (M2.2 sub-task #23).
    for phase in _PHASE_KEYS:
        durations = [
            r.phase_durations_ms[phase]
            for r in results
            if r.phase_durations_ms and phase in r.phase_durations_ms
        ]
        if not durations:
            continue
        total = sum(durations)
        payload[f"phase_{phase}_total_ms"] = round(total, 2)
        payload[f"phase_{phase}_mean_ms"] = round(total / len(durations), 2)
        payload[f"phase_{phase}_n_days"] = len(durations)

    if total_wall_clock_ms is not None:
        payload["total_wall_clock_ms"] = round(total_wall_clock_ms, 2)

    return json.dumps(payload, sort_keys=True)


# ---------------------------------------------------------------------------
# Replay orchestration
# ---------------------------------------------------------------------------


async def _run(cfg: ReplayConfig, args: argparse.Namespace) -> int:
    """Async body of the CLI: opens connections, runs replay, writes report."""
    logger = logging.getLogger("replay_with_llm")
    logger.info(
        "replay starting: %s -> %s, prompt_version=%s, base_portfolio=%s",
        cfg.start_date, cfg.end_date, cfg.llm_prompt_version,
        args.base_portfolio,
    )

    sentiment_conn = open_fixture(cfg.sentiment_fixture_path)
    try:
        # Build + wrap tier clients
        llm_cfg = _replay_config_to_llm_config_dict(cfg)
        raw_clients = build_tier_clients(llm_cfg)
        t1_wrapped = (
            CachedLLMClient(
                raw_clients.t1,
                cache_dir=cfg.cache_dir,
                prompt_version=cfg.llm_prompt_version,
            )
            if raw_clients.t1 is not None else None
        )
        t2_wrapped = (
            CachedLLMClient(
                raw_clients.t2,
                cache_dir=cfg.cache_dir,
                prompt_version=cfg.llm_prompt_version,
            )
            if raw_clients.t2 is not None else None
        )
        # T3 left raw -- run_replay doesn't read clients.t3 today
        # (Tier-3 pass is a deferred sub-task). Wrapping would add
        # dead code.
        wrapped = TierClients(t1=t1_wrapped, t2=t2_wrapped, t3=raw_clients.t3)

        # Open persistence DB
        cfg.replay_db_path.parent.mkdir(parents=True, exist_ok=True)
        persistence_conn = init_replay_db(cfg.replay_db_path)
        try:
            repo_sha = _git_head_sha()
            run_id = start_run(
                persistence_conn, config=cfg, repo_sha=repo_sha,
            )
            logger.info(
                "run_id=%d started (repo_sha=%s, db=%s)",
                run_id, repo_sha, cfg.replay_db_path,
            )

            # Construct portfolios
            llm_portfolio = SimulatedPortfolio(
                starting_cash=cfg.starting_cash, name="llm",
            )
            base_portfolio: SimulatedPortfolio | None = None
            if args.base_portfolio:
                base_portfolio = SimulatedPortfolio(
                    starting_cash=cfg.starting_cash, name="base",
                )

            # T3 budget: constructed when T3 is enabled AND a client
            # was actually wired (factory.build_tier_clients returns
            # None for tier 3 if cfg.t3_enabled is false).
            t3_budget: T3Budget | None = None
            if wrapped.t3 is not None:
                t3_budget = T3Budget(
                    cap_dollars=cfg.t3_max_dollars_per_run,
                    per_call_estimate=args.t3_per_call_estimate,
                )

            # Run replay (wall-clock instrumented for M2.2 sub-task #23).
            _replay_t0 = time.monotonic()
            results = await run_replay(
                config=cfg,
                clients=wrapped,
                sentiment_conn=sentiment_conn,
                portfolio=llm_portfolio,
                base_portfolio=base_portfolio,
                persistence_conn=persistence_conn,
                run_id=run_id,
                t3_budget=t3_budget,
            )
            total_wall_clock_ms = (time.monotonic() - _replay_t0) * 1000.0

            # Complete run with summary
            summary = _build_summary_json(
                results, wrapped,
                t3_budget=t3_budget,
                total_wall_clock_ms=total_wall_clock_ms,
            )
            complete_run(
                persistence_conn, run_id=run_id, summary_json=summary,
            )
            logger.info("run_id=%d completed; summary=%s", run_id, summary)
        finally:
            persistence_conn.close()
    finally:
        sentiment_conn.close()

    # Generate report unless --skip-report
    if args.skip_report:
        print(
            f"Run {run_id} complete; report skipped (--skip-report). "
            f"Generate later via sim.comparison.generate_report.",
            file=sys.stderr,
        )
    else:
        report_path = generate_report(
            db_path=cfg.replay_db_path, run_id=run_id,
        )
        # The path goes to stdout so it's pipeable.
        print(str(report_path))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    cfg = args_to_config(args)

    if args.echo_only:
        print(json.dumps(config_to_printable(cfg), indent=2, sort_keys=True))
        return 0

    _setup_logging()
    try:
        return asyncio.run(_run(cfg, args))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (ValueError, NotImplementedError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
