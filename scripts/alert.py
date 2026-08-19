#!/usr/bin/env python3
"""
alert.py - level and feed-health alerting on top of `scripts.watch` live JSON.

Reads the same data/live/<SYM>.json that scripts.watch writes. Never calls
Schwab or Alpaca, so it consumes no market-data connection (one connection per
account, per vendor) and cannot interfere with the watcher.

    python -m scripts.alert SNDK --levels 1600,1625.78,1645.89
    python -m scripts.alert SNDK --levels 1625.78 --replay      # backtest thresholds

WHY BID/ASK AND NOT LAST
------------------------
A level is not broken because a 5-share odd lot printed through it. On
2026-08-19 SNDK printed 1624.00 through the 1625.78 line and was back above it
45 seconds later with the spread blown out to $3.08. So a side is only
classified from the resting book:

    bid > level   -> whole book above the level  -> "above"
    ask < level   -> whole book below the level  -> "below"
    otherwise     -> level is inside the spread  -> undecided

WHY PACE AND NOT CUMULATIVE VOLUME
----------------------------------
v1 required N shares to trade since the cross began, with no time bound. That
is a clock, not a filter: given enough time any cross accumulates the quota. On
2026-08-19 it fired 2m05s and 2m30s late on two crosses, and blocked a real one
that traded 970 shares in 10s. Confirmation is now measured as a RATE over a
fixed window of snapshots:

    pace = shares traded during the window / window minutes

Calibrated against the 2026-08-19 SNDK pre-market tape:

    05:04 break above  32,520 sh/min   fires
    05:12 break below  10,044 sh/min   fires
    05:28 break below   5,820 sh/min   fires
    05:15 slow drift    3,852 sh/min   blocked
    05:29 dip           1,794 sh/min   blocked
    05:23 drift           654 sh/min   blocked

WHY SNAPSHOTS AND NOT POLLS
---------------------------
The watcher writes every 5s. Polling the file at 1s means four of every five
reads are the same snapshot, so "3 consecutive polls" was really "3 seconds"
and confirmed nothing. Only distinct `updated_at_epoch` values advance the
state machine.

Straddle polls no longer wipe pending state - a level flickering inside a wide
spread is absence of evidence, not evidence of reversal. --straddle-grace
consecutive undecided snapshots are tolerated before pending resets.

Exit: Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MT = timezone(timedelta(hours=-6), "MT")

# ---------------------------------------------------------------- terminal --

def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


COLOR = _enable_ansi()
GREEN, RED, YELLOW, CYAN, GREY, BOLD = "32", "31", "33", "36", "90", "1"


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def beep(pattern: str = "alert") -> None:
    tones = {
        "up": [(880, 90), (1175, 140)],
        "down": [(660, 90), (440, 160)],
        "alert": [(1000, 70), (1000, 70)],
        "bad": [(300, 220), (240, 320)],
    }.get(pattern, [(1000, 100)])
    try:
        import winsound

        for freq, dur in tones:
            winsound.Beep(freq, dur)
    except Exception:
        sys.stdout.write("\a")
        sys.stdout.flush()


# ------------------------------------------------------------------- model --

@dataclass
class Snapshot:
    ts_mt: str = "?"
    epoch: float = 0.0
    market_state: str = "?"
    errors: int = 0
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    spread_bps: float | None = None
    pct: float | None = None
    volume: int | None = None
    adv: float | None = None
    stale: bool = False
    halted: bool = False
    hi: float | None = None
    lo: float | None = None
    pace_1m: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass
class Level:
    """One price line and its confirmation state machine."""

    price: float
    side: str | None = None
    pending: str | None = None
    snaps: int = 0                     # distinct snapshots supporting `pending`
    straddles: int = 0                 # consecutive undecided snapshots
    vol_at_cross: int | None = None
    t_at_cross: float | None = None    # snapshot epoch when the cross began
    last_fire: float = 0.0
    last_progress: float = 0.0

    def classify(self, bid: float | None, ask: float | None) -> str | None:
        if bid is not None and bid > self.price:
            return "above"
        if ask is not None and ask < self.price:
            return "below"
        return None

    def reset(self) -> None:
        self.pending = None
        self.snaps = 0
        self.straddles = 0
        self.vol_at_cross = None
        self.t_at_cross = None


def parse_snapshot(doc: dict) -> Snapshot:
    q = doc.get("schwab") or doc.get("alpaca") or {}
    d = doc.get("derived") or {}
    return Snapshot(
        ts_mt=doc.get("updated_at_mt", "?"),
        epoch=float(doc.get("updated_at_epoch") or 0.0),
        market_state=doc.get("market_state", "?"),
        errors=int(doc.get("errors") or 0),
        last=q.get("last_price"),
        bid=q.get("bid"),
        ask=q.get("ask"),
        bid_size=q.get("bid_size"),
        ask_size=q.get("ask_size"),
        spread_bps=q.get("spread_bps"),
        pct=q.get("net_percent_change") or q.get("change_from_close_pct"),
        volume=q.get("total_volume") or q.get("day_volume"),
        adv=q.get("avg_10day_volume"),
        stale=bool(q.get("is_stale")),
        halted=bool(q.get("is_halted")),
        hi=d.get("watch_high"),
        lo=d.get("watch_low"),
        pace_1m=d.get("shares_per_min_1m"),
    )


def read_doc(path: Path, retries: int = 5, delay: float = 0.04) -> dict:
    """Read the watcher's JSON, tolerating a partially written file."""
    err: Exception | None = None
    for _ in range(retries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            err = exc
            time.sleep(delay)
    raise RuntimeError(f"unreadable after {retries} tries: {path}: {err}")


# ------------------------------------------------------------------ output --

def now_mt() -> str:
    return datetime.now(MT).strftime("%H:%M:%S")


def fmt(v: float | None, nd: int = 2) -> str:
    return "-" if v is None else f"{v:,.{nd}f}"


class Log:
    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, line: str) -> None:
        if not self.path:
            return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line.rstrip() + "\n")
        except OSError:
            pass


