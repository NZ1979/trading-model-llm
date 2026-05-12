"""Backend clients for the tiered LLM evaluation.

Two concrete clients implement the ``LLMClient`` protocol:

- ``AnthropicClient``: calls ``api.anthropic.com`` via the async
  ``anthropic`` SDK. Used for Tier 2 (Sonnet escalations), Tier 3
  (Opus audit and M2 replay labeling), and the "haiku_stand_in"
  Tier 1 backend during the pre-workstation bridge period.
- ``LocalClient``: calls LM Studio's OpenAI-compatible endpoint at
  ``localhost:1234/v1`` via the ``openai`` SDK. Becomes the live Tier
  1 backend once the workstation is online with Qwen 3.6-27B loaded.

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

import json
import logging
import os
import re
from typing import Any, Protocol

import anthropic
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
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
# Helpers for the local (OpenAI-compatible) backend
# ============================================================================

_LOCAL_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
)


def _rendered_to_openai_messages(rendered: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate the Anthropic-style ``{system, messages}`` dict from
    ``prompts.render_messages`` into an OpenAI chat-completions
    ``messages`` list.

    Anthropic accepts ``system`` as a list of typed blocks (with
    ``cache_control``); OpenAI-compatible servers want a single
    ``role=system`` message with plain text content. Same idea for
    user messages: Anthropic uses content blocks; OpenAI wants flat text.

    LM Studio doesn't honor Anthropic's prompt-caching breakpoints, so we
    drop ``cache_control`` and concatenate text blocks. Qwen runs its own
    KV cache internally — caching efficiency is handled by the backend,
    not by client-side breakpoints.
    """
    msgs: list[dict[str, Any]] = []
    sys_blocks = rendered.get("system", [])
    if sys_blocks:
        sys_text = "\n\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in sys_blocks
        )
        msgs.append({"role": "system", "content": sys_text})
    for msg in rendered.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        else:
            text = ""
        msgs.append({"role": msg.get("role", "user"), "content": text})
    # Qwen 3 thinking-mode disable. Without /no_think, Qwen 3.6 emits a
    # <think>...</think> block, completes its reasoning, and frequently
    # stops without making the tool call (finish_reason=stop with empty
    # content + empty tool_calls). /no_think on the LAST user message
    # tells Qwen to skip the thinking phase.
    for m in reversed(msgs):
        if m.get("role") == "user":
            m["content"] = m["content"] + "\n\n/no_think"
            break
    return msgs


# Regex pieces for Qwen's native tool-call format. LM Studio's
# OpenAI-compat layer does not understand this format and dumps the
# raw text into ``message.reasoning_content`` while leaving
# ``message.tool_calls`` empty. We parse it ourselves as a fallback.
_QWEN_FUNCTION_RE = re.compile(
    r"<function=(?P<name>[\w\.\-]+)>(?P<body>.*?)</function>",
    re.DOTALL,
)
_QWEN_PARAMETER_RE = re.compile(
    r"<parameter=(?P<key>[\w\.\-]+)>\s*(?P<val>.*?)\s*</parameter>",
    re.DOTALL,
)


def _coerce_qwen_param(val: str) -> Any:
    """Coerce a Qwen XML-format parameter string to int/float/bool/str/list/dict.

    Qwen often emits list/object fields as JSON-encoded strings (e.g.
    ``["Gap fill risk", "Early volatility"]``). Try JSON decode first
    for anything that looks like a structured value; fall through to
    primitive coercion otherwise.
    """
    val = val.strip()
    # JSON-decode list/object-shaped values (Qwen emits these as strings)
    if val and val[0] in "[{":
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass  # fall through to primitives
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d*\.\d+", val):
        return float(val)
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    return val


def _parse_qwen_tool_call(text: str, expected_tool: str) -> dict[str, Any] | None:
    """Parse Qwen 3.6's native XML-style tool call from a text blob.

    Format (whitespace-tolerant):
        <tool_call>
          <function=submit_decision>
            <parameter=action>Buy</parameter>
            <parameter=confidence>75</parameter>
            ...
          </function>
        </tool_call>

    Returns the parsed argument dict if a matching tool_call is found,
    None otherwise. None signals "no tool call here" and the caller
    raises SchemaInvalidError with the diagnostic dump.
    """
    if not text:
        return None
    fn_match = _QWEN_FUNCTION_RE.search(text)
    if not fn_match:
        return None
    if fn_match.group("name") != expected_tool:
        return None
    body = fn_match.group("body")
    args: dict[str, Any] = {}
    for m in _QWEN_PARAMETER_RE.finditer(body):
        key = m.group("key")
        args[key] = _coerce_qwen_param(m.group("val"))
    return args if args else None


# ============================================================================
# LocalClient (LM Studio OpenAI-compatible backend)
# ============================================================================


