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
from data.alpaca_market_data import AlpacaBarStream
from data.bar_aggregator import BarAggregator
from data.bar_types import FiveMinBar, MinuteBar
from data.news_pipeline import NewsSentimentPipeline, latest_sentiment
from data.polygon_feed import PolygonRESTClient, backfill_premarket_baselines, backfill_daily_bars
from data.polygon_news import PolygonNewsPipeline
from data.finnhub_feed import FinnhubClient, refresh_earnings_calendar, is_earnings_day
from data.watchlist_builder import read_watchlist_file, refresh_dynamic_watchlist
from execution.alpaca_orders import AlpacaOrderClient, OrderResult
from strategy.risk import size_from_risk, validate_order
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
# Trading platform
# ---------------------------------------------------------------------------

class TradingPlatform:
    """Owns all state and orchestrates the daily lifecycle."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
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
        # No background task — used on-demand by the daily backfill at 8:30 ET
        # to populate the catalysts table.
        self.finnhub_client = FinnhubClient(api_key=secrets["finnhub_key"])
        await self.finnhub_client.__aenter__()
        logger.info("Finnhub client initialized (earnings calendar veto enabled)")

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

        # 5. Task supervisor — watches the long-running tasks for silent death.
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
                            "Task %s exited cleanly (returned None) — this is "
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

        backfill_time = _parse_time(self.config["schedule"]["baseline_backfill_time"])
        pm_time = _parse_time(self.config["schedule"]["premarket_context_time"])
        flatten_time = _parse_time(self.config["schedule"]["flatten_time"])
        journal_time = _parse_time(self.config["schedule"]["journal_time"])

        while not self._shutdown.is_set():
            now_et = datetime.now(ET)
            today = now_et.date().isoformat()
            now_t = now_et.time()
            is_weekday = now_et.weekday() < 5  # Mon-Fri

            # Backfill (08:30 ET, weekdays only — needs market data)
            if is_weekday and backfill_done_for != today and now_t >= backfill_time:
                try:
                    await self._run_baseline_backfill()
                    backfill_done_for = today
                except Exception:
                    logger.exception("Baseline backfill failed; will retry")

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

            await asyncio.sleep(30)

    async def _run_baseline_backfill(self) -> None:
        """Fetch daily bars (300d) and 20-day PM volume baselines.

        Both phases run concurrently with Semaphore caps. Pre-fix this method
        looped sequentially fetching MINUTE bars over 450 days per ticker and
        resampled to daily — silently failed on most of 503 tickers (only
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

        # Build DailyContext per symbol from the fetched DataFrames
        for ticker, daily in daily_dfs.items():
            try:
                ctx = compute_daily_context(daily, ticker)
                if ctx is not None:
                    self.symbols[ticker].daily_ctx = ctx
                    self.symbols[ticker].daily_df = daily
            except Exception:
                logger.exception("Daily context build failed for %s", ticker)

        n_daily = sum(1 for s in self.symbols.values() if s.daily_ctx is not None)
        n_pm = len(self.pm_baselines)
        logger.info("Backfill complete: %d daily contexts, %d PM baselines",
                    n_daily, n_pm)

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
        """Refresh the dynamic watchlist (Phase B).

        Builds top-500-by-30D-ADV from the union of S&P 500 + NASDAQ-100 +
        DJIA constituents (sourced from Wikipedia) and writes
        `config/watchlist_dynamic.json`. Runs daily at 08:30 ET including
        weekends — no weekday gating. Membership rarely changes, but ADV
        ranking does shift slightly day-to-day.

        Does NOT update self.watchlist mid-run. The new list is picked up
        on next service restart, when __init__ reads watchlist_dynamic.json.
        """
        polygon_key = self.config["_secrets"]["polygon_key"]
        output_path = Path("config/watchlist_dynamic.json")
        await refresh_dynamic_watchlist(polygon_key, output_path, top_n=500)

    def _compute_premarket_contexts(self) -> None:
        """At 9:30 ET, compute PM context per symbol from buffered PM bars."""
        for ticker, state in self.symbols.items():
            if state.daily_ctx is None:
                continue
            df = state.to_dataframe()
            if df.empty:
                continue
            # We need a daily DataFrame for compute_premarket_context;
            # for the daily ATR it requires daily bars. We have daily_ctx
            # already but compute_premarket_context expects a DataFrame.
            # Rebuild a minimal daily DataFrame from cached state — or just
            # skip if we don't have it cached as raw bars.
            # SIMPLIFICATION: store the daily DataFrame on state during backfill
            # so we can use it here. This is done in _run_baseline_backfill below.
            daily_df = getattr(state, "daily_df", None)
            if daily_df is None:
                continue

            ctx = compute_premarket_context(
                daily_df=daily_df,
                today_full_session_df=df,
                ticker=ticker,
                historical_pm_volumes=self.pm_baselines.get(ticker),
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
        # `.action` — TradeDecision uses `.action` but TechnicalSignal does
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
        is_changed = (
            decision.action != state.last_decision_action
            or decision.setup != state.last_decision_setup
        )
        decision_id: int | None = None
        if is_actionable or is_changed:
            decision_id = self._log_decision(decision)
            state.last_decision_action = decision.action
            state.last_decision_setup = decision.setup

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

        stop_loss_pct = self.config["risk"]["stop_loss_pct"]
        risk_per_trade = self.config["risk"].get("risk_per_trade_pct", 0.5)
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

        # Submit bracket
        result = await self.order_client.submit_bracket_order(
            ticker=decision.ticker,
            side=side,
            qty=check.quantity,
            limit_price=round(latest_price, 2),
            stop_price=check.stop_price,
        )
        if result.success:
            logger.info(
                "ORDER %s %s %d @ ~$%.2f (stop $%.2f, %d%% pos / %d%% total)",
                result.side.upper(), result.ticker, result.submitted_qty,
                latest_price, result.stop_price,
                int(check.position_pct), int(check.total_exposure_pct),
            )
        else:
            logger.error("ORDER REJECTED %s: %s", result.ticker, result.error)
        self._log_order_result(result, latest_price, decision_id)

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
