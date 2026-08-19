#!/usr/bin/env python3
"""
selftest.py - prove the alerter actually fires, without touching the live one.

Runs in its own window. Does NOT read data/live/*.json, does NOT open any
vendor connection, and does NOT disturb the running scripts.watch or
scripts.alert. It imports the SAME scripts/alert.py you are running and drives
synthetic snapshots through the real state machine.

    python -m scripts.selftest

Two things it proves:

  1. REGRESSION - replays the 2026-08-19 SNDK cases that broke v1 and checks
     each fires or stays silent as intended. Prints PASS/FAIL per case and
     exits non-zero on any failure.

  2. ALARM PATH - fires one real banner and one real beep at the end, so you
     see and hear exactly what a live break will look and sound like.

If case 3 fails, the bug that cost you the 05:28 break is back.
"""

from __future__ import annotations

import sys
import time

try:
    from scripts.alert import (  # running as a module inside the repo
        Level, Snapshot, step_level, fire_banner, beep, c, GREEN, RED, GREY, CYAN,
    )
except ImportError:  # running the file directly
    from alert import (
        Level, Snapshot, step_level, fire_banner, beep, c, GREEN, RED, GREY, CYAN,
    )

LEVEL = 1625.78
CONFIRM, MIN_PACE, GRACE, COOLDOWN = 3, 5000.0, 2, 60.0


def drive(steps: list[tuple[float, float, int, float]], start_side: str):
    """Feed (bid, ask, volume_delta, seconds) through a fresh Level.

    Returns (fired_side_or_None, last_snapshot, detail).
    """
    lv = Level(LEVEL)
    lv.side = start_side
    vol, t = 1_000_000, 1_000_000.0
    fired, detail, snap = None, None, None
    for bid, ask, dv, dt in steps:
        vol += dv
        t += dt
        snap = Snapshot(
            epoch=t, last=(bid + ask) / 2, bid=bid, ask=ask,
            bid_size=40, ask_size=40, volume=vol,
            spread_bps=(ask - bid) / ((ask + bid) / 2) * 10000,
            pct=((bid + ask) / 2 - 1625.78) / 1625.78 * 100,
            hi=1645.89, lo=1597.70, adv=15_764_110.0,
        )
        ev, msg = step_level(lv, snap, CONFIRM, MIN_PACE, GRACE, COOLDOWN, t)
        if ev and not fired:
            fired, detail = ev, msg
    return fired, snap, detail


# (name, steps, start_side, expected)  -- steps are (bid, ask, vol_delta, dt_s)
CASES: list[tuple[str, list, str, str | None]] = [
    (
        "5-share print through the level, book still above  (the 05:12 fakeout)",
        [(1630.0, 1631.0, 3000, 5)] * 6,
        "above",
        None,
    ),
    (
        "wide book straddling the level, last below         (the 29bp stretch)",
        [(1623.0, 1628.0, 3000, 5)] * 6,
        "above",
        None,
    ),
    (
        "real break down, 970 shares in 10s = 5,804/min     (the 05:28 MISS)",
        [(1624.0, 1624.84, 421, 5), (1624.6, 1625.0, 549, 5), (1624.6, 1625.0, 600, 5)],
        "above",
        "below",
    ),
    (
        "slow drift below, 642 shares in 10s = 3,852/min    (must stay silent)",
        [(1624.0, 1625.0, 300, 5), (1624.0, 1625.0, 342, 5), (1624.0, 1625.0, 200, 5)],
        "above",
        None,
    ),
    (
        "violent break up, 5,420 shares in 10s = 32,569/min (the 05:04 lift)",
        [(1630.0, 1631.0, 2700, 5), (1630.0, 1631.0, 2720, 5), (1630.0, 1631.0, 900, 5)],
        "below",
        "above",
    ),
    (
        "straddle flicker mid-cross must NOT wipe pending   (v1 bug #2)",
        [(1620.0, 1621.0, 2500, 5), (1623.0, 1628.0, 2600, 5),
         (1620.0, 1621.0, 2600, 5), (1620.0, 1621.0, 2600, 5)],
        "above",
        "below",
    ),
]


def main() -> int:
    print(c("alert.py self-test - synthetic data only, live watcher untouched", CYAN))
    print(c(f"level {LEVEL:,.2f}  confirm={CONFIRM} snapshots  "
            f"min-pace={MIN_PACE:,.0f}/min  grace={GRACE}", GREY))
    print()

    failures = 0
    for name, steps, start, expected in CASES:
        got, _, detail = drive(steps, start)
        ok = got == expected
        failures += not ok
        tag = c(" PASS ", GREEN) if ok else c(" FAIL ", RED)
        want = expected or "silence"
        gotd = got or "silence"
        print(f"  [{tag}] {name}")
        print(c(f"           expected {want:8}  got {gotd:8}"
                + (f"  ({detail})" if detail else ""), GREY))

    print()
    if failures:
        print(c(f"{failures} CASE(S) FAILED - do not trust the alerter", RED + ";1"))
        return 1

    print(c("all cases passed", GREEN + ";1"))
    print()
    print(c("now firing one real alert so you can see and hear it:", CYAN))
    time.sleep(1.0)

    _, snap, detail = drive(CASES[2][1], "above")
    print(fire_banner(LEVEL, "below", snap, detail or ""))
    beep("down")
    print()
    print(c("that banner and that sound are what a live break looks like.", GREY))
    print(c("if you heard nothing, your terminal has no audio - use the banner.", GREY))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
