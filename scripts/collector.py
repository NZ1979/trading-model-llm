#!/usr/bin/env python3
"""Unattended data collector — pre-market, regular hours, and post-market.

    python -m scripts.collector
    python -m scripts.collector --symbol SNDK --dry-run
    python -m scripts.collector --symbols SNDK,MU,WDC --no-dashboard

Leave it running in its own window, like scripts.watch. It collects; it does
not decide, alert, or trade.

WHY THIS EXISTS DESPITE CURRENT_SCOPE.md
----------------------------------------
That document says nothing autonomous, nothing watching. This is a deliberate,
user-authorised exception granted 2026-08-19, recorded in CURRENT_SCOPE.md
under "Authorised exception". A future session must not "fix" this by deleting
it. The exception is narrow and its boundary matters:

    IN SCOPE   collecting and storing data; regenerating a static dashboard
    OUT        alerting, notifying, deciding, ordering, or anything that
               reaches the user without them asking

Nothing here writes to a channel, opens a window, plays a sound, or contacts a
broker. If a future change adds any of those, it is a new decision and needs a
new conversation.

WHAT IT DOES, AND WHY EACH CADENCE
----------------------------------
1. BARS every --bar-every minutes while any session is open. Alpaca REST, so
   no streaming connection is consumed and it cannot collide with the one
   streaming slot per account per vendor. Writes are UPSERTs, so overlapping
   backfills are free and late vendor revisions land automatically.

2. CHAINS twice a day, at --am-fetch and --pm-fetch. Not more, and not at
   arbitrary times, because the two fetches answer different questions:

       pre-market fetch   open interest is T+1, so it is already final;
                          this is the one that supplies levels for the day
       post-close fetch   volume only finishes accumulating after the close;
                          a pre-market fetch records 0 for every contract

   Measured 2026-08-19: the 08-18 chain was fetched 07:30 ET, so its volume
   column was uniformly zero and the day-over-day OI change could not be
   attributed to flow at all. Two fetches fixes that permanently.

   Both use --strikes 400. A narrow fetch OVERWRITES rows written by a wider
   one (chain_snapshots is UNIQUE(session_date, symbol)), so a single stingy
   fetch silently truncates that session's stored chain forever.

   Why 400 and not 200. Measured 2026-08-19 after SNDK fell 3.9%: a --strikes
   200 fetch returned EXACTLY 400 rows (200 strikes x 2 sides) on all 8
   expirations. Returning the cap on every expiration means the cap, not the
   chain, set the boundary. Schwab applies strikeCount PER EXPIRATION around
   each expiration's own ATM, so a falling spot drags every window down and
   sheds the top strikes. The re-fetch at 400 returned 432-742 rows per
   expiration -- under the 800 cap, so 400 is NOT binding -- and revealed
   PUT 800 carrying 10,126 OI at 8/8 coverage, a strike that had 0/8 coverage
   at 200 and was therefore the largest put concentration on the board while
   being completely invisible. Same failure as strike 2000 at --strikes 40.

   The saturation test is mechanical: the fetch was truncated if and only if
   any expiration returns exactly 2 x --strikes rows. Check it before trusting
   any chain-wide aggregate; 400 is today's answer, not a permanent one.

3. DASHBOARD every --dash-every minutes. Pure re-render from stored data, no
   vendor call.

FAIL LOUD, KEEP RUNNING
-----------------------
Every task is wrapped. A failure is logged with its exception type, counted,
and surfaced in the state file — never swallowed, and never fatal. A collector
that dies at 09:31 because one REST call timed out is worse than useless,
because the gap looks like a quiet market.

State lives in data/live/collector_state.json: last successful run of each
task, per-task error counts, and a heartbeat. Read it to tell "running and
quiet" apart from "dead since 06:00".
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
logger = logging.getLogger("collector")

PRE_OPEN = dtime(4, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
POST_CLOSE = dtime(20, 0)


def session_phase(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "WEEKEND"
    t = now_et.time()
    if PRE_OPEN <= t < REGULAR_OPEN:
        return "PRE"
    if REGULAR_OPEN <= t < REGULAR_CLOSE:
        return "REGULAR"
    if REGULAR_CLOSE <= t < POST_CLOSE:
        return "POST"
    return "CLOSED"


class State:
    """Durable task state. Survives restarts so a bounce cannot double-fetch."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"tasks": {}, "errors": {}, "started": None,
                           "heartbeat": None}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("state file unreadable, starting fresh: %s", exc)

    def last(self, task: str) -> str | None:
        return self.data["tasks"].get(task)

    def mark(self, task: str, stamp: str) -> None:
        self.data["tasks"][task] = stamp
        self.save()

    def error(self, task: str, exc: BaseException) -> None:
        e = self.data.setdefault("errors", {}).setdefault(task, {"n": 0})
        e["n"] += 1
        e["last"] = f"{type(exc).__name__}: {exc}"[:300]
        e["at"] = datetime.now(NY).isoformat(timespec="seconds")
        self.save()

    def beat(self, phase: str) -> None:
        self.data["heartbeat"] = datetime.now(NY).isoformat(timespec="seconds")
        self.data["phase"] = phase
        self.save()

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("could not write state: %s", exc)


