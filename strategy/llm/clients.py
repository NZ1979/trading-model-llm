"""Backend clients for the tiered LLM evaluation.

Two concrete clients implement the ``LLMClient`` protocol:

- ``AnthropicClient``: calls ``api.anthropic.com`` via the async
  ``anthropic`` SDK. Used for Tier 2 (Sonnet escalations), Tier 3
  (Opus audit and M2 replay labeling), and the "haiku_stand_in"
  Tier 1 backend during the pre-workstation bridge period.
- ``LocalClient``: calls LM Studio's OpenAI-compatible endpoint at
  ``localhost:1234/v1`` via the ``openai`` SDK. Becomes the live Tier
  1 backend once the workstation is online with Qwen 72B loaded.

Both share the same input/output shape: take an ``LLMContext``, return
an ``LLMDecision``. The signal engine doesn't know which it is talking
to — that is the whole point of the protocol.

Output schema is enforced by Anthropic tool-use: the LLM is forced to
respond via the ``submit_decision`` tool, whose ``input_schema`` is
generated from the ``LLMDecision`` Pydantic model with platform-side
metadata fields (``tier_provenance``, ``raw_response``) stripped.

Retry policy: transient errors (connection, timeout, 5xx, rate-limit)
get up to 3 attempts with exponential backoff (1s/2s/4s, capped at
8s). Anything still failing after retries surfaces as
``APIUnavailableError``. 4xx programmer errors do not retry; they map
to ``SchemaInvalidError`` since they usually mean the request body
was malformed.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import anthropic
from anthropic import AsyncAnthropic
from pydantic import ValidationError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .prompts import render_messages
from .types import LLMContext, LLMDecision

logger = logging.getLogger(__name__)


# ============================================================================
# Protocol + typed exceptions
# ============================================================================


class LLMClient(Protocol):
    """Protocol any tier backend must implement."""

    model_id: str
    backend: str

    async def evaluate(self, ctx: LLMContext) -> LLMDecision: ...


class LLMClientError(Exception):
    """Base class for typed client errors."""


class APIUnavailableError(LLMClientError):
    """Backend returned a connection error, 5xx, or timed out (after retries)."""


class SchemaInvalidError(LLMClientError):
    """Backend returned a response that did not match LLMDecision."""


class BudgetExhaustedError(LLMClientError):
    """Daily spend tracker reports the configured cap is exceeded."""


# ============================================================================
# Schema for tool-use
# ============================================================================

# Fields the LLM must NOT fill in — those are signal_engine metadata,
# stripped from the tool input_schema we send to Anthropic.
_LLM_OUTPUT_ONLY_FIELDS = ("tier_provenance", "raw_response")

_TOOL_NAME = "submit_decision"
_TOOL_DESCRIPTION = (
    "Submit your trading decision in the required schema. "
    "Hold is always a safe default; only return Buy or Sell when you "
    "see a setup worth ~0.5% account risk."
)


def _llm_decision_tool_schema() -> dict[str, Any]:
    """Build the JSON schema for the submit_decision tool.

    Derived from ``LLMDecision.model_json_schema()`` with platform-side
    metadata fields stripped — the LLM is responsible only for the
    decision content, not for filling in ``tier_provenance`` or
    ``raw_response`` (the signal engine sets those).
    """
    schema = LLMDecision.model_json_schema()
    properties = dict(schema.get("properties", {}))
    for field in _LLM_OUTPUT_ONLY_FIELDS:
        properties.pop(field, None)
    schema["properties"] = properties

    required = list(schema.get("required", []))
    schema["required"] = [r for r in required if r not in _LLM_OUTPUT_ONLY_FIELDS]
    return schema


# ============================================================================
# AnthropicClient
# ============================================================================

# Retry on these exception types — transient network/server errors
# that usually succeed on a second attempt. Do NOT retry on 4xx
# programmer errors (those need a code fix, not a retry).
_RETRYABLE_EXCEPTIONS = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
)


class AnthropicClient:
    """Calls Anthropic's API. Used by Tier 2, Tier 3, and Tier-1-fallback
    (when LM Studio is offline), plus Tier-1-stand-in during the
    pre-workstation bridge period (model=claude-haiku-4-5).

    Construction is cheap — the SDK client is lazy. Repeated
    ``evaluate`` calls reuse the same connection pool.
    """

    backend = "anthropic"

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 1024,
        timeout_s: float = 30.0,
        api_key: str | None = None,
    ) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

        # Strip whitespace from the key. A pasted key with a trailing
        # newline or a leading space crashes deep inside httpcore/h11
        # with an opaque "Illegal header value" message that surfaces
        # as APIConnectionError after 3 retries — minutes wasted
        # debugging what looks like a network problem. Defang it here.
        # SDK reads ANTHROPIC_API_KEY from env if api_key is None;
        # construction without a key is allowed so unit tests can mock
        # the client.
        raw_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        clean_key = raw_key.strip() if raw_key else None
        self._client = AsyncAnthropic(
            api_key=clean_key,
            timeout=timeout_s,
        )
        self._tool_schema = _llm_decision_tool_schema()

    async def evaluate(self, ctx: LLMContext) -> LLMDecision:
        """Send the rendered prompt to Anthropic and parse the response.

        Raises:
            APIUnavailableError: connection refused, timeout, 5xx,
                rate-limit (after retries).
            SchemaInvalidError: response did not contain a valid
                ``submit_decision`` tool_use block, or the tool input
                failed Pydantic validation, or a 4xx surfaced.
        """
        rendered = render_messages(ctx)

        try:
            response = await self._call_with_retry(rendered)
        except RetryError as exc:
            inner = exc.last_attempt.exception()
            raise APIUnavailableError(
                f"{type(inner).__name__}: {inner}"
            ) from inner
        except anthropic.APIStatusError as exc:
            # 4xx that wasn't rate-limited. Surface as schema-ish error
            # since it usually means we sent something malformed.
            raise SchemaInvalidError(
                f"Anthropic {exc.status_code}: {exc}"
            ) from exc

        try:
            tool_block = next(
                b for b in response.content
                if getattr(b, "type", None) == "tool_use"
                and getattr(b, "name", None) == _TOOL_NAME
            )
        except StopIteration:
            block_types = [getattr(b, "type", None) for b in response.content]
            raise SchemaInvalidError(
                f"no '{_TOOL_NAME}' tool_use block in response; "
                f"got blocks: {block_types}"
            )

        try:
            decision = LLMDecision(
                **tool_block.input,
                raw_response={
                    "model": self.model_id,
                    "stop_reason": getattr(response, "stop_reason", None),
                    "input_tokens": getattr(response.usage, "input_tokens", None),
                    "output_tokens": getattr(response.usage, "output_tokens", None),
                    "cache_read_input_tokens": getattr(
                        response.usage, "cache_read_input_tokens", None
                    ),
                    "cache_creation_input_tokens": getattr(
                        response.usage, "cache_creation_input_tokens", None
                    ),
                },
            )
        except ValidationError as exc:
            raise SchemaInvalidError(
                f"LLMDecision validation failed: {exc}"
            ) from exc

        return decision

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        reraise=False,
    )
    async def _call_with_retry(self, rendered: dict[str, Any]) -> Any:
        # ``rendered`` carries 'system' (with breakpoint 1) and 'messages'
        # (with breakpoint 2 on market context). Tools and tool_choice
        # are owned by the client since they're tied to the LLMDecision
        # schema, not the prompt content.
        return await self._client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            tools=[
                {
                    "name": _TOOL_NAME,
                    "description": _TOOL_DESCRIPTION,
                    "input_schema": self._tool_schema,
                }
            ],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            **rendered,
        )


# ============================================================================
# LocalClient (placeholder until workstation arrives)
# ============================================================================


class LocalClient:
    """Calls LM Studio's OpenAI-compatible endpoint. Tier 1 once the
    workstation is online.

    Placeholder until the workstation arrives. Constructing this on
    the laptop deliberately raises ``NotImplementedError`` so
    accidental use in dev is loud.
    """

    backend = "lm_studio_local"

    def __init__(
        self,
        model_id: str,
        base_url: str = "http://localhost:1234/v1",
    ) -> None:
        self.model_id = model_id
        self.base_url = base_url
        raise NotImplementedError(
            "LocalClient is a workstation-only backend; "
            "use AnthropicClient with model='claude-haiku-4-5' as a "
            "Tier 1 stand-in until LM Studio is reachable. "
            "Implementation lands when hardware is online."
        )

    async def evaluate(self, ctx: LLMContext) -> LLMDecision:
        raise NotImplementedError("LocalClient.evaluate — workstation only")
