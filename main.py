"""Main trading platform orchestrator.

Boots all components in the correct order, wires their callbacks, and runs
the asyncio event loop until shutdown.

Phase 5 scope: signals are GENERATED and LOGGED but not executed. Phase 6
adds risk validation, order placement, and the EOD flatten/journal routines.

Daily timeline (ET):
  08:30  Polygon REST backfill: daily bars (300d) + 20-day PM volume baseline
         per watchlist symbol. Cached in memory for the day.
  09:00  Alpaca SIP bars WebSocket connects. Databento Live (if enabled)
         connects to ES MBP-10. Bar aggregator starts buffering 1m -> 5m bars.
  09:30  PremarketContext computed per symbol from buffered pre-market bars
         + cached baselines. RTH bars start arriving.
  09:35  Signal engine begins evaluating. Each new 5-min bar triggers:
           indicators -> technical signal -> sentiment lookup -> walls lookup
           -> evaluate_trade() -> log decision (Phase 6 will place orders here).
  15:55  Phase 6: flatten all open positions.
  16:30  Phase 6: EOD journal writes the day's decisions/trades summary.

State management:
  - Per-symbol intraday DataFrame held in SymbolState
  - DailyContext + PremarketContext cached per symbol per day
  - Latest sentiment read from SQLite per signal evaluation (cheap)
  - Futures walls read from FuturesWallMonitor.walls() per signal evaluation

Failure tolerance:
  - News pipeline degraded: signals fire but pullback won't because sentiment=None
    (gap-and-go also won't because it requires sentiment >= 3)
  - Databento unavailable: futures_walls=None; pullback won't fire if
    require_walls_for_pullback=true (config), gap-and-go still fires
  - Polygon unavailable: PM baselines missing, RVOL=0, gap-and-go won't fire,
    pullback still fires (only needs daily bars which we cached at startup)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal as signal_module
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from analysis.indicators import (
    DailyContext,
    PremarketContext,
    compute_daily_context,
    compute_intraday_indicators,
    compute_premarket_context,
    generate_signal,
)
from analysis.futures_walls import FuturesWallMonitor
from analysis.regime import classify_regime
from analysis.regime_data import fetch_regime_inputs
from data.alpaca_market_data import AlpacaBarStream
from data.bar_aggregator import BarAggregator
from data.bar_types import FiveMinBar, MinuteBar
from data.news_pipeline import NewsSentimentPipeline, latest_sentiment
from data.polygon_feed import PolygonRESTClient, backfill_premarket_baselines, backfill_daily_bars
from data.polygon_news import PolygonNewsPipeline
from data.finnhub_feed import FinnhubClient, refresh_earnings_calendar, is_earnings_day
from data.pm_rvol_thresholds import (
    load_thresholds as pm_rvol_thresholds_load,
    get_threshold as pm_rvol_thresholds_get,
)
from data.watchlist_builder import (
    read_watchlist_file,
    refresh_dynamic_watchlist,
    default_small_cap_sources,
)
from execution.alpaca_orders import AlpacaOrderClient, OrderResult
from scripts.backfill_shadow_outcomes import run_backfill as run_shadow_backfill
from scripts.build_pm_rvol_thresholds import refresh_pm_rvol_thresholds
from strategy.llm.context_builder import (
    build_account_state,
    build_llm_context,
    build_market_features,
    synthesize_default_analysis,
)
from strategy.llm.escalation import EscalationBudget
from strategy.llm.factory import build_escalation_budget, build_tier_clients
from strategy.llm.policy import (
    BucketStats,
    FinalTradeDecision,
    PolicyConfig,
    PolicyInput,
    bucket_key_for,
    decide as policy_decide,
    hierarchical_lookup,
)
from strategy.llm.signal_engine import TierClients
from strategy.llm.signal_engine import evaluate as llm_evaluate
from strategy.llm.types import LLMContext, LLMDecision
from strategy.risk import (
    compute_atr_stop_pct,
    compute_take_profit_price,
    size_from_risk,
    validate_order,
)
from strategy.signal_engine import evaluate_trade, TradeDecision

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Per-symbol state
# ---------------------------------------------------------------------------

@dataclass
class SymbolState:
    """Mutable per-symbol state. Owned by TradingPlatform.symbols dict."""
    ticker: str
    # Rolling 5-min bars: list of dicts, converted to DataFrame on demand.
    # Capacity ~200 bars covers ~16h of RTH which is plenty for indicators.
    bars: deque[dict] = None
    daily_ctx: DailyContext | None = None
    daily_df: pd.DataFrame | None = None  # cached daily bars for PM context ATR
    premarket_ctx: PremarketContext | None = None
    # Pre-market 5-min bars buffered before 9:30, used to compute PM context
    premarket_bars: list[dict] = None
    # Last decision logged (to dedup repeated identical signals)
    last_decision_action: str | None = None
    last_decision_setup: str | None = None

    def __post_init__(self) -> None:
        if self.bars is None:
            self.bars = deque(maxlen=200)
        if self.premarket_bars is None:
            self.premarket_bars = []

    def to_dataframe(self) -> pd.DataFrame:
        """Combine premarket + RTH 5-min bars into a single DataFrame."""
        rows = list(self.premarket_bars) + list(self.bars)
        if not rows:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
            ).set_index(pd.DatetimeIndex([], tz="UTC"))
        df = pd.DataFrame(rows).set_index("timestamp")
        df.index = pd.to_datetime(df.index, utc=True)
        return df


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Inject secrets from env
    cfg["_secrets"] = {
        "alpaca_key": _require_env("ALPACA_API_KEY"),
        "alpaca_secret": _require_env("ALPACA_API_SECRET"),
        "anthropic_key": _require_env("ANTHROPIC_API_KEY"),
        "polygon_key": _require_env("POLYGON_API_KEY"),
        "finnhub_key": _require_env("FINNHUB_API_KEY"),
        "databento_key": os.environ.get("DATABENTO_API_KEY"),  # optional
    }
    return cfg


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"Missing required env var: {name}")
    return val


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------

def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if `column` exists on `table` in the connected DB."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in cols)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, defn: str,
) -> None:
    """ALTER TABLE ADD COLUMN, but only if the column isn't already present.

    SQLite has no `ALTER TABLE ADD COLUMN IF NOT EXISTS`. We synthesize it
    via PRAGMA table_info introspection so re-running `init_v2_schema` on
    an already-migrated DB is a no-op.
    """
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {defn}")


def init_v2_schema(conn: sqlite3.Connection) -> None:
    """Apply v2 schema migrations idempotently.

    Per docs/LLM_MODEL_V2_REFINEMENTS.md § Supporting schema (Q2 + Q4).
    Adds new columns to `decisions` and `shadow_outcomes`, creates the
    `position_trace` event ledger, and indexes `position_trace.decision_id`.

    Requires v1 tables (`decisions`, `shadow_outcomes`) to already exist;
    Orchestrator._init_db creates those before calling this function.
    """
    # decisions (Q2: holding_day; Q4: four version fields; Q1: bucket_key_used)
    _add_column_if_missing(conn, "decisions", "holding_day", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "decisions", "policy_version", "TEXT NOT NULL DEFAULT '0.0.0'")
    _add_column_if_missing(conn, "decisions", "prompt_version", "TEXT NOT NULL DEFAULT '0.0.0'")
    _add_column_if_missing(conn, "decisions", "schema_version", "TEXT NOT NULL DEFAULT '0.0.0'")
    _add_column_if_missing(conn, "decisions", "code_sha", "TEXT NOT NULL DEFAULT 'unknown'")
    _add_column_if_missing(conn, "decisions", "bucket_key_used", "TEXT")
    # Review #2 additions (2026-05-13): ev_score is uncalibrated for now
    # (advisory.confidence/100 × expected_move_pct); liquidity_rejected is
    # set when the step 7.5 hard-reject gate fires (distinct from the
    # red-flag downgrade in step 7).
    _add_column_if_missing(conn, "decisions", "ev_score", "REAL")
    _add_column_if_missing(conn, "decisions", "liquidity_rejected", "INTEGER NOT NULL DEFAULT 0")

    # shadow_outcomes extension (Q2: multi-day holds up to 3 trading days)
    _add_column_if_missing(conn, "shadow_outcomes", "day_1_eod_pct", "REAL")
    _add_column_if_missing(conn, "shadow_outcomes", "day_2_eod_pct", "REAL")
    _add_column_if_missing(conn, "shadow_outcomes", "day_3_eod_pct", "REAL")

    # position_trace event ledger (Q2)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS position_trace ("
        "trace_id INTEGER PRIMARY KEY, "
        "decision_id INTEGER NOT NULL, "
        "event_time TEXT NOT NULL, "
        "event_type TEXT NOT NULL, "
        "qty_delta INTEGER, "
        "fill_price REAL, "
        "new_stop_price REAL, "
        "intent TEXT, "
        "FOREIGN KEY(decision_id) REFERENCES decisions(id))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_position_trace_decision_id "
        "ON position_trace(decision_id)"
    )


# ---------------------------------------------------------------------------
# Trading platform
# ---------------------------------------------------------------------------

class TradingPlatform:
    """Owns all state and orchestrates the daily lifecycle."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        # Diagnostic throughput counters (added 2026-05-12). Periodic log
        # every 60s shows signal evaluation activity, so a silent failure
        # downstream of the bar handler becomes visible.
        self._eval_count_window = 0
        self._actionable_count_window = 0
        self._eval_window_start_monotonic = 0.0
        # Phase B: prefer dynamic watchlist if recent (<7 days); else fall
        # back to settings.yaml's static list. Dynamic file is rebuilt
        # daily at 08:30 ET by `_run_dynamic_watchlist_refresh`. SIP
        # subscriptions are set at boot from self.watchlist; the new file
        # only takes effect on next restart (no hot re-sub in Phase B).
        dynamic_path = Path("config/watchlist_dynamic.json")
        dynamic_list = read_watchlist_file(dynamic_path, max_age_days=7)
        if dynamic_list:
            self.watchlist: set[str] = {s.upper() for s in dynamic_list}
            logger.info(
                "Watchlist: %d symbols (dynamic, from %s)",
                len(self.watchlist), dynamic_path,
            )
        else:
            self.watchlist = {s.upper() for s in config["watchlist"]}
            logger.info(
                "Watchlist: %d symbols (static fallback, from settings.yaml)",
                len(self.watchlist),
            )
        self.db_path = Path(config["storage"]["db_path"])
        self.symbols: dict[str, SymbolState] = {
            t: SymbolState(ticker=t) for t in self.watchlist
        }
        self.pm_baselines: dict[str, list[int]] = {}

        # Phase C 2026-05-06: per-ticker PM RVOL thresholds. Loaded at boot
        # from config/pm_rvol_thresholds.json (produced by the daily 08:30 ET
        # refresh task). Empty dict on missing file -> all lookups fall back
        # to HARD_FALLBACK_THRESHOLD (5.0), preserving pre-Phase-C behavior.
        self.pm_rvol_thresholds_path = Path("config/pm_rvol_thresholds.json")
        self.pm_rvol_thresholds = pm_rvol_thresholds_load(
            self.pm_rvol_thresholds_path,
        )

        self._tasks: list[asyncio.Task] = []
        self._shutdown = asyncio.Event()

        # Components instantiated in boot()
        self.news_pipeline: NewsSentimentPipeline | None = None
        self.polygon_news_pipeline: PolygonNewsPipeline | None = None
        self.bar_aggregator: BarAggregator | None = None
        self.bar_stream: AlpacaBarStream | None = None
        self.wall_monitor: FuturesWallMonitor | None = None
        self.order_client: AlpacaOrderClient | None = None
        self.finnhub_client: FinnhubClient | None = None
        # Track flatten/journal completion per day so they don't run twice
        self._flatten_done_for: str | None = None
        self._journal_done_for: str | None = None
        self._shadow_follower_done_for: str | None = None

        # Market regime label, computed once per trading day in
        # _run_regime_classification and read by every _run_llm_shadow call.
        # Default "unknown" persists until the first daily routine wakeup
        # succeeds, at which point all shadow rows for the day share the
        # same regime label. Daily-level cadence is intentional — regime
        # changes are slow and intraday recomputation would just add noise.
        self.current_regime_label: str = "unknown"
        self._regime_done_for: str | None = None

        # LLM shadow path. Armed only when llm.enabled is true in config;
        # construction happens in boot() so tests that never boot don't
        # pay the import-cost or env-key requirement of the Anthropic SDK.
        # See _run_llm_shadow + _log_shadow_decision. Hot-reload of the
        # master switch is NOT supported — restart to flip.
        self.llm_clients: TierClients | None = None
        self.llm_budget: EscalationBudget | None = None
        self.policy_config: PolicyConfig | None = None
        # Last calendar day on which the escalation budget was reset
        # (ISO date string). Reset happens once per trading day at
        # premarket-context time alongside the rest of the daily routine.
        self._llm_budget_reset_for: str | None = None

        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    setup TEXT,
                    sentiment INTEGER,
                    confidence INTEGER,
                    walls_status TEXT,
                    reasons TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_decisions_ticker_ts "
                "ON decisions(ticker, ts DESC)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    stop_price REAL,
                    alpaca_order_id TEXT,
                    client_order_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    decision_id INTEGER,
                    FOREIGN KEY(decision_id) REFERENCES decisions(id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_ticker_ts "
                "ON orders(ticker, ts DESC)"
            )
            # Shadow outcomes table: forward returns, MAE/MFE, would-stop/target.
            # Populated by scripts/backfill_shadow_outcomes.py and eventually a
            # live follower. See docs/LLM_MODEL_V2_REFINEMENTS.md sec A.2 and
            # strategy/llm/metrics.py.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS shadow_outcomes ("
                "decision_id INTEGER PRIMARY KEY, "
                "return_5m_pct REAL, return_15m_pct REAL, return_30m_pct REAL, "
                "return_60m_pct REAL, return_eod_pct REAL, "
                "mae_pct REAL, mfe_pct REAL, "
                "mae_at_minutes INTEGER, mfe_at_minutes INTEGER, "
                "stop_would_hit INTEGER, stop_hit_at_minutes INTEGER, "
                "target_would_hit INTEGER, target_hit_at_minutes INTEGER, "
                "first_touch TEXT, "
                "avg_spread_bps REAL, estimated_slippage_bps REAL, "
                "populated_at REAL NOT NULL, horizon_complete TEXT NOT NULL, "
                "FOREIGN KEY(decision_id) REFERENCES decisions(id))"
            )
            # v2 schema migrations (Q2 + Q4 in LLM_MODEL_V2_REFINEMENTS.md).
            # Idempotent: re-running on a fully-migrated DB is a no-op.
            init_v2_schema(conn)

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    async def boot(self) -> None:
        """Start all background tasks and run forever."""
        secrets = self.config["_secrets"]

        # 0. Alpaca order client (needs explicit __aenter__ for the session).
        self.order_client = AlpacaOrderClient(
            api_key=secrets["alpaca_key"],
            api_secret=secrets["alpaca_secret"],
            paper=(self.config["broker"]["mode"] == "paper"),
        )
        await self.order_client.__aenter__()
        # Smoke check: verify auth + show starting equity
        starting_equity = await self.order_client.get_account_equity(force_refresh=True)
        logger.info(
            "Alpaca %s account equity: $%s",
            self.config["broker"]["mode"],
            f"{starting_equity:,.2f}",
        )

        # 0b. Finnhub HTTP client (Wave 1A: earnings calendar veto for gap-and-go).
        # No background task â€” used on-demand by the daily backfill at 8:30 ET
        # to populate the catalysts table.
        self.finnhub_client = FinnhubClient(api_key=secrets["finnhub_key"])
        await self.finnhub_client.__aenter__()
        logger.info("Finnhub client initialized (earnings calendar veto enabled)")

        # 0c. LLM shadow path — armed only when llm.enabled is true.
        # When false (current default), no clients are constructed, the
        # Anthropic SDK on the LLM-tier path is never instantiated, and no
        # shadow rows are written. Failures in this construction step
        # fail loud (Rule 18); we'd rather crash boot than serve with a
        # broken shadow path that silently produces nothing.
        llm_cfg = self.config.get("llm") or {}
        if llm_cfg.get("enabled", False):
            self.llm_clients = build_tier_clients(llm_cfg)
            self.llm_budget = build_escalation_budget(llm_cfg)
            self.policy_config = PolicyConfig()
            logger.info(
                "LLM shadow path ARMED: t1=%s t2=%s budget=%d/day prompt=%s policy=%s",
                type(self.llm_clients.t1).__name__,
                type(self.llm_clients.t2).__name__ if self.llm_clients.t2 else "off",
                self.llm_budget.max_per_day,
                llm_cfg.get("prompt_version", "?"),
                self.policy_config.policy_version,
            )
        else:
            logger.info("LLM shadow path DORMANT (llm.enabled=false)")

        # 1. News pipeline runs 24/7 (sentiment is computed even pre-session).
        self.news_pipeline = NewsSentimentPipeline(
            alpaca_key=secrets["alpaca_key"],
            alpaca_secret=secrets["alpaca_secret"],
            anthropic_key=secrets["anthropic_key"],
            watchlist=self.watchlist,
            db_path=self.db_path,
        )
        self._tasks.append(asyncio.create_task(
            self.news_pipeline.start(), name="NewsPipeline"
        ))

        # 1b. Polygon News pipeline (supplementary; pre-scored sentiment).
        # Polls every 5 min and writes to the same `sentiment` table. See
        # data/polygon_news.py for the rationale (covers Benzinga gaps).
        self.polygon_news_pipeline = PolygonNewsPipeline(
            polygon_key=secrets["polygon_key"],
            watchlist=self.watchlist,
            db_path=self.db_path,
        )
        self._tasks.append(asyncio.create_task(
            self.polygon_news_pipeline.start(), name="PolygonNewsPipeline"
        ))

        # 2. Bar aggregator + Alpaca SIP bars stream.
        self.bar_aggregator = BarAggregator(on_5min_bar=self._on_5min_bar)
        self.bar_stream = AlpacaBarStream(
            api_key=secrets["alpaca_key"],
            api_secret=secrets["alpaca_secret"],
            symbols=self.watchlist,
            on_bar=self._on_minute_bar,
            feed=self.config["broker"]["alpaca_data_feed"],
        )
        self._tasks.append(asyncio.create_task(
            self.bar_stream.run(), name="AlpacaBars"
        ))

        # 3. Futures wall monitor (optional).
        if self.config["futures"]["enabled"] and secrets["databento_key"]:
            self.wall_monitor = FuturesWallMonitor(
                api_key=secrets["databento_key"],
                symbol=self.config["futures"]["symbol"],
                scan_interval_sec=self.config["futures"]["scan_interval_sec"],
                window_size=self.config["futures"]["persistence_window"],
                min_persistence=self.config["futures"]["persistence_min"],
                max_distance_pct=self.config["futures"]["wall_distance_pct"],
                min_size_multiple=self.config["futures"]["wall_min_size_multiple"],
            )
            await self.wall_monitor.start()
            logger.info("Futures wall monitor started (%s)",
                        self.config["futures"]["symbol"])
        else:
            logger.info("Futures wall monitor DISABLED (no Databento key or futures.enabled=false)")

        # 4. Daily routine task: backfill at 8:30 ET, compute PM context at 9:30.
        self._tasks.append(asyncio.create_task(
            self._daily_routine_loop(), name="DailyRoutine"
        ))

        # 5. Task supervisor â€” watches the long-running tasks for silent death.
        # Background tasks created via asyncio.create_task() can complete or
        # raise without anyone noticing. The supervisor logs loudly if a task
        # exits unexpectedly and restarts the AlpacaBars task specifically
        # (since that's the most critical for trading).
        self._tasks.append(asyncio.create_task(
            self._task_supervisor(), name="TaskSupervisor"
        ))

        # 6. Shutdown signal handlers
        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                asyncio.get_event_loop().add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows doesn't support all signals

        # Wait for shutdown
        logger.info("Platform booted, watching %d symbols", len(self.watchlist))
        await self._shutdown.wait()
        await self._shutdown_all()

    async def _task_supervisor(self) -> None:
        """Watch background tasks for silent failure. Restart bars stream if dead.

        Why this exists: in our 2026-04-28 production incident, the AlpacaBars
        task died sometime after 01:51 UTC with no error logged. The platform
        kept running but received zero bars all day, so no signals fired and
        no orders were placed. asyncio.create_task() doesn't surface task
        exits unless someone awaits the task or handles its result.

        This supervisor runs every 30 seconds. For each tracked task, if
        task.done() AND it wasn't supposed to be done (i.e., we haven't been
        asked to shutdown), it logs the failure mode loudly. For the
        AlpacaBars task specifically, it spawns a fresh replacement.
        """
        # Wait briefly for tasks to actually start before watching them
        await asyncio.sleep(10)
        while not self._shutdown.is_set():
            try:
                # Inspect each tracked task for unexpected completion
                for i, task in enumerate(list(self._tasks)):
                    if not task.done():
                        continue
                    name = task.get_name()
                    # The supervisor task itself shows as done when this code runs?
                    # No - get_name() == "TaskSupervisor" means the supervisor is
                    # checking itself. Skip silently.
                    if name == "TaskSupervisor":
                        continue
                    # Get the exception/result without re-raising
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        exc = "cancelled"
                    except asyncio.InvalidStateError:
                        exc = "invalid_state"

                    if exc is None:
                        logger.error(
                            "Task %s exited cleanly (returned None) â€” this is "
                            "unexpected for a long-running task", name,
                        )
                    else:
                        logger.error(
                            "Task %s died with exception: %r", name, exc,
                        )

                    # Restart the bars stream if that's what died
                    if name == "AlpacaBars" and self.bar_stream is not None:
                        logger.warning("Restarting AlpacaBars task")
                        new_task = asyncio.create_task(
                            self.bar_stream.run(), name="AlpacaBars"
                        )
                        self._tasks[i] = new_task
                    elif name == "NewsPipeline" and self.news_pipeline is not None:
                        logger.warning("Restarting NewsPipeline task")
                        new_task = asyncio.create_task(
                            self.news_pipeline.start(), name="NewsPipeline"
                        )
                        self._tasks[i] = new_task
                    elif name == "PolygonNewsPipeline" and self.polygon_news_pipeline is not None:
                        logger.warning("Restarting PolygonNewsPipeline task")
                        new_task = asyncio.create_task(
                            self.polygon_news_pipeline.start(),
                            name="PolygonNewsPipeline",
                        )
                        self._tasks[i] = new_task
                    elif name == "DailyRoutine":
                        logger.warning("Restarting DailyRoutine task")
                        new_task = asyncio.create_task(
                            self._daily_routine_loop(), name="DailyRoutine"
                        )
                        self._tasks[i] = new_task
            except Exception:
                logger.exception("Task supervisor error (continuing)")

            await asyncio.sleep(30)

    async def _shutdown_all(self) -> None:
        logger.info("Shutting down...")
        if self.wall_monitor:
            await self.wall_monitor.stop()
        if self.bar_stream:
            self.bar_stream.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.order_client:
            await self.order_client.__aexit__(None, None, None)
        if self.finnhub_client:
            await self.finnhub_client.__aexit__(None, None, None)

    # ------------------------------------------------------------------
    # Daily routine
    # ------------------------------------------------------------------

    async def _daily_routine_loop(self) -> None:
        """Wakes every 30s; runs scheduled steps as their times arrive."""
        backfill_done_for: str | None = None
        pm_done_for: str | None = None
        earnings_done_for: str | None = None  # Wave 1A: not weekday-gated
        watchlist_done_for: str | None = None  # Phase B: not weekday-gated
        pm_rvol_thresholds_done_for: str | None = None  # Phase C: not weekday-gated

        backfill_time = _parse_time(self.config["schedule"]["baseline_backfill_time"])
        pm_time = _parse_time(self.config["schedule"]["premarket_context_time"])
        flatten_time = _parse_time(self.config["schedule"]["flatten_time"])
        journal_time = _parse_time(self.config["schedule"]["journal_time"])

        while not self._shutdown.is_set():
            now_et = datetime.now(ET)
            today = now_et.date().isoformat()
            now_t = now_et.time()
            is_weekday = now_et.weekday() < 5  # Mon-Fri

            # Backfill (08:30 ET, weekdays only â€” needs market data)
            if is_weekday and backfill_done_for != today and now_t >= backfill_time:
                try:
                    await self._run_baseline_backfill()
                    backfill_done_for = today
                except Exception:
                    logger.exception("Baseline backfill failed; will retry")

            # Market regime classification (08:30 ET after backfill, weekdays
            # only). Reuses the Polygon REST endpoint so it gates on backfill
            # success rather than just the clock — if Polygon is down, the
            # regime classifier would just fail for the same reason. Result
            # is read by _run_llm_shadow throughout the day. Retried each
            # cycle until it succeeds.
            if (
                is_weekday
                and backfill_done_for == today
                and self._regime_done_for != today
            ):
                try:
                    await self._run_regime_classification()
                    self._regime_done_for = today
                except Exception:
                    logger.exception("Regime classification failed; will retry")

            # Finnhub earnings calendar refresh (08:30 ET, every day including
            # weekends). The calendar is forward-looking so weekend refreshes
            # keep catalysts table current ahead of Monday's first signal eval.
            if earnings_done_for != today and now_t >= backfill_time:
                try:
                    await self._run_finnhub_earnings_refresh()
                    earnings_done_for = today
                except Exception:
                    logger.exception("Finnhub earnings refresh failed; will retry")

            # Dynamic watchlist refresh (08:30 ET, every day including weekends).
            # Writes config/watchlist_dynamic.json which the next service boot
            # will use. Phase B; SIP subscriptions only update on restart.
            if watchlist_done_for != today and now_t >= backfill_time:
                try:
                    await self._run_dynamic_watchlist_refresh()
                    watchlist_done_for = today
                except Exception:
                    logger.exception("Dynamic watchlist refresh failed; will retry")

            # Per-ticker PM RVOL thresholds refresh (Phase C, 2026-05-06).
            # Runs after the watchlist refresh so it operates on the current
            # symbol set. Updates self.pm_rvol_thresholds in-place so changes
            # take effect for the same trading day's pre-market context
            # computation at 09:30 ET. Cost: ~5 minutes for 500 tickers.
            if (
                pm_rvol_thresholds_done_for != today
                and watchlist_done_for == today
                and now_t >= backfill_time
            ):
                try:
                    await self._run_pm_rvol_thresholds_refresh()
                    pm_rvol_thresholds_done_for = today
                except Exception:
                    logger.exception(
                        "PM RVOL thresholds refresh failed; will retry"
                    )

            # Pre-market context (09:30 ET)
            if (
                is_weekday
                and backfill_done_for == today
                and pm_done_for != today
                and now_t >= pm_time
            ):
                try:
                    self._compute_premarket_contexts()
                    pm_done_for = today
                except Exception:
                    logger.exception("Premarket context computation failed")

            # LLM escalation budget reset. Runs once per calendar day at
            # premarket-context time so the daily T2 quota is fresh for
            # the day's first signal evaluations. Independent of weekday
            # check (the budget object exists 7 days a week and resetting
            # a 0/N counter on a market-closed Saturday is a cheap no-op).
            if (
                self.llm_budget is not None
                and self._llm_budget_reset_for != today
                and now_t >= pm_time
            ):
                self.llm_budget.reset()
                self._llm_budget_reset_for = today
                logger.info(
                    "LLM escalation budget reset for %s (cap=%d/day)",
                    today, self.llm_budget.max_per_day,
                )

            # Flatten all positions (15:55 ET)
            if (
                is_weekday
                and self._flatten_done_for != today
                and now_t >= flatten_time
                and now_t < journal_time
            ):
                try:
                    await self._run_flatten()
                    self._flatten_done_for = today
                except Exception:
                    logger.exception("Flatten failed; will retry next cycle")

            # EOD journal (16:30 ET)
            if (
                is_weekday
                and self._journal_done_for != today
                and now_t >= journal_time
            ):
                try:
                    await self._run_eod_journal(today)
                    self._journal_done_for = today
                except Exception:
                    logger.exception("EOD journal failed")

            # Shadow outcomes follower. Runs once per trading day, after
            # the EOD journal step so all 1-min bars for the session are
            # settled at Polygon. Walks Buy/Sell decisions from the last
            # 7 days that lack shadow_outcomes rows and attributes
            # realized returns via the same loop scripts/backfill_*.py
            # uses for manual runs. Idempotent across re-runs.
            if (
                is_weekday
                and self._journal_done_for == today
                and self._shadow_follower_done_for != today
                and now_t >= journal_time
            ):
                try:
                    await self._run_shadow_outcomes_follower()
                    self._shadow_follower_done_for = today
                except Exception:
                    logger.exception(
                        "Shadow outcomes follower wrapper failed; will retry"
                    )

            await asyncio.sleep(30)

    async def _run_baseline_backfill(self) -> None:
        """Fetch daily bars (300d) and 20-day PM volume baselines.

        Both phases run concurrently with Semaphore caps. Pre-fix this method
        looped sequentially fetching MINUTE bars over 450 days per ticker and
        resampled to daily â€” silently failed on most of 503 tickers (only
        loaded ~26). The fix uses Polygon's daily-bar endpoint and concurrency.
        """
        secrets = self.config["_secrets"]
        polygon_key = secrets["polygon_key"]
        baseline_days = self.config["market_data"]["polygon_baseline_days"]
        daily_lookback = self.config["market_data"]["polygon_daily_lookback"]

        logger.info("Starting baseline backfill for %d symbols", len(self.watchlist))

        # PM baselines (concurrent, ~30s)
        self.pm_baselines = await backfill_premarket_baselines(
            api_key=polygon_key,
            symbols=self.watchlist,
            days=baseline_days,
        )

        # Daily bars (concurrent, ~10-30s for 503 tickers)
        daily_dfs = await backfill_daily_bars(
            api_key=polygon_key,
            symbols=self.watchlist,
            lookback_days=daily_lookback,
        )

        # Build DailyContext per symbol from the fetched DataFrames.
        # Bug G fix 2026-05-05: ALWAYS store daily_df, even when
        # compute_daily_context returns None (tickers with <200 daily bars,
        # e.g. recent spinoffs like SNDK). The DataFrame is still useful for
        # premarket ATR computation, and gap-and-go doesn't need daily_ctx
        # â€” only the pullback path does.
        n_daily_short_history = 0
        for ticker, daily in daily_dfs.items():
            try:
                ctx = compute_daily_context(daily, ticker)
                if ctx is not None:
                    self.symbols[ticker].daily_ctx = ctx
                else:
                    n_daily_short_history += 1
                # Store daily_df regardless so premarket_ctx (gap-and-go
                # only) can still build for short-history tickers.
                self.symbols[ticker].daily_df = daily
            except Exception:
                logger.exception("Daily context build failed for %s", ticker)

        n_daily = sum(1 for s in self.symbols.values() if s.daily_ctx is not None)
        n_pm = len(self.pm_baselines)
        logger.info(
            "Backfill complete: %d daily contexts, %d short-history "
            "(gap-and-go only), %d PM baselines",
            n_daily, n_daily_short_history, n_pm,
        )

    async def _run_shadow_outcomes_follower(self) -> None:
        """Attribute realized returns to every Buy/Sell decision logged today.

        Runs once per trading day after the journal step (16:30 ET), at
        which point all 1-minute bars for the session are settled at
        Polygon. Delegates to the refactored backfill loop in
        scripts/backfill_shadow_outcomes.py, which:
          - Joins decisions to orders (real + synthetic 'shadow' rows
            written by _log_synthetic_order)
          - Fetches Polygon 1-min bars per (ticker, date)
          - Calls strategy.llm.metrics.compute_outcome per row
          - Inserts to shadow_outcomes

        Idempotent: rows already in shadow_outcomes are skipped by the
        backfill's LEFT JOIN. Re-running the follower mid-day is safe.
        Failures are logged and swallowed so a Polygon outage on a
        post-market day doesn't crash the orchestrator.
        """
        polygon_key = self.config["_secrets"]["polygon_key"]
        risk_cfg = self.config.get("risk", {}) or {}
        tp_atr = float(risk_cfg.get("take_profit_atr_multiple", 2.0))
        stop_atr = float(risk_cfg.get("stop_atr_multiplier", 1.5))

        # Only backfill decisions from the last 7 days. Older ones either
        # already have outcomes or are stale enough that a Polygon refetch
        # cost isn't worth the marginal data. The 'since' filter is
        # idempotent with the LEFT JOIN inside _read_decisions_to_backfill.
        since_dt = datetime.utcnow().date() - timedelta(days=7)
        try:
            summary = await run_shadow_backfill(
                db_path=self.db_path,
                polygon_key=polygon_key,
                tp_atr=tp_atr,
                stop_atr=stop_atr,
                since=since_dt.isoformat(),
                limit=None,
                dry_run=False,
                log_via_print=False,
            )
            logger.info(
                "Shadow outcomes follower: candidates=%d success=%d "
                "skipped=%d failed=%d",
                summary["candidates"], summary["success"],
                summary["skipped"], summary["failed"],
            )
        except Exception:
            logger.exception("Shadow outcomes follower failed; will retry tomorrow")

    async def _run_regime_classification(self) -> None:
        """Compute today's market regime label and cache on self.

        Pulls SPY + VIX daily bars via Polygon, derives RegimeInputs
        (SPY 20-day return, VIX level + 60d median, breadth proxy from
        SPY vs its 50-day SMA), and dispatches through the five-bucket
        classifier in analysis/regime.py. Result is stored on
        self.current_regime_label and read by every _run_llm_shadow
        call until the next daily routine wakeup overwrites it.

        Per Rule 18, failure leaves the regime at its previous value
        rather than degrading to "unknown" — yesterday's regime is a
        better fallback than the empty default because daily regimes
        change slowly. First-day-after-deploy failures DO surface as
        "unknown" since there's no prior value to keep; that's the
        intended fail-loud signal.
        """
        polygon_key = self.config["_secrets"]["polygon_key"]
        async with PolygonRESTClient(api_key=polygon_key) as client:
            inputs = await fetch_regime_inputs(client)
            label = classify_regime(inputs)
        self.current_regime_label = label
        vix_ratio_str = (
            f"{inputs.vix_ratio:.2f}" if inputs.vix_ratio is not None else "n/a"
        )
        logger.info(
            "Market regime: %s (spy_20d=%.2f%% vix_ratio=%s breadth=%.2f%%)",
            label,
            inputs.spy_return_20d * 100,
            vix_ratio_str,
            inputs.breadth_proxy * 100,
        )

    async def _run_finnhub_earnings_refresh(self) -> None:
        """Refresh the Finnhub earnings calendar (14-day forward window).

        Independent of the weekday-gated `_run_baseline_backfill` so this
        runs on weekends too. The calendar is forward-looking, so weekend
        refreshes keep the catalysts table current ahead of Monday's first
        signal evaluation. One API call (no symbol filter), filtered
        in-memory to watchlist. Idempotent on replay (INSERT OR IGNORE).
        """
        if self.finnhub_client is None:
            return
        await refresh_earnings_calendar(
            self.finnhub_client,
            self.watchlist,
            self.db_path,
            days_forward=14,
        )

    async def _run_dynamic_watchlist_refresh(self) -> None:
        """Refresh the dynamic watchlist (Phase B + fork override 2026-05-13 restored).

        Gap-and-go fork override: passes ``sources=default_small_cap_sources()``
        so the universe is the Russell 2000 (via iShares IWM holdings) instead
        of the base's S&P 500 + NASDAQ + DJIA. This is the fork's primary
        deviation from base; the upstream merge of 2026-05-12 auto-resolved
        this line back to base and was restored 2026-05-13 after R2K trading
        regressed to large-caps.

        Builds top-500-by-30D-ADV from Russell 2000 constituents and writes
        `config/watchlist_dynamic.json`. Runs daily at 08:30 ET including
        weekends â€” no weekday gating.

        Does NOT update self.watchlist mid-run. The new list is picked up
        on next service restart, when __init__ reads watchlist_dynamic.json.
        """
        polygon_key = self.config["_secrets"]["polygon_key"]
        output_path = Path("config/watchlist_dynamic.json")
        await refresh_dynamic_watchlist(
            polygon_key,
            output_path,
            top_n=500,
            sources=default_small_cap_sources(),
        )

    async def _run_pm_rvol_thresholds_refresh(self) -> None:
        """Refresh per-ticker PM RVOL thresholds (Phase C, 2026-05-06).

        For every ticker in the current watchlist, fetch the last 180 days
        of pre-market 1-minute bars, compute that ticker's PM RVOL
        distribution, and set the threshold at the 85th percentile (clipped
        to [2.0, 10.0]).

        Schedule: daily at 08:30 ET, after _run_dynamic_watchlist_refresh
        produces the latest watchlist file. Output:
        config/pm_rvol_thresholds.json. Updates self.pm_rvol_thresholds
        in-place so the same trading day's signal evaluations use the new
        per-ticker values without requiring a service restart.
        """
        polygon_key = self.config["_secrets"]["polygon_key"]
        output_path = self.pm_rvol_thresholds_path
        await refresh_pm_rvol_thresholds(
            api_key=polygon_key,
            watchlist=sorted(self.watchlist),
            output_path=output_path,
        )
        self.pm_rvol_thresholds = pm_rvol_thresholds_load(output_path)

    def _compute_premarket_contexts(self) -> None:
        """At 9:30 ET, compute PM context per symbol from buffered PM bars.

        Bug G fix 2026-05-05: previously gated on daily_ctx is None, which
        excluded tickers with <200 daily bars (e.g. SNDK after the WD
        spinoff). Those tickers had daily_df available but were silently
        skipped, making gap-and-go unreachable for them. Now we gate on
        daily_df instead â€” the only thing premarket_ctx actually needs.
        """
        for ticker, state in self.symbols.items():
            df = state.to_dataframe()
            if df.empty:
                continue
            # daily_df is needed by compute_premarket_context for ATR.
            # daily_ctx is not needed here (only the gap-and-go path uses it
            # internally, and only as a confidence boost, not a hard gate).
            daily_df = getattr(state, "daily_df", None)
            if daily_df is None:
                continue

            # Phase C 2026-05-06: per-ticker PM RVOL threshold from
            # historical distribution. Falls back to the global default
            # (5.0) when no entry exists for this ticker.
            ticker_threshold = pm_rvol_thresholds_get(
                self.pm_rvol_thresholds, ticker,
            )
            ctx = compute_premarket_context(
                daily_df=daily_df,
                today_full_session_df=df,
                ticker=ticker,
                historical_pm_volumes=self.pm_baselines.get(ticker),
                rvol_threshold=ticker_threshold,
            )
            state.premarket_ctx = ctx

        n_pm_ctx = sum(1 for s in self.symbols.values() if s.premarket_ctx is not None)
        logger.info("Pre-market contexts computed for %d symbols", n_pm_ctx)

    # ------------------------------------------------------------------
    # Bar handling
    # ------------------------------------------------------------------

    async def _on_minute_bar(self, bar: MinuteBar) -> None:
        """Forward to aggregator. Aggregator emits 5-min bars to _on_5min_bar."""
        await self.bar_aggregator.on_minute_bar(bar)

    async def _on_5min_bar(self, bar: FiveMinBar) -> None:
        """A new 5-min bar closed for one symbol. Update state and try signal."""
        ticker = bar.symbol
        if ticker not in self.symbols:
            return  # not in watchlist (subscription filter doesn't catch all)
        state = self.symbols[ticker]
        row = {
            "timestamp": bar.timestamp,
            "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume,
        }
        # Determine if this bar is pre-market or RTH
        et = bar.timestamp.astimezone(ET)
        is_rth = (et.hour > 9 or (et.hour == 9 and et.minute >= 30)) and et.hour < 16
        if is_rth:
            state.bars.append(row)
        else:
            state.premarket_bars.append(row)
            return  # don't evaluate signals on pre-market bars

        # Evaluate signals only after 9:35 ET (signal_start_time)
        signal_start = _parse_time(self.config["schedule"]["signal_start_time"])
        if et.time() < signal_start:
            return

        await self._evaluate_and_execute(ticker, state)

    async def _evaluate_and_execute(self, ticker: str, state: SymbolState) -> None:
        """Run the full pipeline: signal -> decision -> risk -> order.

        Decision is always logged. An order is only placed when:
          - decision.action is Buy or Sell (not Hold)
          - we don't already have a same-direction position open (no pyramiding
            in Phase 6; one entry per ticker per day until flattened)
          - risk validator approves the sized order
        """
        # Throughput heartbeat: every 60s log evaluate_and_execute calls
        # and how many returned non-Hold. A silent market is N>0 evals
        # with 0 actionable; a broken pipeline is N=0 evals.
        now = time.monotonic()
        if self._eval_window_start_monotonic == 0.0:
            self._eval_window_start_monotonic = now
        elif now - self._eval_window_start_monotonic >= 60.0:
            logger.info(
                "SignalEngine throughput: last %.1fs -> %d evals, %d actionable",
                now - self._eval_window_start_monotonic,
                self._eval_count_window,
                self._actionable_count_window,
            )
            self._eval_count_window = 0
            self._actionable_count_window = 0
            self._eval_window_start_monotonic = now
        self._eval_count_window += 1
        df = state.to_dataframe()
        if df.empty:
            return  # nothing to evaluate yet

        # 1. Indicators (returns RTH-filtered df with NaN columns when RTH<50)
        df_ind = compute_intraday_indicators(df, rth_only=True)
        if df_ind.empty:
            return  # no RTH bars yet

        # 2. Technical signal. The pullback path checks its own 50-bar warmup
        # internally; gap-and-go (in 9:35-10:00 ET) does NOT need warmup, so
        # we no longer short-circuit on RTH<50 here. Fix for 2026-04-29
        # audit finding that gap-and-go was structurally unreachable.
        tech = generate_signal(df_ind, state.daily_ctx, state.premarket_ctx)

        # Wave 1A: gap-and-go earnings-day veto. Trading gap-and-go on a stock
        # with earnings before/during/after the bar is binary-bet exposure
        # the strategy isn't designed for. Pullback path is unaffected
        # (mean-reversion can be valid signal even on earnings days).
        # NOTE: TechnicalSignal's field is `.signal` (Buy/Sell/Hold), not
        # `.action` â€” TradeDecision uses `.action` but TechnicalSignal does
        # not. Bug fix 2026-05-04 after AttributeError storm in production.
        if tech.signal != "Hold" and tech.setup == "gap_and_go":
            today_et = datetime.now(ET).date().isoformat()
            if is_earnings_day(self.db_path, ticker, today_et):
                if state.last_decision_action != "EarningsVeto":
                    logger.info(
                        "%s [gap_and_go] VETO: earnings on %s; skipping",
                        ticker, today_et,
                    )
                    state.last_decision_action = "EarningsVeto"
                    state.last_decision_setup = "gap_and_go"
                return

        # 3. Latest sentiment
        max_age = self.config["signals"]["sentiment_max_age_sec"]
        sentiment = latest_sentiment(self.db_path, ticker, max_age_sec=max_age)

        # 4. Futures walls
        walls = self.wall_monitor.walls() if self.wall_monitor else None

        # 5. Combined decision
        decision = evaluate_trade(
            ticker=ticker,
            sentiment_score=sentiment,
            technical_signal=tech,
            futures_walls=walls,
            require_walls_for_pullback=self.config["signals"]["require_walls_for_pullback"],
        )

        # 6. Log decision (dedup repeated identical Holds)
        is_actionable = decision.action != "Hold"
        if is_actionable:
            self._actionable_count_window += 1
        is_changed = (
            decision.action != state.last_decision_action
            or decision.setup != state.last_decision_setup
        )
        decision_id: int | None = None
        if is_actionable or is_changed:
            decision_id = self._log_decision(decision)
            state.last_decision_action = decision.action
            state.last_decision_setup = decision.setup

        # 6b. LLM shadow path. Runs alongside the rule-based decision but
        # NEVER routes to the broker — it logs an LLM+policy decision to
        # the same `decisions` table with setup prefix "llm_shadow/" so
        # the shadow_outcomes follower can later attribute forward returns.
        # Per Rule 18, failures here are logged and swallowed so the
        # live rule-based path is never blocked. No-op when llm.enabled
        # is false (self.llm_clients is None).
        if self.llm_clients is not None:
            await self._run_llm_shadow(ticker, state, df_ind, sentiment)

        # 7. Place order if actionable
        if is_actionable:
            latest_price = float(df_ind["close"].iloc[-1])
            await self._place_order(decision, latest_price, decision_id)

    async def _place_order(
        self,
        decision: TradeDecision,
        latest_price: float,
        decision_id: int | None,
    ) -> None:
        """Convert a Buy/Sell decision into a sized, risk-validated bracket order."""
        assert self.order_client is not None

        # Don't pyramid: skip if we already hold a same-direction position.
        positions = await self.order_client.get_open_positions()
        existing = next((p for p in positions if p.ticker == decision.ticker), None)
        if existing is not None:
            same_dir = (
                (decision.action == "Buy" and existing.quantity > 0)
                or (decision.action == "Sell" and existing.quantity < 0)
            )
            if same_dir:
                logger.debug(
                    "%s: skipping %s; same-direction position open (qty=%d)",
                    decision.ticker, decision.action, existing.quantity,
                )
                return
            # Opposite-side signal with existing position: prevent flip.
            # Phase 6 policy is no flips in one order; close-then-reverse only.
            logger.info(
                "%s: %s signal but %d shares opposite direction held; skipping (no flips)",
                decision.ticker, decision.action, existing.quantity,
            )
            return

        # Risk-based sizing: target 0.5% portfolio risk per trade.
        equity = await self.order_client.get_account_equity()
        if equity <= 0:
            logger.warning("Skipping %s: equity unavailable", decision.ticker)
            return

        # Bug I 2026-05-06 (restored 2026-05-13 after Layer 1 backport regression):
        # _place_order needs the per-symbol state to read daily_ctx for ATR sizing,
        # but the function only receives decision/price/id as parameters. Look it
        # up here. If the symbol isn't in self.symbols (shouldn't happen since the
        # decision came from a symbol we track), default daily_atr to 0.0 via the
        # safety check below.
        state = self.symbols.get(decision.ticker)

        # Bug H 2026-05-06: stop distance is now ATR-aware. Falls back to
        # the configured fixed pct when daily_atr_14 is missing (e.g. for
        # tickers with insufficient daily history).
        fallback_stop_pct = self.config["risk"]["stop_loss_pct"]
        risk_cfg = self.config["risk"]
        daily_atr = (
            state.daily_ctx.daily_atr_14
            if state is not None and state.daily_ctx is not None else 0.0
        )
        stop_loss_pct = compute_atr_stop_pct(
            entry_price=latest_price,
            daily_atr=daily_atr,
            atr_multiplier=risk_cfg.get("stop_atr_multiplier", 1.5),
            min_pct=risk_cfg.get("stop_atr_min_pct", 1.0),
            max_pct=risk_cfg.get("stop_atr_max_pct", 5.0),
            fallback_pct=fallback_stop_pct,
        )
        risk_per_trade = risk_cfg.get("risk_per_trade_pct", 0.5)
        logger.info(
            "%s: ATR stop sizing â€” atr=%.3f stop_pct=%.2f%% (fallback=%.2f%%)",
            decision.ticker, daily_atr, stop_loss_pct, fallback_stop_pct,
        )
        qty = size_from_risk(
            account_equity=equity,
            entry_price=latest_price,
            stop_loss_pct=stop_loss_pct,
            risk_per_trade_pct=risk_per_trade,
        )
        if qty <= 0:
            logger.warning("Skipping %s: computed qty=0", decision.ticker)
            return

        # Risk validation
        side = "buy" if decision.action == "Buy" else "sell"
        check = validate_order(
            ticker=decision.ticker,
            side=side,
            requested_quantity=qty,
            current_price=latest_price,
            account_equity=equity,
            open_positions=positions,
            max_position_pct=self.config["risk"]["max_position_pct"],
            max_total_exposure_pct=self.config["risk"]["max_total_exposure_pct"],
            stop_loss_pct=stop_loss_pct,
            scale_down_if_oversized=True,
        )
        if not check.approved:
            logger.warning(
                "%s: risk rejected order: %s",
                decision.ticker, check.reason,
            )
            self._log_order_rejection(decision, qty, latest_price, check.reason, decision_id)
            return

        # Compute take-profit price (Layer 1 of v2 profit-protection).
        # When risk.take_profit_enabled is True, attach a TP leg to the bracket
        # order at limit_price plus or minus tp_atr_multiple times daily ATR(14).
        # Broker holds the TP server-side; fast moves to target fill instantly
        # without waiting for the next 5-min eval. When disabled (default),
        # behavior is identical to v1 (OTO with stop only).
        # See docs/LLM_MODEL_V2_REFINEMENTS.md sec B.1 Layer 1 and
        # strategy/risk.py::compute_take_profit_price for the pure helper.
        risk_cfg = self.config["risk"]
        tp_enabled = bool(risk_cfg.get("take_profit_enabled", False))
        tp_atr_multiple = float(risk_cfg.get("take_profit_atr_multiple", 2.0))
        tp_price = compute_take_profit_price(
            side=side,
            entry_price=round(latest_price, 2),
            daily_atr=daily_atr,
            tp_atr_multiple=tp_atr_multiple,
            enabled=tp_enabled,
        )
        # Rule 18 fail-loud: if the operator enabled TP but ATR isn't
        # warmed up yet, surface that gap in the live logs rather than
        # let it silently degrade.
        if tp_enabled and tp_price is None and daily_atr <= 0:
            logger.warning(
                "%s: take_profit_enabled but daily_atr=%.4f; submitting without TP leg",
                decision.ticker, daily_atr,
            )

        # Submit bracket (TP leg attached if tp_price is not None)
        result = await self.order_client.submit_bracket_order(
            ticker=decision.ticker,
            side=side,
            qty=check.quantity,
            limit_price=round(latest_price, 2),
            stop_price=check.stop_price,
            take_profit_limit_price=tp_price,
        )
        if result.success:
            tp_log = "" if tp_price is None else " tp $%.2f" % tp_price
            logger.info(
                "ORDER %s %s %d @ ~$%.2f (stop $%.2f%s, %d%% pos / %d%% total)",
                result.side.upper(), result.ticker, result.submitted_qty,
                latest_price, result.stop_price, tp_log,
                int(check.position_pct), int(check.total_exposure_pct),
            )
        else:
            logger.error("ORDER REJECTED %s: %s", result.ticker, result.error)
        self._log_order_result(result, latest_price, decision_id)

    def _get_bucket_history(self, key: tuple) -> BucketStats | None:
        """Bucket-history reader for hierarchical_lookup.

        Returns ``None`` for every key until the bucket aggregation
        pipeline lands. That pipeline depends on:
          1. shadow_outcomes table populated with realized returns.
          2. A nightly aggregation job that groups by the 5-tuple
             bucket key and computes (sample_count, expected_r,
             expected_r_lower_ci, win_rate, avg_win_r, avg_loss_r).
          3. This method swapped for a SQLite read against that
             aggregation table.

        Until then the lookup walks all collapse levels, gets None at
        each (which hierarchical_lookup converts to BucketStats.empty),
        and returns the bottom of the collapse ladder. The recorded
        ``bucket_key`` in FinalTradeDecision is still the real
        most-granular key, so audit trails are correct from day one.
        """
        return None

    async def _run_llm_shadow(
        self,
        ticker: str,
        state: SymbolState,
        df_ind: pd.DataFrame,
        sentiment: int | None,
    ) -> None:
        """LLM + policy shadow evaluation. Logs only; never routes to broker.

        Pipeline:
          1. build_llm_context()      — orchestrator state → LLMContext
          2. build_market_features()  — indicator state → MarketFeatures
          3. build_account_state()    — equity + positions → AccountState
          4. synthesize_default_analysis() — bridge LLMAnalysis until the
             LLMOutput refactor lands (see context_builder docstring).
          5. signal_engine.evaluate() — T1 always, T2 conditional per
             escalation rule + daily budget.
          6. hierarchical_lookup()    — bucket-history walk; stubbed to
             None-returning reader until the aggregation table exists.
          7. policy.decide()          — deterministic firewall; produces
             FinalTradeDecision with bucket_key + ev_score +
             liquidity_rejected + version pins.
          8. _log_shadow_decision()   — insert into `decisions` with
             setup="llm_shadow/<tier_provenance>".

        Never raises (Rule 18). Every failure mode is caught and logged
        so the live rule-based path is unaffected. Returns immediately
        when the master switch is off (caller already checks; this is
        a defensive secondary guard).

        UNVERIFIED: bucket history lookup is deferred to a separate
        session per docs/LLM_MODEL_OVERVIEW.md item 8. Until then,
        ``BucketStats.empty(())`` is passed, which the policy maps to
        ``qty_tier="tiny"`` — intentionally conservative.

        ``has_active_stop`` is set from a real /v2/orders query
        (30s-cached on AlpacaOrderClient.get_active_stop_orders).
        Catches bracket stop_loss legs at the top level once their
        parent fills, plus manually-placed stop/stop_limit/
        trailing_stop orders.
        """
        if self.llm_clients is None or self.llm_budget is None or self.policy_config is None:
            return  # defensive — caller should have short-circuited

        try:
            prompt_version = self.config["llm"].get("prompt_version", "unknown")
            now_et = datetime.now(ET)

            # Fetch broker state first so both LLMContext and MarketFeatures
            # see the same position snapshot. equity + active-stop set are
            # 30s-cached in alpaca_orders so multiple shadow-path symbols
            # on the same bar share Alpaca round-trips; get_open_positions
            # is uncached but returns [] on API error (safe failure mode).
            assert self.order_client is not None
            equity = await self.order_client.get_account_equity()
            positions = await self.order_client.get_open_positions()
            active_stop_tickers = await self.order_client.get_active_stop_orders()

            # Translate this ticker's broker-side position (if any) into the
            # {qty, avg_price, unrealized_pl_pct, has_active_stop} dict shape
            # context_builder expects. None when flat. Sign convention:
            # unrealized_pl_pct is positive when the position is in the money
            # regardless of long/short direction.
            position_dict: dict[str, Any] | None = None
            this_position = next(
                (p for p in positions if p.ticker == ticker), None,
            )
            if this_position is not None and this_position.quantity != 0:
                qty = this_position.quantity  # signed
                avg = this_position.avg_price
                cur = this_position.current_price
                raw_pl_pct = (cur / avg - 1.0) * 100.0 if avg > 0 else 0.0
                pl_pct = raw_pl_pct if qty > 0 else -raw_pl_pct
                position_dict = {
                    "qty": qty,
                    "avg_price": avg,
                    "unrealized_pl_pct": pl_pct,
                    "has_active_stop": ticker in active_stop_tickers,
                }

            # 1. LLMContext
            ctx = build_llm_context(
                ticker=ticker,
                timestamp_et=now_et.isoformat(),
                prompt_version=prompt_version,
                df_ind=df_ind,
                daily_ctx=state.daily_ctx,
                premarket_ctx=state.premarket_ctx,
                sentiment=float(sentiment) if sentiment is not None else None,
                position=position_dict,
                market_regime_label=self.current_regime_label,
            )

            # 2. MarketFeatures
            daily_atr = (
                state.daily_ctx.daily_atr_14
                if state.daily_ctx is not None else 0.0
            )
            features = build_market_features(
                df_ind=df_ind,
                daily_atr=daily_atr,
                position=position_dict,
            )

            # 3. AccountState. Built from the same equity/positions snapshot
            # so all three (ctx, features, account) reference the same point
            # in time.
            account = build_account_state(
                equity=equity,
                open_positions=positions,
            )

            # 4. Bridge LLMAnalysis
            analysis = synthesize_default_analysis()

            # 5. Tier orchestration (T1 always, T2 conditional)
            t2_cfg = self.config["llm"].get("t2", {}) or {}
            advisory = await llm_evaluate(
                ctx,
                self.llm_clients,
                self.llm_budget,
                confidence_floor=int(t2_cfg.get("confidence_floor", 50)),
                confidence_ceiling=int(t2_cfg.get("confidence_ceiling", 75)),
                pm_rvol_min=float(t2_cfg.get("pm_rvol_min", 3.0)),
            )

            # 6. Bucket lookup. The hierarchical_lookup helper walks
            # progressively-collapsed bucket keys (time_of_day → cap_size)
            # until a bucket with enough samples is found. The reader
            # below returns None for every key until the bucket
            # aggregation table is populated from shadow_outcomes — at
            # that point the policy's sample-count tier sizing starts
            # working off real data without any further main.py change.
            base_key = bucket_key_for(
                market_regime_label=ctx.market_regime_label,
                market_cap_bucket=ctx.market_cap_bucket,
                catalyst_quality_value=analysis.catalyst_quality.value,
                minutes_since_open=ctx.minutes_since_open,
                action=advisory.action,
            )
            bucket_stats = hierarchical_lookup(
                base_key=base_key,
                get_bucket=self._get_bucket_history,
                sample_min=self.policy_config.sample_min_for_normal_tier,
            )

            # 7. Policy decision.
            policy_input = PolicyInput(
                ctx=ctx,
                analysis=analysis,
                advisory=advisory,
                features=features,
                account=account,
                bucket_history=bucket_stats,
                health_state="healthy",
            )
            final = policy_decide(policy_input, self.policy_config)

            # 8. Log shadow row. Distinguishing setup prefix means
            # rule-based analytics queries can WHERE setup NOT LIKE
            # 'llm_shadow/%' to exclude these.
            decision_id = self._log_shadow_decision(ticker, ctx, advisory, final)

            # 9. Synthetic order row for Buy/Sell decisions. Shadow
            # decisions never reach the broker, but the shadow_outcomes
            # backfill (scripts/backfill_shadow_outcomes.py) joins
            # decisions to orders to recover entry + stop prices. This
            # synthetic row preserves that join shape with
            # status='shadow', so real-trade analytics can filter on
            # status='submitted' and shadow analytics can filter on
            # status='shadow'.
            #
            # UNVERIFIED: the backfill recomputes target_price from
            # the config's tp_atr/stop_atr ratio, not the LLM
            # advisory's specific multiples. When the LLM picks
            # ratios that differ from config, target-hit computation
            # drifts slightly. Acceptable first-land approximation.
            if final.action != "Hold" and decision_id is not None:
                daily_atr = (
                    state.daily_ctx.daily_atr_14
                    if state.daily_ctx is not None else 0.0
                )
                if daily_atr > 0 and ctx.current_close > 0:
                    synthetic_limit = ctx.current_close
                    stop_distance = (
                        final.stop_loss_atr_multiple * daily_atr
                    )
                    if final.action == "Buy":
                        synthetic_stop = synthetic_limit - stop_distance
                    else:  # Sell
                        synthetic_stop = synthetic_limit + stop_distance
                    self._log_synthetic_order(
                        decision_id=decision_id,
                        ticker=ticker,
                        action=final.action,
                        limit_price=synthetic_limit,
                        stop_price=synthetic_stop,
                    )
        except Exception:
            logger.exception(
                "LLM shadow path failed for %s; live path unaffected",
                ticker,
            )

    def _log_shadow_decision(
        self,
        ticker: str,
        ctx: LLMContext,
        advisory: LLMDecision,
        final: FinalTradeDecision,
    ) -> int | None:
        """Insert a shadow row into the decisions table.

        Reuses the existing `decisions` schema (extended by init_v2_schema
        with bucket_key_used + policy_version + prompt_version columns).
        Shadow rows differ from rule-based rows by:

          - setup column: ``"llm_shadow/<tier_provenance>"`` (e.g.,
            ``"llm_shadow/t1_only"``, ``"llm_shadow/t1+t2"``,
            ``"llm_shadow/t1_failed"``).
          - reasons column: policy ``rejection_reason`` when the policy
            overrode the advisory; otherwise the advisory's ``setup_label``;
            otherwise the literal string ``"approved"``.
          - confidence column: the advisory's confidence (0-100).
          - bucket_key_used / policy_version / prompt_version: v2 columns,
            pinning the row to the code that produced it.

        Failures here are swallowed so the live path is never affected.
        Returns the inserted row id on success, None on failure or when
        decisions-to-db logging is disabled in config.
        """
        tier = advisory.tier_provenance or "unknown"
        setup = f"llm_shadow/{tier}"
        reasons = final.rejection_reason or advisory.setup_label or "approved"
        # Truncate reasons defensively; the decisions.reasons column has no
        # length limit in SQLite but the EOD report assumes <=280 chars.
        reasons = reasons[:280]

        ev_str = (
            f"{final.ev_score:+.3f}" if final.ev_score is not None else "n/a"
        )
        logger.info(
            "%s [%s] %s (qty_tier=%s conf=%d ev=%s liq_rej=%s reasons=%s)",
            ticker, setup, final.action, final.qty_tier,
            advisory.confidence, ev_str,
            int(final.liquidity_rejected), reasons,
        )
        if not self.config["logging"]["decisions_to_db"]:
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO decisions "
                    "(ts, ticker, action, setup, sentiment, confidence, "
                    " walls_status, reasons, bucket_key_used, "
                    " policy_version, prompt_version, ev_score, "
                    " liquidity_rejected) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        time.time(), ticker, final.action, setup,
                        None, advisory.confidence, None, reasons,
                        str(final.bucket_key),
                        final.policy_version, ctx.prompt_version,
                        final.ev_score,
                        1 if final.liquidity_rejected else 0,
                    ),
                )
                return cursor.lastrowid
        except Exception:
            logger.exception("Shadow decision INSERT failed for %s", ticker)
            return None

    def _log_synthetic_order(
        self,
        decision_id: int,
        ticker: str,
        action: str,
        limit_price: float,
        stop_price: float,
    ) -> None:
        """Insert a synthetic orders row for a shadow Buy/Sell decision.

        Shadow decisions never reach the broker, but
        scripts/backfill_shadow_outcomes.py joins decisions to orders
        to recover entry + stop prices. This synthetic row preserves
        that join shape without polluting real-trade analytics: rows
        are marked status='shadow', so any aggregation that cares
        about real fills can filter on status='submitted'.

        qty is set to 0 because shadow has no real position size; the
        backfill script does not read qty, so this doesn't affect
        outcomes. Failures are logged and swallowed so a synthetic-
        order INSERT failure can't break the shadow path.
        """
        side = "buy" if action == "Buy" else "sell"
        if not self.config["logging"]["decisions_to_db"]:
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO orders "
                    "(ts, ticker, side, qty, limit_price, stop_price, "
                    " alpaca_order_id, client_order_id, status, error, "
                    " decision_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        time.time(), ticker, side, 0,
                        limit_price, stop_price,
                        None, None, "shadow", None, decision_id,
                    ),
                )
        except Exception:
            logger.exception(
                "Synthetic order INSERT failed for shadow decision %d",
                decision_id,
            )

    def _log_decision(self, d: TradeDecision) -> int | None:
        """Log decision and return the inserted decision row id (for FK linking)."""
        if d.action == "Hold":
            logger.debug(
                "%s [%s] Hold: %s",
                d.ticker, d.setup, ", ".join(d.reasons),
            )
        else:
            logger.info(
                "%s [%s] %s (sent=%s conf=%d walls=%s)",
                d.ticker, d.setup, d.action,
                d.sentiment_score, d.technical_confidence,
                d.walls_status,
            )
        if not self.config["logging"]["decisions_to_db"]:
            return None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO decisions "
                "(ts, ticker, action, setup, sentiment, confidence, walls_status, reasons) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), d.ticker, d.action, d.setup,
                 d.sentiment_score, d.technical_confidence,
                 d.walls_status, " | ".join(d.reasons)),
            )
            return cursor.lastrowid

    def _log_order_result(
        self, result: OrderResult, limit_price: float, decision_id: int | None,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO orders "
                "(ts, ticker, side, qty, limit_price, stop_price, "
                " alpaca_order_id, client_order_id, status, error, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), result.ticker, result.side,
                    result.submitted_qty, limit_price, result.stop_price,
                    result.order_id, result.client_order_id,
                    "submitted" if result.success else "failed",
                    result.error, decision_id,
                ),
            )

    def _log_order_rejection(
        self, decision: TradeDecision, qty: int, price: float,
        reason: str, decision_id: int | None,
    ) -> None:
        side = "buy" if decision.action == "Buy" else "sell"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO orders "
                "(ts, ticker, side, qty, limit_price, stop_price, "
                " alpaca_order_id, client_order_id, status, error, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), decision.ticker, side, qty, price, None,
                    None, None, "risk_rejected", reason, decision_id,
                ),
            )

    # ------------------------------------------------------------------
    # Flatten and EOD journal
    # ------------------------------------------------------------------

    async def _run_flatten(self) -> None:
        """15:55 ET routine: cancel all orders + close all positions."""
        if self.order_client is None:
            return
        logger.info("=== FLATTEN ROUTINE STARTING (15:55 ET) ===")
        positions_before = await self.order_client.get_open_positions()
        if not positions_before:
            logger.info("No positions to flatten.")
            return
        logger.info("Flattening %d positions: %s",
                    len(positions_before),
                    [f"{p.ticker}({p.quantity})" for p in positions_before])
        result = await self.order_client.close_all_positions(cancel_orders=True)
        logger.info(
            "Flatten complete: cancelled=%d closed=%d errors=%d",
            len(result["cancelled_orders"]),
            len(result["closed_positions"]),
            len(result["errors"]),
        )
        for err in result["errors"]:
            logger.error("Flatten error: %s", err)

    async def _run_eod_journal(self, date_str: str) -> None:
        """16:30 ET routine: write a daily journal markdown report."""
        from journal.eod_report import write_eod_report
        if self.order_client is None:
            return
        equity = await self.order_client.get_account_equity(force_refresh=True)
        report_path = write_eod_report(
            db_path=self.db_path,
            date_str=date_str,
            ending_equity=equity,
            output_dir=Path(self.config.get("storage", {}).get("journal_dir", "journals")),
        )
        logger.info("EOD journal written: %s", report_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time(s: str) -> dtime:
    """Parse 'HH:MM' to a datetime.time."""
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress HTTP-client URL logging at INFO level. Polygon (and some
    # other vendors) put the API key in URL query parameters rather than
    # a header, so logging full URLs at INFO would leak credentials into
    # journalctl / log files. Each library below logs at INFO by default;
    # WARNING keeps real errors visible while hiding routine request URLs.
    # Bug fix 2026-05-04 after a Polygon key leaked via httpx INFO logs.
    for noisy_logger in ("httpx", "httpcore", "aiohttp", "anthropic",
                         "urllib3"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


async def amain(config_path: str) -> None:
    config = load_config(config_path)
    setup_logging(config["logging"]["level"])
    platform = TradingPlatform(config)
    await platform.boot()


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/settings.yaml"
    try:
        asyncio.run(amain(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
