"""Verification script for AnthropicClient (step 3 plumbing test).

Exercises every code path in AnthropicClient with mocked SDK responses.
No real API calls; safe to run anywhere. Run with:

    python scripts/verify_anthropic_client.py

Exits 0 on all assertions passing, 1 on any failure.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic

from strategy.llm.clients import (
    APIUnavailableError,
    AnthropicClient,
    SchemaInvalidError,
    _llm_decision_tool_schema,
    _LLM_OUTPUT_ONLY_FIELDS,
    _TOOL_NAME,
)
from strategy.llm.types import LLMContext, LLMDecision


def _make_ctx() -> LLMContext:
    return LLMContext(
        ticker="AAPL",
        timestamp_et="2026-05-08 09:42:00 ET",
        prompt_version="v0.0-stub",
    )


def _make_mock_response(tool_input: dict, *, with_usage: bool = True) -> SimpleNamespace:
    """Build a mock that mimics anthropic.types.Message shape."""
    tool_block = SimpleNamespace(
        type="tool_use",
        name=_TOOL_NAME,
        id="toolu_test",
        input=tool_input,
    )
    usage = SimpleNamespace(
        input_tokens=1500,
        output_tokens=120,
        cache_read_input_tokens=1200,
        cache_creation_input_tokens=0,
    ) if with_usage else SimpleNamespace()
    return SimpleNamespace(
        content=[tool_block],
        stop_reason="tool_use",
        usage=usage,
    )


def _make_mock_response_no_tool() -> SimpleNamespace:
    text_block = SimpleNamespace(type="text", text="I refuse to use the tool.")
    return SimpleNamespace(
        content=[text_block],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )


def _patch_client(client: AnthropicClient, mock_create: AsyncMock) -> None:
    """Replace the SDK's messages.create with a mock."""
    client._client = MagicMock()
    client._client.messages = MagicMock()
    client._client.messages.create = mock_create


