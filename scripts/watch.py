"""Keep ONE symbol's live snapshot fresh on disk.

    python -m scripts.watch SNDK
    python -m scripts.watch SPCX --interval 3

Polls Schwab /quotes and Alpaca's snapshot every --interval seconds and
atomically rewrites data/live/<SYMBOL>.json. Claude reads that file directly
through the Cowork device bridge, so the round trip disappears: you ask a
question, Claude answers from data a few seconds old instead of asking you to
run a command and paste the output.

WHAT THIS IS NOT
----------------
This is NOT data/feed_daemon.py and must not grow into it. feed_daemon is the
dormant websocket tick-corpus component of the OTHER project sharing this
directory -- see CURRENT_SCOPE.md, "What this project is NOT". This file:

  * polls REST, does not stream
  * holds ONE symbol
  * writes ONE JSON file: no SQLite, no tick storage, no corpus
  * keeps a short IN-MEMORY series only so a reader sees the recent
    trajectory in a single read

If it ever seems to need a database, stop and re-read CURRENT_SCOPE.md.

NOTHING AUTONOMOUS. This process does not decide, alert, or trade. It
refreshes a file. Claude still only speaks when you send a message --
CURRENT_SCOPE.md's "nothing autonomous, nothing watching" is unchanged by it.

Credentials are read exactly as every other script reads them and never leave
Godzilla. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data.alpaca_rest import AlpacaRESTClient  # noqa: E402

LIVE_DIR = REPO_ROOT / "data" / "live"
MT = ZoneInfo("America/Denver")
ET = ZoneInfo("America/New_York")

_OPEN = dtime(9, 30)
_CLOSE = dtime(16, 0)
_PRE = dtime(4, 0)
_POST = dtime(20, 0)


def market_state(now_et: datetime) -> str:
    """Coarse session label. ET drives it; Godzilla's local time never does."""
    if now_et.weekday() >= 5:
        return "WEEKEND"
    t = now_et.time()
    if _OPEN <= t < _CLOSE:
        return "REGULAR HOURS"
    if _PRE <= t < _OPEN:
        return "PRE-MARKET"
    if _CLOSE <= t < _POST:
        return "AFTER-HOURS"
    return "CLOSED"


def _plain(obj):
    """Dataclass -> dict, with datetimes stringified so json.dump succeeds."""
    if obj is None:
        return None
    d = asdict(obj) if is_dataclass(obj) else dict(obj)
    out = {}
    for k, v in d.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    # frozen dataclasses expose derived values as properties, which asdict
    # drops. Re-attach the ones a reader actually wants.
    for prop in ("mid", "spread", "spread_bps", "is_realtime", "is_stale",
                 "is_halted", "change_from_close_pct", "volume_vs_avg_full_day",
                 "gap_pct", "change_pct", "day_bar_matches_last",
                 "last_session", "day_bar_session"):
        if hasattr(obj, prop):
            try:
                out[prop] = getattr(obj, prop)
            except Exception:
                pass
    return out


def _derive(series: deque) -> dict:
    """Cheap summary of the in-memory series so a reader need not recompute.

    Every field is None when the window is not yet covered. A partially
    filled window reporting a number as though it were a full one is the
    same class of error as a partial session compared against a full-day
    average -- see schwab_quotes.volume_vs_avg_full_day.
    """
    if not series:
        return {}
    now_ms, last, vol = series[-1][0], series[-1][1], series[-1][2]
    span_s = (now_ms - series[0][0]) / 1000.0
    out = {"samples": len(series), "window_seconds": round(span_s, 1)}

    def at_or_before(ms):
        best = None
        for row in series:
            if row[0] <= ms:
                best = row
            else:
                break
        return best

    for label, secs in (("1m", 60), ("5m", 300), ("15m", 900)):
        if span_s < secs:
            out[f"change_{label}_pct"] = None
            out[f"volume_{label}"] = None
            continue
        ref = at_or_before(now_ms - secs * 1000)
        if not ref or not ref[1] or last is None:
            out[f"change_{label}_pct"] = None
            out[f"volume_{label}"] = None
            continue
        out[f"change_{label}_pct"] = round((last - ref[1]) / ref[1] * 100, 4)
        out[f"volume_{label}"] = (vol - ref[2]
                                  if vol is not None and ref[2] is not None
                                  else None)

    if out.get("volume_1m") is not None:
        out["shares_per_min_1m"] = out["volume_1m"]
    if out.get("volume_5m") is not None:
        out["shares_per_min_5m"] = round(out["volume_5m"] / 5.0)

    prices = [r[1] for r in series if r[1] is not None]
    if prices:
        out["watch_high"] = max(prices)
        out["watch_low"] = min(prices)
    return out