def banner(title: str, body: list[str], tint: str) -> str:
    width = 74
    out = [c("=" * width, tint), c(f"  {title}", tint + ";1")]
    out += [f"  {line}" for line in body if line]
    out.append(c("=" * width, tint))
    return "\n".join(out)


def heartbeat(s: Snapshot, sym: str) -> str:
    pct = f"{s.pct:+.2f}%" if s.pct is not None else "  -   "
    tint = GREEN if (s.pct or 0) > 0 else RED if (s.pct or 0) < 0 else GREY
    book = f"{fmt(s.bid)}x{s.bid_size or 0:<4} / {fmt(s.ask)}x{s.ask_size or 0:<4}"
    sp = f"{s.spread_bps:.1f}bp" if s.spread_bps is not None else "-"
    return (
        f"{c(now_mt(), GREY)}  {sym}  {c(fmt(s.last), BOLD)}  {c(pct, tint)}  "
        f"{book}  {sp}  vol {s.volume:,}  pace {s.pace_1m or 0:,.0f}/m"
    )


# ---------------------------------------------------------- state machine ---

def step_level(
    lv: Level,
    s: Snapshot,
    confirm: int,
    min_pace: float,
    straddle_grace: int,
    cooldown: float,
    now: float,
) -> tuple[str | None, str | None]:
    """Advance one level by one DISTINCT snapshot.

    Returns (event, progress) where event is 'above'/'below' when an alert
    should fire, and progress is an optional human-readable pending line.
    """
    if lv.side is None:
        ref = s.mid
        if ref is None:
            return None, None
        lv.side = "above" if ref >= lv.price else "below"
        return None, f"baseline {lv.price:,.2f}: {lv.side} (mid {ref:,.2f})"

    cand = lv.classify(s.bid, s.ask)

    if cand is None:
        if lv.pending is not None:
            lv.straddles += 1
            if lv.straddles > straddle_grace:
                lv.reset()
        return None, None

    lv.straddles = 0

    if cand == lv.side:
        lv.reset()
        return None, None

    if lv.pending != cand:
        lv.pending = cand
        lv.snaps = 1
        lv.vol_at_cross = s.volume
        lv.t_at_cross = s.epoch
        return None, None

    lv.snaps += 1
    traded = (s.volume or 0) - (lv.vol_at_cross or s.volume or 0)
    elapsed = max(s.epoch - (lv.t_at_cross or s.epoch), 1e-6)
    pace = traded / (elapsed / 60.0)

    if lv.snaps < confirm:
        return None, None

    if pace < min_pace:
        # Visible near-miss: silence here is what made v1 feel broken.
        return None, (
            f"pending {'BREAK BELOW' if cand == 'below' else 'BREAK ABOVE'} "
            f"{lv.price:,.2f}  {lv.snaps} snaps  {traded:,} sh  "
            f"{pace:,.0f}/m (need {min_pace:,.0f})"
        )

    if now - lv.last_fire < cooldown:
        return None, None

    lv.side = cand
    lv.last_fire = now
    lv.reset()
    return cand, f"{traded:,} shares in {elapsed:.0f}s = {pace:,.0f}/min"


