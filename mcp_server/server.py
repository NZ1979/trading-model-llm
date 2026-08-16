"""On-demand market data MCP server — skeleton.

Exposes read-only tools to Claude so a stock or option chain can be looked at
when asked. Nothing here watches, alerts, polls, or trades. The model is
turn-based; this server answers when called and is otherwise idle.

Scope, per the 2026-08-15 correction
------------------------------------
This is the component `docs/FEED_SPEC_V4.md` §7 describes and §11 qualifies
("I'm turn-based ... I cannot watch, alert, or trigger on a condition"). It is
NOT the tick daemon. `data/feed_daemon.py` and `data/tick_store.py` remain on
disk, dormant, and nothing here imports them.

This file is deliberately the SKELETON: `get_health` only. The transport is
unproven until Claude Desktop has actually launched this and called it, and
writing five tools against an unproven transport is how the last two sessions
went wrong. Add tools after the loop closes, not before.

Two hard constraints for any stdio MCP server
---------------------------------------------
1. **NOTHING MAY WRITE TO STDOUT.** stdout is the JSON-RPC channel. A stray
   `print()`, a library logging to stdout, or a warning banner corrupts the
   protocol stream and the client disconnects with a parse error that names
   neither the writer nor the line. All logging here is pinned to stderr, and
   any dependency added later must be checked for the same.
2. **`get_health` must never raise.** It is the tool called first to decide
   whether the rest can be trusted. A health check that crashes when
   credentials are missing reports nothing at the exact moment its answer
   matters most. Every probe below is individually guarded and degrades to a
   status string.

Package name
------------
This package is `mcp_server`, NOT `mcp`. A local `mcp/` directory containing
`__init__.py` shadows the installed SDK completely — verified 2026-08-16, at
which point `from mcp.server import MCPServer` fails from inside this very
file. `FEED_SPEC_V4` §7 says `mcp/server.py`; it is wrong.

SDK version
-----------
Written against `mcp` **2.0.0**, which removed `FastMCP` in favour of
`MCPServer`. Code written against 1.x examples (`from mcp.server import
FastMCP`) raises ImportError here.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Repo root on sys.path BEFORE any first-party import, so the server works
# regardless of the cwd Claude Desktop launches it with. Do not rely on the
# client setting a working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server import MCPServer  # noqa: E402

# stderr ONLY. See constraint 1 above.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_server")

ET = ZoneInfo("America/New_York")

SERVER_NAME = "llm-model-market-data"
SERVER_VERSION = "0.1.0"

server = MCPServer(
    name=SERVER_NAME,
    title="LLM Model market data",
    version=SERVER_VERSION,
    instructions=(
        "Read-only, on-demand market data for equities and options. "
        "ALWAYS call get_health first. If it reports a degraded or expired "
        "credential, say so rather than analysing data that may be stale or "
        "absent. This server has no order-entry capability of any kind."
    ),
)


def _probe(name: str, fn) -> Any:
    """Run one health probe, converting any failure into a reported status.

    Rule 18: a probe that fails must say so loudly in its own field rather
    than taking down the whole health response. A get_health that raises is
    strictly worse than one reporting 'schwab: ERROR ...', because the caller
    then has no information at all.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - deliberate: never propagate
        logger.exception("health probe %r failed", name)
        return {"status": "ERROR", "detail": f"{type(exc).__name__}: {exc}"}


def _schwab_health() -> dict:
    from data.schwab_auth import health as schwab_auth_health

    return schwab_auth_health()


def _alpaca_health() -> dict:
    """Presence only. Never the values, never a prefix, never a length.

    A key length or first-four-characters is still a credential disclosure in
    a transcript that gets pasted around.
    """
    return {
        "api_key_present": bool(os.environ.get("ALPACA_API_KEY")),
        "api_secret_present": bool(os.environ.get("ALPACA_API_SECRET")),
    }


def _chain_store_health() -> dict:
    from data.chain_store import ChainStore

    db_dir = _REPO_ROOT / "data" / "chains"
    if not (db_dir / "chains.db").exists():
        return {
            "status": "NO_DATA",
            "detail": "No chain snapshots recorded yet. Day-over-day open "
                      "interest change needs at least two stored sessions.",
        }
    with ChainStore(str(db_dir)) as store:
        stats = store.stats()
        stats["underlying_symbols"] = store.underlyings()
    stats["status"] = "OK"
    return stats


def _clock() -> dict:
    """Both clocks, always, from one measurement.

    The server reports time rather than letting the caller assume it. Three
    incorrect time claims were made on 2026-08-15 by extrapolating from a
    stale reading instead of taking a fresh one.
    """
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    return {
        "utc": now_utc.isoformat(timespec="seconds"),
        "eastern": now_et.isoformat(timespec="seconds"),
        "eastern_weekday": now_et.strftime("%A"),
    }


@server.tool(
    description=(
        "Report whether this server can answer questions right now: "
        "credential state for each vendor, what market data is available, "
        "and the current time in UTC and US/Eastern. Call this FIRST, before "
        "any other tool. If a vendor reports anything other than OK, say so "
        "in your answer rather than presenting its data as current."
    ),
)
def get_health() -> dict:
    """Health and readiness of every data source this server can reach."""
    return {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "python": sys.version.split()[0],
        "repo_root": str(_REPO_ROOT),
        "clock": _probe("clock", _clock),
        "schwab": _probe("schwab", _schwab_health),
        "alpaca": _probe("alpaca", _alpaca_health),
        "chain_store": _probe("chain_store", _chain_store_health),
        "tools_available": ["get_health"],
        "note": (
            "Skeleton build. get_snapshot, get_bars, get_option_chain and "
            "get_oi_change are not implemented yet."
        ),
    }


def main() -> None:
    logger.info("%s %s starting on stdio (repo root: %s)",
                SERVER_NAME, SERVER_VERSION, _REPO_ROOT)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