def _write_atomic(path: Path, payload: dict) -> None:
    """Write via temp + os.replace so a reader never sees a half file.

    os.replace is atomic on Windows as well as POSIX. Without it, Claude
    reading through the device bridge mid-write would get truncated JSON and
    report it as a data problem.
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


async def run(symbol: str, interval: float, history: int) -> int:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LIVE_DIR / f"{symbol}.json"
    series: deque = deque(maxlen=history)

    from data.schwab_auth import get_client, health
    from data.schwab_quotes import fetch_quote

    h = health()
    if h["auth_state"] not in ("OK", "WARN_EXPIRING"):
        print(f"FAILED: Schwab auth state is {h['auth_state']}. "
              f"{h.get('action_required', '')}", file=sys.stderr)
        return 2
    schwab = get_client()

    started = datetime.now(timezone.utc)
    polls = errors = 0
    last_error = None
    stop = False

    def _sigint(*_):
        nonlocal stop
        stop = True
    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    print(f"watching {symbol} every {interval}s -> {out_path}")
    print("Ctrl-C to stop. Claude reads the file directly; you do not need "
          "to run anything else.\n")

    async with AlpacaRESTClient.from_env() as alpaca:
        while not stop:
            t0 = time.monotonic()
            polls += 1
            sq = asnap = None
            try:
                sq = await asyncio.to_thread(fetch_quote, schwab, symbol)
            except Exception as e:
                errors += 1
                last_error = f"schwab: {type(e).__name__}: {e}"
            try:
                got = await alpaca.snapshots([symbol])
                asnap = got.get(symbol)
            except Exception as e:
                errors += 1
                last_error = f"alpaca: {type(e).__name__}: {e}"

            now = datetime.now(timezone.utc)
            last = (sq.last_price if sq else None) or (
                asnap.last_price if asnap else None)
            vol = sq.total_volume if sq else None
            bid = (sq.bid if sq else None) or (asnap.bid if asnap else None)
            ask = (sq.ask if sq else None) or (asnap.ask if asnap else None)
            if last is not None:
                series.append([int(now.timestamp() * 1000), last, vol,
                               bid, ask])

            now_mt, now_et = now.astimezone(MT), now.astimezone(ET)
            payload = {
                "symbol": symbol,
                "pid": os.getpid(),
                "interval_seconds": interval,
                "started_at_mt": started.astimezone(MT).isoformat(),
                "updated_at_mt": now_mt.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "updated_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "updated_at_epoch": now.timestamp(),
                "market_state": market_state(now_et),
                "polls": polls,
                "errors": errors,
                "last_error": last_error,
                "schwab": _plain(sq),
                "alpaca": _plain(asnap),
                "derived": _derive(series),
                # [epoch_ms, last, cumulative_volume, bid, ask]
                "series": list(series),
            }
            try:
                _write_atomic(out_path, payload)
            except Exception as e:
                errors += 1
                last_error = f"write: {type(e).__name__}: {e}"

            px = f"{last:,.2f}" if last is not None else "   n/a"
            line = (f"\r{now_mt.strftime('%H:%M:%S')} {symbol} {px}  "
                    f"polls {polls}  errors {errors}   ")
            sys.stdout.write(line)
            if polls % 60 == 0:
                sys.stdout.write("\n")
            sys.stdout.flush()

            await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))

    print(f"\nstopped after {polls} polls, {errors} errors")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", nargs="?", default="SNDK")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between polls. Schwab allows 120 req/min; "
                         "5s is 12/min per endpoint.")
    ap.add_argument("--history", type=int, default=1440,
                    help="samples kept in memory (1440 = 2h at 5s)")
    a = ap.parse_args()
    if a.interval < 1.0:
        print("FAILED: --interval below 1s serves no purpose and risks a "
              "rate limit.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(a.symbol.upper(), a.interval, a.history))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
