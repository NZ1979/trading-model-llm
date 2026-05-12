"""Verification script for strategy.llm.factory (step 4).

Loads settings.yaml from disk, exercises every factory branch with
config variants, and confirms:
- Tier 1 (haiku_stand_in) constructs an AnthropicClient pinned to claude-haiku-4-5
- Tier 2 (anthropic + sonnet) constructs an AnthropicClient
- Tier 3 disabled returns None
- Tier 3 enabled (anthropic + opus) constructs an AnthropicClient
- haiku_stand_in pinned to wrong model is rejected
- qwen_local raises NotImplementedError loudly (workstation-only)
- enabled=false at top level is rejected
- EscalationBudget reads max_per_day correctly, returns 0-cap when t2 disabled

Run with:
    cd C:\\trading\\LLM model
    $env:PYTHONPATH = "."
    python scripts/verify_llm_factory.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from strategy.llm.clients import AnthropicClient, LocalClient
from strategy.llm.escalation import EscalationBudget
from strategy.llm.factory import build_escalation_budget, build_tier_clients


def _print(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _enabled_default_config() -> dict:
    """The settings.yaml default with enabled flipped to true so the
    factory will actually build. Mirrors what main.py will do once the
    signal engine is wired."""
    return {
        "enabled": True,
        "prompt_version": "v0.0-stub",
        "t1": {
            "backend": "haiku_stand_in",
            "model_id": "claude-haiku-4-5",
            "timeout_s": 30,
            "max_tokens": 1024,
        },
        "t2": {
            "enabled": True,
            "backend": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "timeout_s": 30,
            "max_tokens": 1024,
            "max_per_day": 25,
            "confidence_floor": 50,
            "confidence_ceiling": 75,
            "pm_rvol_min": 3.0,
        },
        "t3": {
            "enabled": False,
            "backend": "anthropic",
            "model_id": "claude-opus-4-6",
            "timeout_s": 60,
            "max_tokens": 1024,
            "sample_rate": 1.0,
        },
    }


def main() -> int:
    all_ok = True

    # ---- Load settings.yaml from disk and confirm the llm: block parses ----
    settings_path = Path("config/settings.yaml")
    with open(settings_path) as f:
        full_cfg = yaml.safe_load(f)

    all_ok &= _print(
        "settings.yaml has llm: block",
        "llm" in full_cfg and isinstance(full_cfg["llm"], dict),
    )
    llm_cfg_disk = full_cfg["llm"]
    all_ok &= _print(
        "settings.yaml llm.enabled defaults to false",
        llm_cfg_disk.get("enabled") is False,
        "intentional: live signal engine isn't wired yet",
    )
    all_ok &= _print(
        "settings.yaml llm.t1.backend = haiku_stand_in",
        llm_cfg_disk["t1"]["backend"] == "haiku_stand_in",
    )
    all_ok &= _print(
        "settings.yaml llm.t1.model_id = claude-haiku-4-5",
        llm_cfg_disk["t1"]["model_id"] == "claude-haiku-4-5",
    )

    # ---- enabled=false should be rejected ----
    try:
        build_tier_clients(llm_cfg_disk)
        all_ok &= _print("enabled=false rejected", False, "no exception")
    except ValueError as exc:
        all_ok &= _print(
            "enabled=false rejected",
            "enabled is false" in str(exc),
            f"{str(exc)[:80]}",
        )

    # ---- Happy path: enabled config builds T1+T2, T3=None ----
    cfg = _enabled_default_config()
    clients = build_tier_clients(cfg)
    all_ok &= _print(
        "enabled config builds T1 as AnthropicClient(haiku)",
        isinstance(clients.t1, AnthropicClient)
        and clients.t1.model_id == "claude-haiku-4-5"
        and clients.t1.backend == "anthropic",
        f"t1.model_id={clients.t1.model_id}",
    )
    all_ok &= _print(
        "enabled config builds T2 as AnthropicClient(sonnet)",
        isinstance(clients.t2, AnthropicClient)
        and clients.t2.model_id == "claude-sonnet-4-5",
        f"t2.model_id={clients.t2.model_id if clients.t2 else None}",
    )
    all_ok &= _print(
        "T3 disabled returns None",
        clients.t3 is None,
    )

    # ---- T3 enabled builds Opus client ----
    cfg_t3 = _enabled_default_config()
    cfg_t3["t3"]["enabled"] = True
    clients_t3 = build_tier_clients(cfg_t3)
    all_ok &= _print(
        "T3 enabled builds AnthropicClient(opus)",
        isinstance(clients_t3.t3, AnthropicClient)
        and clients_t3.t3.model_id == "claude-opus-4-6"
        and clients_t3.t3.timeout_s == 60,
        f"t3.model={clients_t3.t3.model_id} timeout={clients_t3.t3.timeout_s}",
    )

    # ---- haiku_stand_in pinned to wrong model -> rejected ----
    cfg_bad = _enabled_default_config()
    cfg_bad["t1"]["model_id"] = "claude-sonnet-4-5"
    try:
        build_tier_clients(cfg_bad)
        all_ok &= _print(
            "haiku_stand_in with wrong model rejected", False, "no exception"
        )
    except ValueError as exc:
        all_ok &= _print(
            "haiku_stand_in with wrong model rejected",
            "haiku_stand_in" in str(exc) and "claude-haiku-4-5" in str(exc),
            f"{str(exc)[:100]}",
        )

    # ---- qwen_local raises NotImplementedError (placeholder) ----
    cfg_local = _enabled_default_config()
    cfg_local["t1"]["backend"] = "qwen_local"
    cfg_local["t1"]["model_id"] = "qwen3.6-27b-instruct-q4"
    try:
        build_tier_clients(cfg_local)
        all_ok &= _print(
            "qwen_local placeholder raises", False, "no exception"
        )
    except NotImplementedError as exc:
        all_ok &= _print(
            "qwen_local placeholder raises NotImplementedError",
            "workstation" in str(exc).lower(),
            f"{str(exc)[:80]}",
        )

    # ---- Unknown backend rejected ----
    cfg_unknown = _enabled_default_config()
    cfg_unknown["t1"]["backend"] = "made_up_backend"
    try:
        build_tier_clients(cfg_unknown)
        all_ok &= _print("unknown backend rejected", False, "no exception")
    except ValueError as exc:
        all_ok &= _print(
            "unknown backend rejected",
            "unknown backend" in str(exc),
            f"{str(exc)[:80]}",
        )

    # ---- EscalationBudget: T2 enabled reads max_per_day ----
    budget = build_escalation_budget(cfg)
    all_ok &= _print(
        "EscalationBudget reads max_per_day from t2",
        isinstance(budget, EscalationBudget) and budget.max_per_day == 25,
        f"max_per_day={budget.max_per_day}",
    )

    # ---- EscalationBudget: T2 disabled returns 0-cap ----
    cfg_no_t2 = _enabled_default_config()
    cfg_no_t2["t2"]["enabled"] = False
    budget_disabled = build_escalation_budget(cfg_no_t2)
    all_ok &= _print(
        "EscalationBudget T2-disabled returns 0-cap",
        budget_disabled.max_per_day == 0
        and not budget_disabled.has_capacity(),
        f"max_per_day={budget_disabled.max_per_day} has_cap={budget_disabled.has_capacity()}",
    )

    # ---- T2 disabled -> clients.t2 is None ----
    clients_no_t2 = build_tier_clients(cfg_no_t2)
    all_ok &= _print(
        "T2 disabled returns None",
        clients_no_t2.t2 is None and clients_no_t2.t1 is not None,
    )

    print()
    print("ALL OK" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