def fire_banner(lv_price: float, side: str, s: Snapshot, detail: str) -> str:
    direction = "BREAK BELOW" if side == "below" else "BREAK ABOVE"
    tint = RED if side == "below" else GREEN
    adv = f"  ({s.volume / s.adv * 100:.1f}% ADV)" if s.volume and s.adv else ""
    return banner(
        f"** {direction} {lv_price:,.2f} **   {now_mt()} MT",
        [
            f"last {fmt(s.last)}" + (f"  ({s.pct:+.2f}%)" if s.pct is not None else ""),
            f"book {fmt(s.bid)}x{s.bid_size or 0} / {fmt(s.ask)}x{s.ask_size or 0}"
            + (f"   spread {s.spread_bps:.1f}bp" if s.spread_bps else ""),
            f"confirmed {detail}",
            f"session lo {fmt(s.lo)}  hi {fmt(s.hi)}  vol {s.volume:,}{adv}"
            if s.volume
            else "",
        ],
        tint,
    )


# ----------------------------------------------------------------- replay ---

def replay(path: Path, levels: list[Level], args) -> int:
    """Replay the watcher's stored series through the state machine.

    Lets you calibrate --min-pace against real tape instead of guessing.
    """
    doc = read_doc(path)
    series = doc.get("series") or []
    if not series:
        print("no series in file", file=sys.stderr)
        return 2
    tmpl = parse_snapshot(doc)
    print(c(f"replaying {len(series)} snapshots  min-pace {args.min_pace:,.0f}/m  "
            f"confirm {args.confirm}  grace {args.straddle_grace}", CYAN))
    fired = 0
    for ts, px, cv, bid, ask in series:
        s = Snapshot(
            epoch=ts / 1000.0,
            last=px,
            bid=bid,
            ask=ask,
            volume=cv,
            adv=tmpl.adv,
            hi=tmpl.hi,
            lo=tmpl.lo,
            pct=None if not tmpl.pct else None,
        )
        t = datetime.fromtimestamp(ts / 1000, MT).strftime("%H:%M:%S")
        for lv in levels:
            ev, msg = step_level(
                lv, s, args.confirm, args.min_pace, args.straddle_grace,
                args.cooldown, ts / 1000.0,
            )
            if ev:
                fired += 1
                print(c(f"  {t}  {'BREAK BELOW' if ev == 'below' else 'BREAK ABOVE'} "
                        f"{lv.price:,.2f}  last {px:,.2f}  {msg}",
                        RED if ev == "below" else GREEN))
            elif msg and msg.startswith("baseline"):
                print(c(f"  {t}  {msg}", GREY))
    print(c(f"{fired} alert(s) over the replayed tape", CYAN))
    return 0


