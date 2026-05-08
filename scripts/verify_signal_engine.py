"""Verification for signal_engine.evaluate (step 5 plumbing test).

Mocks Tier 1 and Tier 2 clients and exercises every branch in the
orchestrator. No real API calls, no Anthropic costs.

Paths covered:
  1. T1 returns Hold with low confidence -> no escalation -> t1_only
  2. T1 conf in escalation band, good catalyst, high RVOL -> T2 fires
     and agrees -> t1_t2_agree, higher confidence wins
  3. Same setup but T2 disagrees -> t1_t2_disagree, Hold
  4. T1 conf in band but no catalyst -> no escalation -> t1_only
  5. T1 conf below floor -> no escalation -> t1_only
  6. Budget exhausted before call -> no escalation -> t1_only_budget_exhausted
  7. T1 raises APIUnavailableError -> Hold(api_failure_t1) -> t1_failed
  8. T1 raises SchemaInvalidError -> Hold(schema_invalid_t1) -> t1_failed
  9. T2 raises APIUnavailableError mid-escalation -> t1_fallback_t2,
     budget consumed (not rolled back)
 10. T2 raises SchemaInvalidError mid-escalation -> t1_fallback_t2
 11. clients.t2 is None -> T1 is live decision regardless of gates
     -> t1_only

Run with:
    cd C:\\trading\\LLM model
    $env:PYTHONPATH = "."
    python scripts/verify_signal_engine.py
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock

from strategy.llm.clients import APIUnavailableError, SchemaInvalidError
from strategy.llm.escalation import EscalationBudget
from strategy.llm.signal_engine import TierClients, evaluate
from strategy.llm.types import LLMContext, LLMDecision

HIGH_QUALITY_CATALYST_TUPLE = ("FDA_approval",)


def _ctx(
    *,
    catalyst_flags: tuple[str, ...] = HIGH_QUALITY_CATALYST_TUPLE,
    pm_rvol: float = 4.0,
) -> LLMContext:
    return LLMContext(
        ticker="NVDA",
        timestamp_et="2026-05-08 09:42:00 ET",
        prompt_version="v0.0-stub",
        catalyst_flags=catalyst_flags,
        pm_rvol=pm_rvol,
    )


def _decision(
    action: str = "Hold",
    confidence: int = 30,
    setup_label: str = "test",
    reasoning: str = "test",
) -> LLMDecision:
    return LLMDecision(
        action=action,
        confidence=confidence,
        setup_label=setup_label,
        reasoning=reasoning,
    )


def _print(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _make_clients(
    t1_returns=None,
    t1_raises=None,
    t2_returns=None,
    t2_raises=None,
    t2_present: bool = True,
) -> TierClients:
    t1 = AsyncMock()
    if t1_raises is not None:
        t1.evaluate.side_effect = t1_raises
    else:
        t1.evaluate.return_value = t1_returns

    if not t2_present:
        return TierClients(t1=t1, t2=None, t3=None)

    t2 = AsyncMock()
    if t2_raises is not None:
        t2.evaluate.side_effect = t2_raises
    else:
        t2.evaluate.return_value = t2_returns
    return TierClients(t1=t1, t2=t2, t3=None)


async def main() -> int:
    all_ok = True

    # --- Path 1: T1 Hold, low confidence -> no escalation -> t1_only ---
    clients = _make_clients(
        t1_returns=_decision(action="Hold", confidence=20, setup_label="weak")
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "1. low-conf Hold -> t1_only",
        result.action == "Hold"
        and result.tier_provenance == "t1_only"
        and clients.t2.evaluate.call_count == 0,
        f"action={result.action} prov={result.tier_provenance} t2_calls={clients.t2.evaluate.call_count}",
    )

    # --- Path 2: escalation fires + T1/T2 agree -> t1_t2_agree ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65, setup_label="gap-and-go", reasoning="r1"),
        t2_returns=_decision(action="Buy", confidence=80, setup_label="gap-and-go", reasoning="r2 deeper"),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "2. agree -> t1_t2_agree, max(conf), T2 reasoning",
        result.action == "Buy"
        and result.tier_provenance == "t1_t2_agree"
        and result.confidence == 80
        and "r2 deeper" in result.reasoning
        and result.reasoning.startswith("[T1+T2 agree]")
        and budget.used == 1,
        f"action={result.action} conf={result.confidence} prov={result.tier_provenance} budget_used={budget.used}",
    )

    # --- Path 3: escalation fires + disagree -> t1_t2_disagree, Hold ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_returns=_decision(action="Sell", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "3. disagree -> Hold, t1_t2_disagree",
        result.action == "Hold"
        and result.tier_provenance == "t1_t2_disagree"
        and result.confidence == 0
        and result.setup_label == "tier_disagreement"
        and budget.used == 1,
        f"action={result.action} prov={result.tier_provenance} setup={result.setup_label}",
    )

    # --- Path 4: gates fail (no catalyst) -> no escalation -> t1_only ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_returns=_decision(action="Buy", confidence=80),  # never called
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(catalyst_flags=()), clients, budget)
    all_ok &= _print(
        "4. no catalyst -> no escalation -> t1_only",
        result.action == "Buy"
        and result.tier_provenance == "t1_only"
        and clients.t2.evaluate.call_count == 0
        and budget.used == 0,
        f"prov={result.tier_provenance} t2_calls={clients.t2.evaluate.call_count}",
    )

    # --- Path 5: T1 conf below floor -> no escalation -> t1_only ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=40),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "5. conf below floor -> no escalation -> t1_only",
        result.tier_provenance == "t1_only"
        and clients.t2.evaluate.call_count == 0,
        f"prov={result.tier_provenance}",
    )

    # --- Path 6: budget exhausted -> t1_only_budget_exhausted ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=2)
    budget.record()
    budget.record()  # used=2, has_capacity=False
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "6. budget exhausted -> t1_only_budget_exhausted",
        result.tier_provenance == "t1_only_budget_exhausted"
        and clients.t2.evaluate.call_count == 0
        and budget.used == 2,  # not incremented further
        f"prov={result.tier_provenance} budget_used={budget.used}",
    )

    # --- Path 7: T1 APIUnavailableError -> Hold(api_failure_t1) -> t1_failed ---
    clients = _make_clients(
        t1_raises=APIUnavailableError("connection refused"),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "7. T1 API unavail -> Hold(api_failure_t1), t1_failed, no T2 call",
        result.action == "Hold"
        and result.tier_provenance == "t1_failed"
        and result.setup_label == "api_failure_t1"
        and clients.t2.evaluate.call_count == 0
        and budget.used == 0,
        f"setup={result.setup_label} prov={result.tier_provenance}",
    )

    # --- Path 8: T1 SchemaInvalidError -> Hold(schema_invalid_t1) -> t1_failed ---
    clients = _make_clients(
        t1_raises=SchemaInvalidError("missing required field"),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "8. T1 schema-invalid -> Hold(schema_invalid_t1), t1_failed",
        result.action == "Hold"
        and result.tier_provenance == "t1_failed"
        and result.setup_label == "schema_invalid_t1"
        and clients.t2.evaluate.call_count == 0,
        f"setup={result.setup_label} prov={result.tier_provenance}",
    )

    # --- Path 9: T2 APIUnavailableError mid-escalation -> t1_fallback_t2 ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65, setup_label="x", reasoning="r1"),
        t2_raises=APIUnavailableError("503"),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "9. T2 API unavail -> t1_fallback_t2 (T1 alone, budget consumed)",
        result.action == "Buy"
        and result.tier_provenance == "t1_fallback_t2"
        and result.confidence == 65
        and budget.used == 1,  # consumed despite T2 failure
        f"prov={result.tier_provenance} budget_used={budget.used}",
    )

    # --- Path 10: T2 SchemaInvalidError -> t1_fallback_t2 ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_raises=SchemaInvalidError("malformed"),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)
    all_ok &= _print(
        "10. T2 schema-invalid -> t1_fallback_t2",
        result.action == "Buy"
        and result.tier_provenance == "t1_fallback_t2"
        and budget.used == 1,
        f"prov={result.tier_provenance}",
    )

    # --- Path 11: clients.t2 is None -> t1_only regardless of gates ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_present=False,
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(), clients, budget)  # gates would normally fire
    all_ok &= _print(
        "11. T2 absent -> t1_only regardless of gates",
        result.action == "Buy"
        and result.tier_provenance == "t1_only"
        and budget.used == 0,
        f"prov={result.tier_provenance}",
    )

    # --- Bonus: catalyst present but not high-quality -> no escalation ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(
        _ctx(catalyst_flags=("low_quality_rumor",)), clients, budget
    )
    all_ok &= _print(
        "12. low-quality catalyst -> no escalation -> t1_only",
        result.tier_provenance == "t1_only"
        and clients.t2.evaluate.call_count == 0,
        f"prov={result.tier_provenance}",
    )

    # --- Bonus: pm_rvol below threshold -> no escalation ---
    clients = _make_clients(
        t1_returns=_decision(action="Buy", confidence=65),
        t2_returns=_decision(action="Buy", confidence=80),
    )
    budget = EscalationBudget(max_per_day=25)
    result = await evaluate(_ctx(pm_rvol=2.0), clients, budget)
    all_ok &= _print(
        "13. pm_rvol below threshold -> no escalation -> t1_only",
        result.tier_provenance == "t1_only"
        and clients.t2.evaluate.call_count == 0,
        f"prov={result.tier_provenance}",
    )

    print()
    print("ALL OK" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
