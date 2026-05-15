"""Tests for data/replay_cache.py (M2.2 sub-task #8).

Covers:
  - Cache miss path: writes envelope, returns inner's decision,
    increments misses
  - Cache hit path: reads envelope, returns deserialized decision
    equal to original, increments hits, does NOT call inner
  - Identity round-trip via Pydantic model_validate (cache hit
    decision == original)
  - Cache key invalidation: prompt_version bump, backend change,
    model_id change, LLMContext field change all produce cache
    misses
  - Canonical-JSON stability: two semantically identical
    LLMContexts (e.g. constructed in different field orders) hash
    to the same key
  - Soft-miss paths log WARNING + re-call:
      * file doesn't exist -> clean miss (no warning)
      * malformed JSON -> WARNING, miss, overwrite on next call
      * non-dict envelope root -> WARNING, miss
      * schema_version mismatch -> WARNING, miss
      * missing 'decision' field -> WARNING, miss
      * decision payload fails LLMDecision schema -> WARNING, miss
  - Write failure (simulated read-only dir / mocked OSError):
    WARNING, returns inner's result unchanged, cache file absent
  - Inner exceptions propagate uncached:
      * APIUnavailableError
      * SchemaInvalidError
      * BudgetExhaustedError
      * generic RuntimeError
  - model_id with slashes (HF-style "meta-llama/Llama-3.3-70B")
    sanitized via cache_key
  - Empty prompt_version at construction raises ValueError
  - Counter accuracy across a mixed hit/miss sequence
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, '.')

from data.replay_cache import (
    CACHE_SCHEMA_VERSION,
    CachedLLMClient,
    _ctx_to_canonical_json,
)
from strategy.llm.clients import (
    APIUnavailableError,
    BudgetExhaustedError,
    SchemaInvalidError,
)
from strategy.llm.types import LLMContext, LLMDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(**overrides: Any) -> LLMContext:
    """Build a minimal LLMContext for tests."""
    kwargs: dict[str, Any] = dict(
        ticker="AAPL",
        timestamp_et="2026-04-15T09:35:00-04:00",
        prompt_version="v0.1-test",
    )
    kwargs.update(overrides)
    return LLMContext(**kwargs)


def _decision(**overrides: Any) -> LLMDecision:
    """Build a minimal valid LLMDecision."""
    kwargs: dict[str, Any] = dict(
        action="Buy",
        confidence=72,
        setup_label="gap_and_go",
        reasoning="strong premarket volume and gap above resistance",
    )
    kwargs.update(overrides)
    return LLMDecision(**kwargs)


class _FakeClient:
    """LLMClient stub: returns a pre-canned decision, counts calls."""

    def __init__(
        self,
        *,
        decision: LLMDecision | None = None,
        raises: Exception | None = None,
        backend: str = "anthropic",
        model_id: str = "claude-sonnet-4-5",
    ) -> None:
        self.model_id = model_id
        self.backend = backend
        self._decision = decision or _decision()
        self._raises = raises
        self.calls = 0

    async def evaluate(self, ctx: LLMContext) -> LLMDecision:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._decision


def _run(coro):
    """Drive an async coroutine to completion.

    Uses ``asyncio.run`` (Python 3.7+) rather than the deprecated
    ``get_event_loop`` so this works cleanly under Python 3.10+ where
    the loop isn't pre-created. Each call spins up a fresh loop;
    that's fine because ``CachedLLMClient`` carries no loop-bound
    state.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Construction / surface
# ---------------------------------------------------------------------------


def test_cached_llm_client_exposes_inner_identity(tmp_path):
    inner = _FakeClient(backend="anthropic", model_id="claude-opus-4-6")
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    assert wrapped.backend == "anthropic"
    assert wrapped.model_id == "claude-opus-4-6"
    assert wrapped.hits == 0
    assert wrapped.misses == 0


def test_cached_llm_client_rejects_empty_prompt_version(tmp_path):
    with pytest.raises(ValueError, match="prompt_version"):
        CachedLLMClient(
            _FakeClient(), cache_dir=tmp_path, prompt_version=""
        )


# ---------------------------------------------------------------------------
# Cache miss path
# ---------------------------------------------------------------------------