# -------------------------------------------------------------------- main --

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="alert",
        description="Level and feed-health alerts on top of scripts.watch JSON.",
    )
    ap.add_argument("symbol")
    ap.add_argument("--levels", required=True, help="comma-separated prices")
    ap.add_argument("--file", help="override path to the watcher JSON")
    ap.add_argument("--interval", type=float, default=1.0, help="file poll seconds")
    ap.add_argument(
        "--confirm",
        type=int,
        default=3,
        help="DISTINCT snapshots the whole book must sit past a level (default 3)",
    )
    ap.add_argument(
        "--min-pace",
        type=float,
        default=5000.0,
        help="shares/min that must trade during confirmation (default 5000)",
    )
    ap.add_argument(
        "--straddle-grace",
        type=int,
        default=2,
        help="undecided snapshots tolerated before pending resets (default 2)",
    )
    ap.add_argument("--cooldown", type=float, default=60.0)
    ap.add_argument("--stall", type=float, default=30.0)
    ap.add_argument("--heartbeat", type=float, default=30.0)
    ap.add_argument(
        "--replay",
        action="store_true",
        help="replay the stored series to calibrate thresholds, then exit",
    )
    ap.add_argument("--no-beep", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sym = args.symbol.upper()
    path = Path(args.file) if args.file else Path("data/live") / f"{sym}.json"
    if not path.exists():
        print(f"no such file: {path.resolve()}", file=sys.stderr)
        print("is scripts.watch running, and are you in C:\\trading\\LLM model ?",
              file=sys.stderr)
        return 2

    try:
        levels = [Level(float(x)) for x in args.levels.split(",") if x.strip()]
    except ValueError:
        print("--levels must be comma-separated numbers", file=sys.stderr)
        return 2
    if not levels:
        print("--levels must contain at least one price", file=sys.stderr)
        return 2
    levels.sort(key=lambda lv: lv.price)

    if args.replay:
        return replay(path, levels, args)

    log = Log(None if args.no_log else path.with_name(f"{sym}_alerts.log"))
    say = (lambda p: None) if args.no_beep else beep

    print(c(f"alerting {sym} on " + ", ".join(f"{lv.price:,.2f}" for lv in levels), CYAN))
    print(c(f"source {path.resolve()}", GREY))
    print(c(f"confirm={args.confirm} snapshots  min-pace={args.min_pace:,.0f}/min  "
            f"grace={args.straddle_grace}  cooldown={args.cooldown:.0f}s", GREY))
    print(c("crosses judged on bid/ask and pace, not last. Ctrl-C to stop.", GREY))
    print()

    last_epoch = 0.0
    seen_at = time.monotonic()
    stall_fired = False
    prev_errors = 0
    prev_state: str | None = None
    prev_halt = prev_stale = False
    next_hb = 0.0
    first = True

    try:
        while True:
            loop = time.monotonic()
            try:
                s = parse_snapshot(read_doc(path))
            except RuntimeError as exc:
                print(c(f"{now_mt()}  READ FAIL  {exc}", YELLOW))
                time.sleep(args.interval)
                continue

            fresh = s.epoch != last_epoch

            # -- feed health -------------------------------------------------
            if fresh:
                if stall_fired:
                    msg = f"{now_mt()}  feed recovered, watcher is writing again"
                    print(c(msg, GREEN))
                    log.write(msg)
                last_epoch = s.epoch
                seen_at = loop
                stall_fired = False
            elif not stall_fired and loop - seen_at > args.stall:
                stall_fired = True
                out = banner(
                    "** FEED STALL **",
                    [
                        f"{path.name} has not advanced in {loop - seen_at:.0f}s",
                        f"last write {s.ts_mt}",
                        "scripts.watch may have died, or Schwab auth expired",
                    ],
                    RED,
                )
                print(out)
                log.write(out)
                say("bad")

            if s.errors > prev_errors:
                out = banner("** WATCHER ERRORS **",
                             [f"error count {prev_errors} -> {s.errors} at {s.ts_mt}"],
                             YELLOW)
                print(out)
                log.write(out)
                say("bad")
            prev_errors = s.errors

            if s.halted and not prev_halt:
                out = banner("** HALTED **", [f"{sym} halted at {s.ts_mt}"], RED)
                print(out)
                log.write(out)
                say("bad")
            prev_halt = s.halted

            if s.stale and not prev_stale:
                out = f"{now_mt()}  STALE QUOTE flagged by watcher"
                print(c(out, YELLOW))
                log.write(out)
            prev_stale = s.stale

            if prev_state is not None and s.market_state != prev_state:
                out = banner("** MARKET STATE **",
                             [f"{prev_state} -> {s.market_state} at {s.ts_mt}",
                              f"{sym} {fmt(s.last)}"], CYAN)
                print(out)
                log.write(out)
                say("alert")
            prev_state = s.market_state

            # -- levels: only advance on a NEW snapshot ----------------------
            if fresh:
                for lv in levels:
                    ev, msg = step_level(
                        lv, s, args.confirm, args.min_pace,
                        args.straddle_grace, args.cooldown, loop,
                    )
                    if ev:
                        out = fire_banner(lv.price, ev, s, msg or "")
                        print(out)
                        log.write(out)
                        say("down" if ev == "below" else "up")
                    elif msg:
                        if msg.startswith("baseline"):
                            print(c(f"{now_mt()}  {msg}", GREY))
                        elif loop - lv.last_progress >= 10.0:
                            lv.last_progress = loop
                            print(c(f"{now_mt()}  {msg}", YELLOW))

            if first or (args.heartbeat and loop >= next_hb):
                print(heartbeat(s, sym))
                next_hb = loop + args.heartbeat
                first = False

            time.sleep(max(0.0, args.interval - (time.monotonic() - loop)))

    except KeyboardInterrupt:
        print()
        print(c("stopped", GREY))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
