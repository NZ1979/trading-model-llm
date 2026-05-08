"""Isolate where the Anthropic connection fails.

Three layered tests:
  1. Sync Anthropic call (matches the production sentiment scorer's pattern).
     If this fails, the network/key/firewall is the problem.
  2. Async Anthropic call WITHOUT our retry/tool-use wrapper.
     If this fails but #1 worked, async path on Python 3.14 + Windows
     has an issue.
  3. Async Anthropic with tool-use (exact shape of our AnthropicClient).
     If this fails but #2 worked, our tool-use config is the issue.

Also prints relevant library versions and the asyncio event loop policy
in use, since httpx + asyncio on Windows has had historical quirks.

Run with:
    cd C:\\trading\\LLM model
    $env:ANTHROPIC_API_KEY = '<your-key>'
    python scripts/diagnose_anthropic_connection.py
"""
from __future__ import annotations

import asyncio
import os
import platform
import sys
import traceback


def _print_env() -> None:
    print("=" * 60)
    print("Environment")
    print("=" * 60)
    print(f"Python   : {sys.version.split()[0]}  ({platform.system()} {platform.release()})")
    try:
        import anthropic
        print(f"anthropic: {anthropic.__version__}")
    except Exception as e:
        print(f"anthropic import failed: {e}")
    try:
        import httpx
        print(f"httpx    : {httpx.__version__}")
    except Exception as e:
        print(f"httpx import failed: {e}")
    try:
        import httpcore
        print(f"httpcore : {httpcore.__version__}")
    except Exception:
        pass

    if platform.system() == "Windows":
        # Windows default policy on Python 3.8+ is ProactorEventLoop.
        # Some httpx versions had issues with it; SelectorEventLoop is
        # the historical workaround.
        try:
            policy = asyncio.get_event_loop_policy()
            print(f"asyncio  : {type(policy).__name__}")
        except Exception:
            pass
    print()


def _test1_sync() -> bool:
    """Sync Anthropic call. Mirrors the production sentiment scorer."""
    print("=" * 60)
    print("Test 1: SYNC Anthropic call (no async, no tool-use)")
    print("=" * 60)
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=50,
            messages=[{"role": "user", "content": "Reply with one word: pong"}],
        )
        text = resp.content[0].text if resp.content else "(empty)"
        print(f"  PASS — response: {text!r}")
        print(f"  usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return True
    except Exception:
        print(f"  FAIL — {traceback.format_exc().splitlines()[-1]}")
        traceback.print_exc()
        return False
    finally:
        print()


async def _test2_async_simple() -> bool:
    """Async Anthropic call without tool-use, without our retry wrapper."""
    print("=" * 60)
    print("Test 2: ASYNC Anthropic call (no tool-use, no retry wrapper)")
    print("=" * 60)
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=50,
            messages=[{"role": "user", "content": "Reply with one word: pong"}],
        )
        text = resp.content[0].text if resp.content else "(empty)"
        print(f"  PASS — response: {text!r}")
        print(f"  usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return True
    except Exception:
        print(f"  FAIL — {traceback.format_exc().splitlines()[-1]}")
        traceback.print_exc()
        return False
    finally:
        print()


async def _test3_async_tooluse() -> bool:
    """Async Anthropic with tool-use, mirroring our AnthropicClient call shape."""
    print("=" * 60)
    print("Test 3: ASYNC Anthropic + tool-use (matches AnthropicClient)")
    print("=" * 60)
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            tools=[
                {
                    "name": "submit_decision",
                    "description": "Submit a trivial decision.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["A", "B"]},
                        },
                        "required": ["action"],
                    },
                }
            ],
            tool_choice={"type": "tool", "name": "submit_decision"},
            messages=[{"role": "user", "content": "Pick A."}],
        )
        block = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if block is None:
            print(f"  FAIL — no tool_use block; got {[getattr(b, 'type', None) for b in resp.content]}")
            return False
        print(f"  PASS — tool input: {block.input}")
        return True
    except Exception:
        print(f"  FAIL — {traceback.format_exc().splitlines()[-1]}")
        traceback.print_exc()
        return False
    finally:
        print()


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set. Run:")
        print("    $env:ANTHROPIC_API_KEY = '<your-key>'")
        return 1

    _print_env()

    sync_ok = _test1_sync()
    async_simple_ok = await _test2_async_simple()
    async_tooluse_ok = await _test3_async_tooluse()

    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Test 1 (sync, no tools)             : {'PASS' if sync_ok else 'FAIL'}")
    print(f"  Test 2 (async, no tools)            : {'PASS' if async_simple_ok else 'FAIL'}")
    print(f"  Test 3 (async + tool-use)           : {'PASS' if async_tooluse_ok else 'FAIL'}")
    print()

    if not sync_ok:
        print("DIAGNOSIS: Network / firewall / API key issue. Production sentiment")
        print("scorer should be failing too. Check:")
        print("  - Outbound HTTPS to api.anthropic.com (port 443)")
        print("  - VPN or proxy interception")
        print("  - Key has expired or scope is restricted")
        return 1
    if not async_simple_ok:
        print("DIAGNOSIS: AsyncAnthropic / httpx-async path broken on this")
        print("Python+OS combo. Likely Python 3.14 + httpx + Windows event-loop")
        print("policy interaction. Workaround: use sync Anthropic in the client.")
        return 2
    if not async_tooluse_ok:
        print("DIAGNOSIS: Async base works, but tool-use call fails. Schema or")
        print("tool_choice shape mismatch. Inspect the traceback above.")
        return 3

    print("ALL PASS — connection from this shell is working. The earlier")
    print("smoke_test_haiku.py failure may have been transient. Try re-running it.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
