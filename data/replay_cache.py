"""Per-tier LLM response cache for the M2 replay harness.

Wraps an ``LLMClient`` (any tier backend: Anthropic, Qwen local,
Haiku stand-in) with an on-disk JSON cache keyed by
``(prompt_version, canonical_serialized_context, backend, model_id)``.

Two cost stories:

- For Anthropic-backed tiers (T2 Sonnet, T3 Opus), cache hits on a
  re-run save real dollars. A 30-day replay across a ~50-ticker
  watchlist generates O(tens of thousands) of tier calls; eating
  that bill twice while iterating prompts would be wasteful.
- For local Qwen via LM Studio, "cost" is wall-clock. A 50-tok/s
  model evaluating ~10k contexts takes hours; cache hits make
  re-runs minutes.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § LLM caching, the cache
key includes ``prompt_version`` so bumping a prompt only invalidates
the affected tier's cache (other tiers' caches survive). The
existing ``data.replay.config.cache_key`` helper builds the path;
this module owns the read / write / wrap logic on top.

A note on context serialization:

``LLMContext`` is a frozen dataclass (``strategy/llm/types.py``),
NOT a Pydantic model, so the canonical-JSON we feed into
``cache_key`` is built via ``dataclasses.asdict`` + ``json.dumps``
with ``sort_keys=True``. ``LLMDecision`` IS Pydantic; the cached
payload uses ``model_dump_json`` / ``model_validate_json`` for the
decision portion of the envelope. The asymmetry is intentional and
the imports below make it visible.

Failure semantics (Rule 18):

- Cache hit but malformed JSON / wrong schema_version / failing
  ``model_validate_json``: log WARNING, treat as miss, call inner,
  overwrite. Option 2 visible degradation -- the replay continues
  with correct data, the operator sees the warning.
- Cache write failure (disk full, permission denied): log WARNING,
  return inner's result anyway. Cache writes are best-effort;
  blocking a 6-hour replay over a transient disk error would be
  the wrong trade.
- Inner client raises (``APIUnavailableError`` /
  ``SchemaInvalidError`` / ``BudgetExhaustedError`` / anything
  else): propagate unchanged. The wrapper does NOT swallow inner
  errors; the calling signal-engine path already handles them via
  ``tier_provenance``. Errors are never cached -- a transient 5xx
  must not poison the cache for the next run.
- Context serialization failure: raise loud. This would indicate an
  ``LLMContext`` field type the json encoder can't handle, which
  is a caller bug worth surfacing.

Concurrency: two replay processes sharing one cache_dir read the
same files concurrently safely; writes use ``.tmp`` + ``os.replace``
so a partial write never leaves a half-truncated file the next
process would mis-parse. No explicit locking -- the worst case is
both writes racing and one wins, which is fine for an idempotent
cache.

Status: M2.2 sub-task #8 -- fully implemented.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from data.replay.config import cache_key
from strategy.llm.clients import LLMClient
from strategy.llm.types import LLMContext, LLMDecision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Bump on any structural change to the on-disk envelope so stale
# caches deserialize as miss instead of producing silently-wrong
# decisions. The bump path: change CACHE_SCHEMA_VERSION here, the
# old cache files will all soft-miss + WARNING, and the next run
# re-populates them with the new envelope.
CACHE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _ctx_to_canonical_json(ctx: LLMContext) -> str:
    """Serialize an LLMContext to a stable, sorted-keys JSON string.

    This is the input to ``cache_key``'s ``prompt`` parameter. Two
    contexts that serialize identically share a cache slot, which is
    the right behavior: identical inputs produce identical model
    calls.

    ``default=str`` defends against any field type that slips into
    the dataclass and isn't natively JSON-encodable (e.g. a stray
    ``Decimal`` or ``datetime``). It does NOT silently lose data --
    a custom type just gets stringified, which means two contexts
    with the same string representation also share a cache slot.
    """
    return json.dumps(
        dataclasses.asdict(ctx),
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )


def _utc_now_iso() -> str:
    """UTC now as the envelope's ``cached_at`` ISO string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _read_envelope(path: Path) -> LLMDecision | None:
    """Read the cached envelope at ``path`` and return the decision.

    Returns ``None`` on any of:
      - file does not exist (clean miss, not logged)
      - JSON decode error (corrupt file -- WARNING, treat as miss)
      - missing ``schema_version`` / mismatch (WARNING, treat as miss)
      - missing ``decision`` payload (WARNING, treat as miss)
      - ``LLMDecision.model_validate_json`` raises (WARNING, treat as
        miss; this catches the case where the LLMDecision schema
        evolves and old envelopes can't deserialize cleanly)

    Every soft-miss path logs at WARNING so a malformed cache surfaces
    in journal output rather than silently producing wrong decisions.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            envelope = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "replay_cache: failed to read %s (%s); treating as miss",
            path, e,
        )
        return None

    if not isinstance(envelope, dict):
        logger.warning(
            "replay_cache: envelope at %s is not a dict (%s); "
            "treating as miss",
            path, type(envelope).__name__,
        )
        return None

    if envelope.get("schema_version") != CACHE_SCHEMA_VERSION:
        logger.warning(
            "replay_cache: envelope at %s has schema_version=%r, "
            "expected %r; treating as miss",
            path, envelope.get("schema_version"), CACHE_SCHEMA_VERSION,
        )
        return None

    decision_payload = envelope.get("decision")
    if decision_payload is None:
        logger.warning(
            "replay_cache: envelope at %s missing 'decision' field; "
            "treating as miss",
            path,
        )
        return None

    try:
        if isinstance(decision_payload, str):
            return LLMDecision.model_validate_json(decision_payload)
        return LLMDecision.model_validate(decision_payload)
    except ValidationError as e:
        logger.warning(
            "replay_cache: decision payload at %s failed schema validation "
            "(%s); treating as miss",
            path, e,
        )
        return None


def _write_envelope(
    path: Path,
    decision: LLMDecision,
    *,
    prompt_version: str,
    backend: str,
    model_id: str,
) -> bool:
    """Atomically write an envelope to ``path``. Returns True on success.

    Atomicity via ``<path>.tmp`` + ``os.replace`` so a SIGKILL or
    disk-full mid-write never leaves a half-truncated JSON file the
    next replay run would treat as malformed.

    Any OSError during write or replace is logged at WARNING and
    causes a False return -- callers treat that as "cache write
    failed, return inner result anyway." This is best-effort caching
    by design (Rule 18 option 2): never block the replay over a
    transient disk problem.
    """
    envelope = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cached_at": _utc_now_iso(),
        "prompt_version": prompt_version,
        "backend": backend,
        "model_id": model_id,
        "decision": json.loads(decision.model_dump_json()),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning(
            "replay_cache: failed to write %s (%s); returning inner "
            "decision uncached",
            path, e,
        )
        # Best-effort cleanup of the stray .tmp if it exists. Don't
        # raise if cleanup fails -- we already logged the parent
        # write failure.
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# CachedLLMClient adapter
# ---------------------------------------------------------------------------


class CachedLLMClient:
    """LLMClient adapter that JSON-caches ``evaluate`` responses on disk.

    Satisfies the ``strategy.llm.clients.LLMClient`` protocol so it
    can wrap any tier backend transparently. The replay loop wraps
    each enabled tier in one of these; the live signal engine does
    NOT use this wrapper (live calls go through directly).

    Construction is cheap and lazy: nothing touches disk until the
    first ``evaluate`` call.

    Attributes (publicly readable):
        model_id: delegated to ``inner.model_id``.
        backend: delegated to ``inner.backend``.
        hits: count of cache hits across this instance's lifetime.
        misses: count of cache misses (and soft-misses on malformed
            cache files).

    The hits/misses counters reset only on instance creation;
    long-lived replay runs accumulate stats the loop's wrap step
    reads at end.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        cache_dir: Path,
        prompt_version: str,
    ) -> None:
        if not prompt_version:
            raise ValueError(
                "CachedLLMClient requires a non-empty prompt_version"
            )
        self._inner = inner
        self._cache_dir = cache_dir
        self._prompt_version = prompt_version
        self.hits = 0
        self.misses = 0

    # Pass-through identity fields so the wrapper satisfies the
    # LLMClient Protocol's ``model_id`` and ``backend`` attribute
    # contracts.
    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def backend(self) -> str:
        return self._inner.backend

    async def evaluate(self, ctx: LLMContext) -> LLMDecision:
        """Return a cached decision if available, else call inner + cache the result.

        Cache key derivation per
        ``docs/M2_REPLAY_HARNESS_DESIGN.md`` § LLM caching: the
        existing ``cache_key`` helper hashes
        ``(prompt_version, canonical_serialized_context)`` and
        namespaces by ``(backend, model_id)``. Bumping
        ``prompt_version`` invalidates this tier alone; bumping
        ``model_id`` is also an invalidation (different parent dir).

        Inner errors are NEVER cached -- a transient
        ``APIUnavailableError`` must not poison the cache for the
        next run. They propagate to the caller unchanged.
        """
        # Build the cache path. cache_key can raise ValueError for
        # empty backend/model_id/prompt_version; the prompt-version
        # check is in __init__, but backend/model_id come from the
        # inner client and we cannot assume those are non-empty.
        # Let cache_key raise -- a misbuilt client is a programmer
        # error worth surfacing loudly (Rule 18 option 3).
        prompt = _ctx_to_canonical_json(ctx)
        path = cache_key(
            prompt=prompt,
            prompt_version=self._prompt_version,
            backend=self._inner.backend,
            model_id=self._inner.model_id,
            cache_dir=self._cache_dir,
        )

        cached = _read_envelope(path)
        if cached is not None:
            self.hits += 1
            return cached

        # Cache miss (clean or soft). Call inner; on success, write
        # the envelope. Inner exceptions propagate without being
        # cached.
        self.misses += 1
        decision = await self._inner.evaluate(ctx)

        _write_envelope(
            path,
            decision,
            prompt_version=self._prompt_version,
            backend=self._inner.backend,
            model_id=self._inner.model_id,
        )
        # _write_envelope's return value is informational; even if
        # the write failed we still return the live decision.
        return decision

    # ---- Debug helpers ----

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"CachedLLMClient(backend={self.backend!r}, "
            f"model_id={self.model_id!r}, "
            f"prompt_version={self._prompt_version!r}, "
            f"hits={self.hits}, misses={self.misses})"
        )


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CachedLLMClient",
]