def _print(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


async def main() -> int:
    all_ok = True
    ctx = _make_ctx()

    # ---- Static check 1: tool schema strips metadata fields ----
    schema = _llm_decision_tool_schema()
    props = schema["properties"]
    metadata_present = [f for f in _LLM_OUTPUT_ONLY_FIELDS if f in props]
    all_ok &= _print(
        "tool schema strips metadata fields",
        not metadata_present,
        f"properties has {len(props)} fields; metadata still present: {metadata_present}",
    )
    # Action enum should still be present
    all_ok &= _print(
        "tool schema retains action enum",
        "action" in props and "Buy" in str(props["action"]),
    )

    # ---- Static check 2: client construction without API key ----
    client = AnthropicClient(model_id="claude-haiku-4-5", api_key="sk-test-fake")
    all_ok &= _print(
        "AnthropicClient constructs without real API key",
        client.model_id == "claude-haiku-4-5" and client.backend == "anthropic",
    )

    # ---- Path 1: happy path — valid tool_use response ----
    happy_input = {
        "action": "Hold",
        "confidence": 0,
        "setup_label": "plumbing_stub",
        "reasoning": "step 3 plumbing test",
        "stop_loss_atr_multiple": 1.5,
        "take_profit_atr_multiple": 2.0,
        "time_horizon": "intraday",
        "concerns": [],
        "alternative_view": "",
    }
    mock_create = AsyncMock(return_value=_make_mock_response(happy_input))
    _patch_client(client, mock_create)
    decision = await client.evaluate(ctx)
    all_ok &= _print(
        "happy path returns LLMDecision",
        isinstance(decision, LLMDecision)
        and decision.action == "Hold"
        and decision.confidence == 0,
        f"action={decision.action} conf={decision.confidence} setup={decision.setup_label}",
    )
    all_ok &= _print(
        "happy path captures cache token metadata in raw_response",
        decision.raw_response is not None
        and decision.raw_response.get("cache_read_input_tokens") == 1200,
        f"raw={decision.raw_response}",
    )
    all_ok &= _print(
        "happy path called create exactly once",
        mock_create.call_count == 1,
        f"call_count={mock_create.call_count}",
    )
    # Inspect the call args to verify tool_use shape
    call_kwargs = mock_create.call_args.kwargs
    all_ok &= _print(
        "create called with model and tool_choice forced",
        call_kwargs["model"] == "claude-haiku-4-5"
        and call_kwargs["tool_choice"] == {"type": "tool", "name": _TOOL_NAME}
        and call_kwargs["tools"][0]["name"] == _TOOL_NAME,
    )
    all_ok &= _print(
        "system block has cache_control ephemeral",
        call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"},
    )

    # ---- Path 2: schema-invalid tool input — confidence out of range ----
    bad_input = {
        "action": "Buy",
        "confidence": 999,  # rejected by Pydantic ge=0, le=100
        "setup_label": "x",
        "reasoning": "y",
        "stop_loss_atr_multiple": 1.5,
        "take_profit_atr_multiple": 2.0,
    }
    mock_create = AsyncMock(return_value=_make_mock_response(bad_input))
    _patch_client(client, mock_create)
    try:
        await client.evaluate(ctx)
        all_ok &= _print("schema-invalid raises SchemaInvalidError", False, "no exception")
    except SchemaInvalidError as exc:
        all_ok &= _print(
            "schema-invalid raises SchemaInvalidError",
            "validation failed" in str(exc).lower(),
            f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    except Exception as exc:
        all_ok &= _print(
            "schema-invalid raises SchemaInvalidError",
            False,
            f"wrong exception type: {type(exc).__name__}",
        )

    # ---- Path 3: no tool_use block in response ----
    mock_create = AsyncMock(return_value=_make_mock_response_no_tool())
    _patch_client(client, mock_create)
    try:
        await client.evaluate(ctx)
        all_ok &= _print("no-tool-block raises SchemaInvalidError", False, "no exception")
    except SchemaInvalidError as exc:
        all_ok &= _print(
            "no-tool-block raises SchemaInvalidError",
            "tool_use" in str(exc),
            f"{type(exc).__name__}: {str(exc)[:80]}",
        )

    # ---- Path 4: connection error retried then surfaces APIUnavailableError ----
    conn_err = anthropic.APIConnectionError(request=MagicMock())
    mock_create = AsyncMock(side_effect=conn_err)
    _patch_client(client, mock_create)
    try:
        await client.evaluate(ctx)
        all_ok &= _print("connection error -> APIUnavailableError", False, "no exception")
    except APIUnavailableError as exc:
        all_ok &= _print(
            "connection error -> APIUnavailableError after 3 retries",
            mock_create.call_count == 3,
            f"call_count={mock_create.call_count}, message={str(exc)[:80]}",
        )
    except Exception as exc:
        all_ok &= _print(
            "connection error -> APIUnavailableError",
            False,
            f"wrong exception type: {type(exc).__name__}: {exc}",
        )

    # ---- Path 5: 4xx error surfaces as SchemaInvalidError without retry ----
    status_err = anthropic.BadRequestError(
        message="bad input",
        response=MagicMock(status_code=400),
        body=None,
    )
    mock_create = AsyncMock(side_effect=status_err)
    _patch_client(client, mock_create)
    try:
        await client.evaluate(ctx)
        all_ok &= _print("400 -> SchemaInvalidError no retry", False, "no exception")
    except SchemaInvalidError as exc:
        all_ok &= _print(
            "400 -> SchemaInvalidError no retry",
            mock_create.call_count == 1,
            f"call_count={mock_create.call_count}, message={str(exc)[:80]}",
        )
    except Exception as exc:
        all_ok &= _print(
            "400 -> SchemaInvalidError",
            False,
            f"wrong exception type: {type(exc).__name__}: {exc}",
        )

    # ---- Path 6: transient error then success on retry ----
    success_resp = _make_mock_response(happy_input)
    transient = anthropic.InternalServerError(
        message="500",
        response=MagicMock(status_code=500),
        body=None,
    )
    mock_create = AsyncMock(side_effect=[transient, transient, success_resp])
    _patch_client(client, mock_create)
    try:
        decision = await client.evaluate(ctx)
        all_ok &= _print(
            "transient error retried then succeeds",
            decision.action == "Hold" and mock_create.call_count == 3,
            f"call_count={mock_create.call_count}",
        )
    except Exception as exc:
        all_ok &= _print(
            "transient error retried then succeeds",
            False,
            f"unexpected exception: {type(exc).__name__}: {exc}",
        )

    print()
    print("ALL OK" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
