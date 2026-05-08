"""Build TierClients and EscalationBudget from parsed settings.yaml.

The signal_engine doesn't read config directly — main.py (and the M2
replay harness) pass it the already-constructed clients and budget.
This module is the only place that maps config strings to concrete
client classes.

Backend mapping:

- ``haiku_stand_in``: pre-workstation bridge. Routes to ``AnthropicClient``
  with model pinned to ``claude-haiku-4-5``. The pin is enforced here —
  if config tries to use a different model under this backend, we
  reject it loudly so accidental Sonnet bills don't sneak in via the
  Tier 1 path.
- ``anthropic``: real Anthropic API call with whatever ``model_id`` the
  config specifies (used by Tier 2 Sonnet, Tier 3 Opus).
- ``qwen_local``: ``LocalClient`` (LM Studio). Currently raises
  ``NotImplementedError`` on construction until the workstation arrives.
"""
from __future__ import annotations

from typing import Any

from .clients import AnthropicClient, LLMClient, LocalClient
from .escalation import EscalationBudget
from .signal_engine import TierClients

_HAIKU_STAND_IN_MODEL = "claude-haiku-4-5"


def build_tier_clients(llm_config: dict[str, Any]) -> TierClients:
    """Construct TierClients from the parsed ``llm:`` section of settings.yaml.

    Tier 1 is required (the live engine cannot run without it). Tiers 2
    and 3 are optional — if their ``enabled`` key is false (or the
    block is absent entirely), the corresponding slot in the returned
    TierClients is None and the signal_engine skips that tier.

    Raises:
        ValueError: missing required keys, or unknown backend, or
            haiku_stand_in pinned to wrong model_id.
    """
    if not llm_config.get("enabled", False):
        raise ValueError(
            "llm.enabled is false in config; refusing to build clients. "
            "Flip llm.enabled to true once the signal engine is wired."
        )

    t1_cfg = llm_config.get("t1")
    if not t1_cfg:
        raise ValueError("llm.t1 section is required when llm.enabled is true")

    t1 = _build_tier_client(t1_cfg, required=True)
    t2 = _build_tier_client(llm_config.get("t2", {}), required=False)
    t3 = _build_tier_client(llm_config.get("t3", {}), required=False)
    return TierClients(t1=t1, t2=t2, t3=t3)


def build_escalation_budget(llm_config: dict[str, Any]) -> EscalationBudget:
    """Read max_per_day from llm.t2 and construct the budget.

    Returns a budget with cap=0 (always rejects) when t2 is disabled,
    so the signal_engine's ``escalation_rule`` short-circuits without
    a separate ``if t2 is None`` branch.
    """
    t2_cfg = llm_config.get("t2", {})
    if not t2_cfg.get("enabled", False):
        return EscalationBudget(max_per_day=0)
    return EscalationBudget(max_per_day=int(t2_cfg.get("max_per_day", 25)))


def _build_tier_client(
    tier_cfg: dict[str, Any],
    *,
    required: bool,
) -> LLMClient | None:
    """Instantiate one tier client from its config block, or None if disabled."""
    if not tier_cfg:
        if required:
            raise ValueError("required tier config is empty")
        return None

    if not required and not tier_cfg.get("enabled", False):
        return None

    backend = tier_cfg.get("backend")
    model_id = tier_cfg.get("model_id")
    if not backend or not model_id:
        raise ValueError(
            f"tier config missing backend or model_id: {tier_cfg}"
        )

    timeout_s = float(tier_cfg.get("timeout_s", 30))
    max_tokens = int(tier_cfg.get("max_tokens", 1024))

    if backend == "haiku_stand_in":
        if model_id != _HAIKU_STAND_IN_MODEL:
            raise ValueError(
                f"haiku_stand_in backend pins model_id to "
                f"{_HAIKU_STAND_IN_MODEL!r}; got {model_id!r}. "
                f"To use a different Anthropic model, set "
                f"backend: anthropic instead."
            )
        return AnthropicClient(
            model_id=model_id,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )

    if backend == "anthropic":
        return AnthropicClient(
            model_id=model_id,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )

    if backend == "qwen_local":
        # Constructor raises NotImplementedError until the workstation
        # is online with LM Studio reachable. Surfacing that error from
        # the factory rather than silently substituting Anthropic is
        # deliberate: misconfigured local deployment should fail loud.
        return LocalClient(model_id=model_id)

    raise ValueError(f"unknown backend: {backend!r}")