def run_bars(symbols: list[str], days: int, dry: bool) -> None:
    from scripts.backfill_bars import main as backfill
    argv = ["--symbol", ",".join(symbols), "--days", str(days)]
    logger.info("BARS  backfill_bars %s", " ".join(argv))
    if dry:
        return
    rc = backfill(argv)
    if rc != 0:
        raise RuntimeError(f"backfill_bars exited {rc}")


def run_chain(symbols: list[str], strikes: int, dte: int, dry: bool) -> None:
    """Subprocess rather than an import: fetch_option_chain reads sys.argv and
    prints a full report. Isolating it keeps its output and its exit code
    intact, and a crash there cannot take the collector down with it."""
    for sym in symbols:
        cmd = [sys.executable, "-m", "scripts.fetch_option_chain",
               "--symbol", sym, "--strikes", str(strikes), "--dte", str(dte)]
        logger.info("CHAIN %s", " ".join(cmd[2:]))
        if dry:
            continue
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(
                f"fetch_option_chain {sym} exited {r.returncode}: "
                f"{(r.stderr or '')[:300]}")
        for line in (r.stdout or "").splitlines():
            if "stored" in line or "contracts" in line.lower():
                logger.info("      %s", line.strip())
                break


def run_dashboard(symbols: list[str], dry: bool) -> None:
    from scripts.sndk_dashboard import main as dash
    for sym in symbols:
        logger.info("DASH  %s", sym)
        if dry:
            continue
        rc = dash(["--symbol", sym])
        if rc != 0:
            raise RuntimeError(f"sndk_dashboard {sym} exited {rc}")


def _due_interval(state: State, task: str, minutes: int, now: datetime) -> bool:
    last = state.last(task)
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - prev >= timedelta(minutes=minutes)