class LocalClient:
    """Calls LM Studio's OpenAI-compatible endpoint. Tier 1 production
    backend once the workstation is online with Qwen 3.6-27B loaded.

    Construction is cheap; the openai SDK client is lazy. Repeated
    ``evaluate`` calls reuse the same connection pool.

    Notes on Qwen 3.6 thinking-mode behavior:
      - The model emits ``<think>...</think>`` reasoning before the tool
        call. LM Studio strips it from ``message.content`` but the
        thinking tokens still count against ``max_tokens``. Default
        max_tokens is 2048 (vs Anthropic's 1024) to leave headroom.
      - Tool-use is forced via ``tool_choice={"type": "function", ...}``
        which steers the model to emit a tool_call rather than prose.
      - ``temperature=0`` mirrors the Anthropic client for determinism.
    """

    backend = "lm_studio_local"

    def __init__(
        self,
        model_id: str,
        max_tokens: int = 4096,
        timeout_s: float = 60.0,
        base_url: str = "http://localhost:1234/v1",
        api_key: str = "lm-studio-no-auth",
    ) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.base_url = base_url
        # LM Studio doesn't authenticate but the openai SDK still
        # requires a non-empty api_key. Any string works.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
        )
        self._tool_schema = _llm_decision_tool_schema()

    async def evaluate(self, ctx: LLMContext) -> LLMDecision:
        """Send the rendered prompt to LM Studio and parse the tool_call.

        Raises:
            APIUnavailableError: connection refused, timeout, 5xx, or
                rate-limit (after retries).
            SchemaInvalidError: response had no submit_decision tool_call,
                arguments were not valid JSON, or the parsed args failed
                LLMDecision validation.
        """
        rendered = render_messages(ctx)
        oai_messages = _rendered_to_openai_messages(rendered)

        try:
            response = await self._call_with_retry(oai_messages)
        except RetryError as exc:
            inner = exc.last_attempt.exception()
            raise APIUnavailableError(
                f"{type(inner).__name__}: {inner}"
            ) from inner
        except openai.APIStatusError as exc:
            raise SchemaInvalidError(
                f"LM Studio {exc.status_code}: {exc}"
            ) from exc

        if not response.choices:
            raise SchemaInvalidError("LM Studio response had no choices")
        choice = response.choices[0]
        message = choice.message
        tool_calls = getattr(message, "tool_calls", None) or []
        target = next(
            (tc for tc in tool_calls
             if getattr(getattr(tc, "function", None), "name", None) == _TOOL_NAME),
            None,
        )
        # Fallback: LM Studio's tool-parser doesn't recognize Qwen 3.6's
        # native XML tool-call format. When that's the case, tool_calls
        # is empty and the raw call ends up in reasoning_content. Try to
        # parse it directly before declaring failure.
        if target is None:
            reasoning_text = getattr(message, "reasoning_content", None) or ""
            qwen_args = _parse_qwen_tool_call(reasoning_text, _TOOL_NAME)
            if qwen_args is None:
                # Sometimes the XML-style tool call lands in content
                # instead. Try there too.
                qwen_args = _parse_qwen_tool_call(message.content or "", _TOOL_NAME)
            if qwen_args is not None:
                args = qwen_args
                # Bypass the standard "args from tool_calls" path below.
                # Build the same LLMDecision and return early.
                usage = getattr(response, "usage", None)
                try:
                    decision = LLMDecision(
                        **args,
                        raw_response={
                            "model": self.model_id,
                            "backend": self.backend,
                            "finish_reason": choice.finish_reason,
                            "input_tokens": getattr(usage, "prompt_tokens", None),
                            "output_tokens": getattr(usage, "completion_tokens", None),
                            "tool_parse": "qwen_xml_fallback",
                        },
                    )
                except ValidationError as exc:
                    raise SchemaInvalidError(
                        f"Qwen XML tool_call parsed but LLMDecision "
                        f"validation failed: {exc}; args={args}"
                    ) from exc
                return decision

            # Genuine no-tool-call response. Build the rich diagnostic.
            got = [
                getattr(getattr(tc, "function", None), "name", None)
                for tc in tool_calls
            ]
            usage = getattr(response, "usage", None)
            out_tok = getattr(usage, "completion_tokens", None) if usage else None
            in_tok = getattr(usage, "prompt_tokens", None) if usage else None
            try:
                msg_dump = message.model_dump() if hasattr(message, "model_dump") else dict(message.__dict__)
            except Exception:
                msg_dump = {"__repr__": repr(message)[:400]}
            raise SchemaInvalidError(
                f"no '{_TOOL_NAME}' tool_call in response; "
                f"finish_reason={choice.finish_reason}; "
                f"got tool_calls={got}; "
                f"tokens=in:{in_tok}/out:{out_tok}/max:{self.max_tokens}; "
                f"message_dump={json.dumps(msg_dump, default=str)[:600]}"
            )

        try:
            args = json.loads(target.function.arguments)
        except json.JSONDecodeError as exc:
            raise SchemaInvalidError(
                f"tool_call.arguments was not valid JSON: {exc}; "
                f"raw={target.function.arguments[:200]!r}"
            ) from exc

        usage = getattr(response, "usage", None)
        try:
            decision = LLMDecision(
                **args,
                raw_response={
                    "model": self.model_id,
                    "backend": self.backend,
                    "finish_reason": choice.finish_reason,
                    "input_tokens": getattr(usage, "prompt_tokens", None),
                    "output_tokens": getattr(usage, "completion_tokens", None),
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
        retry=retry_if_exception_type(_LOCAL_RETRYABLE),
        reraise=False,
    )
    async def _call_with_retry(
        self, oai_messages: list[dict[str, Any]]
    ) -> Any:
        return await self._client.chat.completions.create(
            model=self.model_id,
            messages=oai_messages,
            max_tokens=self.max_tokens,
            temperature=0,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "description": _TOOL_DESCRIPTION,
                        "parameters": self._tool_schema,
                    },
                }
            ],
            tool_choice="required",  # LM Studio only accepts none|auto|required; with one tool defined, "required" forces it
        )
