"""LLM signal engine — the live entry point.

``evaluate`` is what ``main.py`` calls per (ticker, timestamp) candidate
in the live signal path, and what the M2 replay harness calls per
(ticker, time_tick) replay step. It orchestrates the three tiers:

1. Always call Tier 1 (Qwen local, or Anthropic stand-in during the
   bridge period). Failure here yields a synthetic Hold with the
   failure mode encoded in setup_label.
2. Conditionally call Tier 2 (Sonnet) per ``escalation.escalation_rule``.
   On success, merge T1 and T2. On failure, fall back to T1 alone.
3. Tier 3 (Opus) is NOT in the live path — it runs only in the M2
   replay harness and the offline weekly audit. Live ``evaluate``
   ignores ``clients.t3``.

Failure handling follows ``docs/LLM_SIGNAL_INTERFACE.md`` § "Failure
modes & fallbacks": every uncaught exception inside a tier collapses
into ``Hold(reason=...)``. The signal engine never raises.

The budget is consumed when an escalation is *attempted*, not only on
success. Otherwise a flaky T2 endpoint on a busy day could blow
through the per-day cap many times over.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .clients import (
    APIUnavailableError,
    LLMClient,
    SchemaInvalidError,
)
from .escalation import EscalationBudget, escalation_rule
from .merge import merge_tiers
from .types import LLMContext, LLMDecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TierClients:
    """The set of clients the signal_engine talks to.

    ``t1`` is required. ``t2`` is required when escalation is enabled in
    config (Phase 0 default: enabled). ``t3`` is NOT used by live
    ``evaluate`` — the field exists here so ``TierClients`` can be the
    single config object passed around by both replay and live paths.
    The replay harness reads ``t3`` directly; live ignores it.
    """

    t1: LLMClient
    t2: LLMClient | None
    t3: LLMClient | None


# Hold setup_labels used by failure paths. Kept as a constant block so
# the M2 replay harness and post-hoc audit can group decisions by this
# field without having to grep the source.
_LABEL_SCHEMA_INVALID_T1 = "schema_invalid_t1"
_LABEL_API_FAILURE_T1 = "api_failure_t1"
_LABEL_T1_UNEXPECTED = "t1_unexpected"


def _make_hold_failure(label: str, reason: str) -> LLMDecision:
    """Synthesize a Hold for Tier 1 failure paths.

    Uses ``tier_provenance="t1_failed"`` so post-hoc analysis can
    distinguish failure-Holds from rule-based Holds without parsing
    setup_label strings.
    """
    return LLMDecision(
        action="Hold",
        confidence=0,
        setup_label=label[:50],
        reasoning=reason[:280],
        tier_provenance="t1_failed",
    )


async def evaluate(
    ctx: LLMContext,
    clients: TierClients,
    budget: EscalationBudget,
    *,
    confidence_floor: int = 50,
    confidence_ceiling: int = 75,
    pm_rvol_min: float = 3.0,
) -> LLMDecision:
    """Run the live tier orchestration. Returns the merged decision.

    Never raises. Every failure mode is mapped to either a synthetic
    Hold (Tier 1 failure) or a fall-back to Tier 1 (Tier 2 failure).
    """
    # ---- Tier 1: always called ----
    try:
        t1 = await clients.t1.evaluate(ctx)
    except SchemaInvalidError as exc:
        logger.warning("Tier 1 schema-invalid for %s: %s", ctx.ticker, exc)
        return _make_hold_failure(_LABEL_SCHEMA_INVALID_T1, str(exc))
    except APIUnavailableError as exc:
        logger.error("Tier 1 API unavailable for %s: %s", ctx.ticker, exc)
        return _make_hold_failure(_LABEL_API_FAILURE_T1, str(exc))
    except Exception as exc:
        # Defensive catch-all — never let the signal engine raise.
        logger.exception("Tier 1 unexpected error for %s", ctx.ticker)
        return _make_hold_failure(
            _LABEL_T1_UNEXPECTED, f"{type(exc).__name__}: {exc}"
        )

    # ---- Decide whether to escalate ----
    t2_client = clients.t2
    if t2_client is None:
        # T2 not configured: T1 is the live decision.
        return t1.model_copy(update={"tier_provenance": "t1_only"})

    should_escalate = escalation_rule(
        ctx,
        t1,
        budget,
        confidence_floor=confidence_floor,
        confidence_ceiling=confidence_ceiling,
        pm_rvol_min=pm_rvol_min,
    )
    if not should_escalate:
        # Distinguish "gates didn't fire" from "budget exhausted" so
        # post-hoc analysis can spot days when budget would have caught
        # otherwise-valid escalations.
        if not budget.has_capacity():
            return t1.model_copy(
                update={"tier_provenance": "t1_only_budget_exhausted"}
            )
        return t1.model_copy(update={"tier_provenance": "t1_only"})

    # ---- Tier 2: escalate ----
    # Consume budget BEFORE the call so flaky T2 endpoints can't blow
    # through the per-day cap on retries.
    budget.record()
    try:
        t2 = await t2_client.evaluate(ctx)
    except (SchemaInvalidError, APIUnavailableError) as exc:
        logger.warning(
            "Tier 2 escalation failed for %s; using Tier 1 alone: %s",
            ctx.ticker,
            exc,
        )
        return t1.model_copy(update={"tier_provenance": "t1_fallback_t2"})
    except Exception as exc:
        logger.exception("Tier 2 unexpected error for %s", ctx.ticker)
        return t1.model_copy(update={"tier_provenance": "t1_fallback_t2"})

    return merge_tiers(t1, t2)