def test_miss_calls_inner_writes_envelope_returns_decision(tmp_path):
    inner = _FakeClient(decision=_decision(action="Buy", confidence=80))
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    result = _run(wrapped.evaluate(_ctx()))
    assert isinstance(result, LLMDecision)
    assert result.action == "Buy"
    assert result.confidence == 80
    assert inner.calls == 1
    assert wrapped.misses == 1
    assert wrapped.hits == 0

    # Cache file was written under <cache_dir>/<backend>/<model_id>/<sha>.json
    written = list(tmp_path.rglob("*.json"))
    assert len(written) == 1
    envelope = json.loads(written[0].read_text())
    assert envelope["schema_version"] == CACHE_SCHEMA_VERSION
    assert envelope["prompt_version"] == "v1"
    assert envelope["backend"] == "anthropic"
    assert envelope["model_id"] == "claude-sonnet-4-5"
    assert envelope["decision"]["action"] == "Buy"
    assert envelope["decision"]["confidence"] == 80
    # cached_at field present and is an ISO-Z string
    assert envelope["cached_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Cache hit path
# ---------------------------------------------------------------------------


def test_hit_skips_inner_returns_deserialized_decision(tmp_path):
    inner = _FakeClient(decision=_decision(action="Sell", confidence=65))
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    # First call populates the cache.
    first = _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert wrapped.misses == 1

    # Second call hits.
    second = _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1  # no additional call
    assert wrapped.hits == 1
    assert wrapped.misses == 1

    # Decisions are equal (Pydantic equality).
    assert first == second
    assert second.action == "Sell"
    assert second.confidence == 65


def test_hit_round_trip_preserves_all_fields(tmp_path):
    inner = _FakeClient(
        decision=_decision(
            action="Buy",
            confidence=88,
            setup_label="vwap_reclaim",
            reasoning="bounce off VWAP with RSI divergence and rising MACD",
            stop_loss_atr_multiple=1.8,
            take_profit_atr_multiple=2.5,
            time_horizon="intraday",
            expected_move_pct=2.3,
            expected_holding_minutes=45,
            concerns=["earnings risk"],
            alternative_view="market regime shift could invalidate setup",
        )
    )
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    first = _run(wrapped.evaluate(_ctx()))
    second = _run(wrapped.evaluate(_ctx()))
    assert first == second
    # Spot-check each non-default field.
    assert second.setup_label == "vwap_reclaim"
    assert second.stop_loss_atr_multiple == 1.8
    assert second.take_profit_atr_multiple == 2.5
    assert second.expected_move_pct == 2.3
    assert second.expected_holding_minutes == 45
    assert second.concerns == ["earnings risk"]
    assert second.alternative_view.startswith("market regime")


# ---------------------------------------------------------------------------
# Key invalidation
# ---------------------------------------------------------------------------


def test_prompt_version_bump_misses(tmp_path):
    inner = _FakeClient()
    a = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    b = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v2")

    _run(a.evaluate(_ctx()))
    assert inner.calls == 1

    # Different prompt_version -> different cache file -> miss.
    _run(b.evaluate(_ctx()))
    assert inner.calls == 2
    assert b.misses == 1
    assert b.hits == 0


def test_backend_change_misses(tmp_path):
    inner_a = _FakeClient(backend="anthropic", model_id="m")
    inner_b = _FakeClient(backend="qwen_local", model_id="m")
    a = CachedLLMClient(inner_a, cache_dir=tmp_path, prompt_version="v1")
    b = CachedLLMClient(inner_b, cache_dir=tmp_path, prompt_version="v1")

    _run(a.evaluate(_ctx()))
    _run(b.evaluate(_ctx()))
    assert inner_a.calls == 1
    assert inner_b.calls == 1
    # Two distinct cache files under different parent dirs.
    parents = {p.parent for p in tmp_path.rglob("*.json")}
    assert len(parents) == 2


def test_model_id_change_misses(tmp_path):
    inner_a = _FakeClient(backend="anthropic", model_id="claude-sonnet-4-5")
    inner_b = _FakeClient(backend="anthropic", model_id="claude-opus-4-6")
    a = CachedLLMClient(inner_a, cache_dir=tmp_path, prompt_version="v1")
    b = CachedLLMClient(inner_b, cache_dir=tmp_path, prompt_version="v1")

    _run(a.evaluate(_ctx()))
    _run(b.evaluate(_ctx()))
    assert inner_a.calls == 1
    assert inner_b.calls == 1
    # Different model_id => different parent dir under same backend.
    parents = sorted({str(p.parent) for p in tmp_path.rglob("*.json")})
    assert len(parents) == 2
    assert "claude-sonnet-4-5" in parents[0] or "claude-sonnet-4-5" in parents[1]
    assert "claude-opus-4-6" in parents[0] or "claude-opus-4-6" in parents[1]


def test_context_change_misses(tmp_path):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    _run(wrapped.evaluate(_ctx(ticker="AAPL")))
    _run(wrapped.evaluate(_ctx(ticker="NVDA")))
    assert inner.calls == 2
    assert wrapped.misses == 2
    assert wrapped.hits == 0


# ---------------------------------------------------------------------------
# Canonical JSON stability
# ---------------------------------------------------------------------------


def test_canonical_json_is_sorted_and_stable():
    """Same content, sorted-keys output regardless of field-declaration order."""
    ctx_a = _ctx(ticker="AAPL", catalyst_flags=("earnings", "guidance"))
    s_a = _ctx_to_canonical_json(ctx_a)
    s_b = _ctx_to_canonical_json(ctx_a)
    assert s_a == s_b  # deterministic
    # Sorted: assert the first top-level key is alphabetically smallest.
    keys_in_order = [
        k for k in s_a.replace('{', '').split(',') if k and k.startswith('"')
    ]
    # The very first '"key":' substring should be alphabetical.
    first_key_pos = s_a.find('"')
    assert s_a[first_key_pos:first_key_pos + 20].startswith('"avg_daily_volume"')


def test_canonical_json_differs_when_field_differs():
    s_a = _ctx_to_canonical_json(_ctx(ticker="AAPL"))
    s_b = _ctx_to_canonical_json(_ctx(ticker="NVDA"))
    assert s_a != s_b


def test_two_identical_contexts_share_cache_slot(tmp_path):
    """Two contexts constructed with identical field values produce the
    same cache key and the second call is a hit even if they're
    distinct Python objects."""
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    ctx_a = _ctx(ticker="AAPL", pm_rvol=2.5)
    ctx_b = _ctx(ticker="AAPL", pm_rvol=2.5)
    assert ctx_a is not ctx_b
    assert ctx_a == ctx_b  # frozen dataclass equality

    _run(wrapped.evaluate(ctx_a))
    _run(wrapped.evaluate(ctx_b))
    assert inner.calls == 1
    assert wrapped.hits == 1
    assert wrapped.misses == 1


# ---------------------------------------------------------------------------
# Soft-miss paths (malformed cache files)
# ---------------------------------------------------------------------------


def _seed_envelope_file(
    tmp_path: Path,
    *,
    backend: str = "anthropic",
    model_id: str = "claude-sonnet-4-5",
    prompt_version: str = "v1",
    ctx: LLMContext | None = None,
    payload: Any = "OVERRIDE",
) -> Path:
    """Pre-populate the exact cache path a CachedLLMClient would write.

    Returns the path written. ``payload`` may be a dict (written
    as-is), a str (written as raw text -- useful for "malformed
    JSON" tests), or the sentinel ``"OVERRIDE"`` to write a default
    valid envelope.
    """
    from data.replay.config import cache_key

    if ctx is None:
        ctx = _ctx()
    prompt = _ctx_to_canonical_json(ctx)
    path = cache_key(
        prompt=prompt,
        prompt_version=prompt_version,
        backend=backend,
        model_id=model_id,
        cache_dir=tmp_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload == "OVERRIDE":
        envelope = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cached_at": "2026-05-15T00:00:00Z",
            "prompt_version": prompt_version,
            "backend": backend,
            "model_id": model_id,
            "decision": json.loads(_decision().model_dump_json()),
        }
        path.write_text(json.dumps(envelope))
    elif isinstance(payload, str):
        path.write_text(payload)
    else:
        path.write_text(json.dumps(payload))
    return path


def test_malformed_json_treated_as_miss(tmp_path, caplog):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    _seed_envelope_file(tmp_path, payload="{ this is : not valid json")

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        decision = _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert wrapped.misses == 1
    assert wrapped.hits == 0
    assert any("failed to read" in r.message for r in caplog.records)
    # The miss path overwrites the file with a valid envelope.
    assert isinstance(decision, LLMDecision)


def test_non_dict_root_treated_as_miss(tmp_path, caplog):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    _seed_envelope_file(tmp_path, payload=["not", "a", "dict"])

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert any("not a dict" in r.message for r in caplog.records)


def test_schema_version_mismatch_treated_as_miss(tmp_path, caplog):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    _seed_envelope_file(
        tmp_path,
        payload={
            "schema_version": CACHE_SCHEMA_VERSION + 99,
            "decision": json.loads(_decision().model_dump_json()),
        },
    )

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert any("schema_version" in r.message for r in caplog.records)


def test_missing_decision_field_treated_as_miss(tmp_path, caplog):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    _seed_envelope_file(
        tmp_path,
        payload={
            "schema_version": CACHE_SCHEMA_VERSION,
            "prompt_version": "v1",
        },
    )

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert any("missing 'decision'" in r.message for r in caplog.records)


def test_decision_schema_validation_failure_treated_as_miss(tmp_path, caplog):
    """If the cached decision payload doesn't validate as LLMDecision
    (schema evolution, corruption), treat as miss + WARNING."""
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")
    _seed_envelope_file(
        tmp_path,
        payload={
            "schema_version": CACHE_SCHEMA_VERSION,
            "decision": {
                "action": "Definitely Not A Valid Action",
                "confidence": 500,  # out of range
            },
        },
    )

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        _run(wrapped.evaluate(_ctx()))
    assert inner.calls == 1
    assert any("schema validation" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cache-write failure
# ---------------------------------------------------------------------------


def test_write_failure_returns_inner_result_with_warning(monkeypatch, tmp_path, caplog):
    """If os.replace raises OSError, log WARNING + return inner's
    decision unchanged. Cache writes are best-effort."""
    inner = _FakeClient(decision=_decision(action="Buy", confidence=77))
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    real_replace = __import__("os").replace

    def boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr("data.replay_cache.os.replace", boom)

    with caplog.at_level("WARNING", logger="data.replay_cache"):
        decision = _run(wrapped.evaluate(_ctx()))

    assert decision.action == "Buy"
    assert decision.confidence == 77
    assert inner.calls == 1
    assert any("failed to write" in r.message for r in caplog.records)
    # No final cache file landed (os.replace failed before atomicity).
    final_files = [
        p for p in tmp_path.rglob("*.json") if not p.name.endswith(".tmp")
    ]
    assert final_files == []


# ---------------------------------------------------------------------------
# Inner-exception propagation (errors are never cached)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        APIUnavailableError("backend timed out"),
        SchemaInvalidError("LLM returned malformed JSON"),
        BudgetExhaustedError("daily T2 cap reached"),
        RuntimeError("generic boom"),
    ],
)
def test_inner_exceptions_propagate_uncached(tmp_path, exc):
    inner = _FakeClient(raises=exc)
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    with pytest.raises(type(exc)):
        _run(wrapped.evaluate(_ctx()))

    # No cache file was written -- transient errors must not poison cache.
    assert list(tmp_path.rglob("*.json")) == []
    # And the wrapper counted a miss but no hit.
    assert wrapped.misses == 1
    assert wrapped.hits == 0


