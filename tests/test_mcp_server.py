"""Tests for the on-demand market data MCP server skeleton.

Covers the properties that decide whether this thing is safe to point Claude
Desktop at:

  1. get_health NEVER raises, however broken the environment is. It is the
     tool called first to decide whether anything else can be trusted; one
     that crashes on missing credentials reports nothing at the exact moment
     its answer matters most.
  2. No credential value, prefix, or length ever appears in a response.
  3. Nothing writes to stdout. stdout is the JSON-RPC channel and a single
     stray print corrupts the stream. The end-to-end test is the real check:
     if anything polluted stdout, the initialize handshake would not parse.
  4. The stdio transport actually works, subprocess and all.

No network. No credentials required - the tests run in an environment with
none, which is precisely the degraded case worth asserting on.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp_server.server import (
    SERVER_NAME,
    SERVER_VERSION,
    _alpaca_health,
    _clock,
    _probe,
    get_health,
    server,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ probe guarding

def test_probe_contains_failure_instead_of_raising():
    """Rule 18: a failed probe reports in its own field. A get_health that
    raises leaves the caller with no information at all, which is strictly
    worse than one field reading ERROR."""
    def boom():
        raise RuntimeError("vendor exploded")

    got = _probe("boom", boom)
    assert got["status"] == "ERROR"
    assert "vendor exploded" in got["detail"]
    assert "RuntimeError" in got["detail"]


def test_get_health_never_raises_with_empty_environment(monkeypatch):
    """The whole point of the tool. Strip every credential and it must still
    answer."""
    for key in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "SCHWAB_API_KEY",
                "SCHWAB_APP_SECRET", "SCHWAB_TOKEN_PATH"):
        monkeypatch.delenv(key, raising=False)

    health = get_health()
    assert isinstance(health, dict)
    assert health["server"]["name"] == SERVER_NAME


def test_get_health_reports_every_source(monkeypatch):
    health = get_health()
    for key in ("server", "python", "clock", "schwab", "alpaca",
                "chain_store", "tools_available"):
        assert key in health, f"missing health field: {key}"


# --------------------------------------------------------- credential safety

def test_alpaca_health_reports_presence_not_values(monkeypatch):
    """A key prefix or length is still a disclosure in a transcript that gets
    pasted into a chat window."""
    monkeypatch.setenv("ALPACA_API_KEY", "PKSECRETKEY1234567890")
    monkeypatch.setenv("ALPACA_API_SECRET", "supersecretvalue")

    got = _alpaca_health()
    assert got == {"api_key_present": True, "api_secret_present": True}

    blob = json.dumps(got)
    assert "PKSECRET" not in blob
    assert "supersecret" not in blob


def test_full_health_response_leaks_no_credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKLEAKCANARY99")
    monkeypatch.setenv("ALPACA_API_SECRET", "SECRETLEAKCANARY99")

    blob = json.dumps(get_health(), default=str)
    assert "LEAKCANARY" not in blob


def test_alpaca_health_false_when_absent(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert _alpaca_health() == {
        "api_key_present": False, "api_secret_present": False}


# ------------------------------------------------------------------- clock

def test_clock_reports_both_utc_and_eastern():
    """The server reports time rather than letting the caller assume it.
    Three incorrect time claims were made on 2026-08-15 by extrapolating
    from a stale reading instead of taking a fresh one."""
    got = _clock()
    assert got["utc"].endswith("+00:00")
    assert got["eastern"].endswith(("-04:00", "-05:00"))  # EDT or EST
    assert got["eastern_weekday"] in {
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday"}


# -------------------------------------------------------------- registration

def test_get_health_is_registered_as_a_tool():
    tools = asyncio.run(server.list_tools())
    assert [t.name for t in tools] == ["get_health"]


def test_tool_description_tells_the_caller_to_call_it_first():
    """The description is the only instruction the model reliably sees."""
    tools = asyncio.run(server.list_tools())
    assert "FIRST" in tools[0].description


def test_server_advertises_no_order_entry():
    """This server is read-only. The instructions must say so, because the
    model's behaviour is shaped by them and there is no order-entry code to
    defend if it never gets asked for."""
    assert "no order-entry" in (server.instructions or "").lower()


# ------------------------------------------------- end to end over real stdio

def test_stdio_transport_end_to_end():
    """Launch the server as a subprocess and speak actual JSON-RPC to it.

    This is also the stdout-pollution test. If any import in the server
    process writes to stdout - a print, a banner, a library logging to the
    wrong stream - the initialize handshake fails to parse and this test
    fails with a protocol error rather than an assertion.
    """
    from mcp import ClientSession, StdioServerParameters, stdio_client

    async def run() -> dict:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == SERVER_NAME
                assert init.server_info.version == SERVER_VERSION

                tools = await session.list_tools()
                assert [t.name for t in tools.tools] == ["get_health"]

                res = await session.call_tool("get_health", {})
                assert not res.is_error
                return res.structured_content or json.loads(
                    res.content[0].text)

    payload = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert payload["server"]["name"] == SERVER_NAME
    assert "clock" in payload and "utc" in payload["clock"]
    assert payload["tools_available"] == ["get_health"]


def test_server_module_is_not_named_mcp():
    """A local package named `mcp` shadows the installed SDK completely -
    verified 2026-08-16, at which point `from mcp.server import MCPServer`
    fails from inside the server itself. FEED_SPEC_V4 §7 says `mcp/server.py`
    and is wrong. This test fails loudly if someone renames it back."""
    assert not (REPO_ROOT / "mcp" / "__init__.py").exists(), (
        "A local mcp/ package shadows the installed MCP SDK. "
        "Use mcp_server/ instead."
    )