def _due_daily(state: State, task: str, at: dtime, now: datetime) -> bool:
    """True once per calendar day, at or after `at`. A restart after the window
    still fires it, because a missed pre-market fetch is worth catching late."""
    if now.time() < at or now.weekday() >= 5:
        return False
    return (state.last(task) or "")[:10] != now.date().isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="collector",
        description="Unattended collection across pre, regular and post market.")
    ap.add_argument("--symbols", default="SNDK",
                    help="comma-separated; chains are fetched for each")
    ap.add_argument("--bar-every", type=int, default=5,
                    help="minutes between bar backfills while a session is open")
    ap.add_argument("--bar-days", type=int, default=2,
                    help="days of bars to re-pull each time (UPSERT, so cheap)")
    ap.add_argument("--dash-every", type=int, default=5)
    ap.add_argument("--am-fetch", default="08:00",
                    help="ET time for the pre-market chain fetch")
    ap.add_argument("--pm-fetch", default="16:15",
                    help="ET time for the post-close chain fetch (volume final)")
    ap.add_argument("--strikes", type=int, default=400,
                    help="NEVER lower this: a narrow fetch overwrites a wider "
                         "one for the same session, permanently. 200 was "
                         "measured SATURATED on all 8 SNDK expirations "
                         "2026-08-19; see module docstring.")
    ap.add_argument("--dte", type=int, default=60)
    ap.add_argument("--state", default="data/live/collector_state.json")
    ap.add_argument("--tick", type=int, default=30, help="loop seconds")
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would run, call nothing")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")
    # Rule 22: httpx logs full URLs at INFO. Never let a vendor URL reach a log.
    for noisy in ("httpx", "httpcore", "urllib3", "aiohttp", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("--symbols must name at least one ticker", file=sys.stderr)
        return 2
    try:
        am = dtime.fromisoformat(args.am_fetch)
        pm = dtime.fromisoformat(args.pm_fetch)
    except ValueError:
        print("--am-fetch / --pm-fetch must be HH:MM", file=sys.stderr)
        return 2
    if args.strikes < 400:
        logger.warning("--strikes %d is below 400. A narrow fetch OVERWRITES "
                       "wider rows for the same session and cannot be undone.",
                       args.strikes)

    state_path = Path(args.state)
    if args.dry_run:
        # A dry run must not touch the real state. The first version marked
        # bars/chain_am/dashboard as done without doing them, so a real start
        # immediately afterwards SKIPPED the catch-up chain fetch — a dry run
        # that silently costs you a fetch is worse than no dry run. Same
        # machinery, scratch file, zero side effects.
        state_path = state_path.with_suffix(".dryrun.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = State(state_path)
    state.data["started"] = datetime.now(NY).isoformat(timespec="seconds")
    state.save()

    stop = {"now": False}

    def _sigint(*_):
        stop["now"] = True
        logger.info("stopping after this cycle")

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except (AttributeError, ValueError):
        pass

    logger.info("collector up | symbols %s | bars every %dm | dash every %dm | "
                "chains %s and %s ET | strikes %d%s",
                ",".join(symbols), args.bar_every, args.dash_every,
                args.am_fetch, args.pm_fetch, args.strikes,
                "  [DRY RUN]" if args.dry_run else "")
    logger.info("state %s — read it to tell 'quiet' from 'dead'", state_path)

    last_phase = None
    while not stop["now"]:
        now = datetime.now(NY)
        phase = session_phase(now)
        if phase != last_phase:
            logger.info("--- %s (%s ET) ---", phase, now.strftime("%H:%M"))
            last_phase = phase
        state.beat(phase)

        open_now = phase in ("PRE", "REGULAR", "POST")

        if open_now and _due_interval(state, "bars", args.bar_every, now):
            try:
                run_bars(symbols, args.bar_days, args.dry_run)
                state.mark("bars", now.isoformat(timespec="seconds"))
            except Exception as exc:
                logger.error("BARS FAILED: %s: %s", type(exc).__name__, exc)
                state.error("bars", exc)

        for task, at in (("chain_am", am), ("chain_pm", pm)):
            if _due_daily(state, task, at, now):
                try:
                    run_chain(symbols, args.strikes, args.dte, args.dry_run)
                    state.mark(task, now.isoformat(timespec="seconds"))
                except Exception as exc:
                    logger.error("%s FAILED: %s: %s", task.upper(),
                                 type(exc).__name__, exc)
                    state.error(task, exc)

        if (not args.no_dashboard and open_now
                and _due_interval(state, "dashboard", args.dash_every, now)):
            try:
                run_dashboard(symbols, args.dry_run)
                state.mark("dashboard", now.isoformat(timespec="seconds"))
            except Exception as exc:
                logger.error("DASH FAILED: %s: %s", type(exc).__name__, exc)
                state.error("dashboard", exc)

        for _ in range(max(args.tick, 1)):
            if stop["now"]:
                break
            time.sleep(1)

    logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