# ---------------------------------------------------------------------------
# Filesystem edge cases
# ---------------------------------------------------------------------------


def test_model_id_with_slashes_sanitized(tmp_path):
    """HF-style model IDs containing slashes shouldn't create
    unintended directory nesting; cache_key already handles this but
    the wrapper relies on it."""
    inner = _FakeClient(backend="local", model_id="meta-llama/Llama-3.3-70B")
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    _run(wrapped.evaluate(_ctx()))
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    # The model component appears with underscores, not as a nested dir.
    assert "meta-llama_Llama-3.3-70B" in str(files[0].parent)


# ---------------------------------------------------------------------------
# Counter accuracy
# ---------------------------------------------------------------------------


def test_counters_track_mixed_hit_miss_sequence(tmp_path):
    inner = _FakeClient()
    wrapped = CachedLLMClient(inner, cache_dir=tmp_path, prompt_version="v1")

    # Miss, hit, miss (different ctx), hit (re-call first ctx), hit (re-call second ctx)
    ctx_a = _ctx(ticker="AAPL")
    ctx_b = _ctx(ticker="NVDA")
    _run(wrapped.evaluate(ctx_a))  # miss
    _run(wrapped.evaluate(ctx_a))  # hit
    _run(wrapped.evaluate(ctx_b))  # miss
    _run(wrapped.evaluate(ctx_a))  # hit
    _run(wrapped.evaluate(ctx_b))  # hit

    assert wrapped.misses == 2
    assert wrapped.hits == 3
    assert inner.calls == 2


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


def test_cache_persists_across_wrapper_instances(tmp_path):
    """A second CachedLLMClient pointing at the same cache_dir +
    prompt_version + inner gets a hit on a key the first instance
    wrote -- enables re-running the harness without re-paying for
    the LLM."""
    inner_a = _FakeClient(decision=_decision(action="Buy", confidence=66))
    wrapped_a = CachedLLMClient(inner_a, cache_dir=tmp_path, prompt_version="v1")
    _run(wrapped_a.evaluate(_ctx()))
    assert inner_a.calls == 1

    # Fresh wrapper, fresh inner. Second inner is rigged to FAIL if
    # called -- we expect a cache hit so it shouldn't be reached.
    inner_b = _FakeClient(raises=RuntimeError("should never be called"))
    wrapped_b = CachedLLMClient(inner_b, cache_dir=tmp_path, prompt_version="v1")
    decision = _run(wrapped_b.evaluate(_ctx()))
    assert decision.action == "Buy"
    assert decision.confidence == 66
    assert inner_b.calls == 0
    assert wrapped_b.hits == 1
