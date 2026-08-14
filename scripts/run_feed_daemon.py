"""Run the feed daemon: live Alpaca SIP tape -> SQLite tick corpus.

REAL NETWORK CALL. Requires ALPACA_API_KEY and ALPACA_API_SECRET in the
environment. WRITES TO DISK under data/ticks/ (gitignored).

Run from C:\\trading\\LLM model with the venv active:

    python -m scripts.run_feed_daemon --symbols SNDK --seconds 60
    python -m scripts.run_feed_daemon --symbols SNDK,MU --seconds 300
    python -m scripts.run_feed_daemon --symbols SNDK           # until Ctrl-C

Only ONE instance may run at a time — Alpaca permits a single market-data
connection per account, and a second would produce a reconnect loop that looks
like a network fault. The lock file under data/ticks/ enforces this; if the
daemon is killed uncleanly, delete alpaca.lock before restarting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.feed_daemon import FeedDaemon  # noqa: E402

DEFAULT_SYMBOLS = "SNDK"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Rule 22: HTTP clients log full URLs at INFO by default. Nothing here
    # passes credentials in a URL, but the suppression stays so a future
    # dependency change cannot quietly start leaking.
    for noisy in ("websockets", "httpx", "httpcore", "aiohttp", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--seconds", type=float, default=None,
                    help="stop after N seconds; omit to run until Ctrl-C")
    ap.add_argument("--feed", default="sip", choices=("sip", "iex"))
    ap.add_argument("--db-dir", default=os.path.join("data", "ticks"))
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    setup_logging(args.log_level)

    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        print("FAILED: ALPACA_API_KEY / ALPACA_API_SECRET not set in the "
              "environment.", file=sys.stderr)
        return 2

    symbols = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    daemon = FeedDaemon(
        api_key=key, api_secret=secret, symbols=symbols,
        db_dir=args.db_dir, feed=args.feed,
        lock_path=os.path.join(args.db_dir, "alpaca.lock"),
    )

    print(f"Feed daemon starting: feed={args.feed} symbols={sorted(symbols)}")
    print(f"  corpus: {daemon.store.db_path}")
    print(f"  {'running ' + str(args.seconds) + 's' if args.seconds else 'running until Ctrl-C'}")

    await daemon.start()
    try:
        if args.seconds:
            await asyncio.sleep(args.seconds)
        else:
            while True:
                await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nstopping ...")
    finally:
        await daemon.stop()

    print()
    print(json.dumps(daemon.summary, indent=2))

    s = daemon.store.stats
    recv = sum(daemon._counts.values())
    wrote = s.trades + s.quotes + s.bars + s.statuses
    print()
    if s.dropped:
        print(f"WARNING: {s.dropped} rows DROPPED — the corpus is incomplete "
              f"for this window. {s.drop_reasons}")
        return 1
    if recv and wrote != recv:
        print(f"WARNING: received {recv} messages but persisted {wrote}. "
              f"These should match.")
        return 1
    print(f"OK: {recv} messages received, {wrote} persisted, 0 dropped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
