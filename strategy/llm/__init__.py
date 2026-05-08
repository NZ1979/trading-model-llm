"""LLM-based signal generator subsystem (Tier 1 + Tier 2 + Tier 3).

This package implements the tiered LLM evaluation architecture documented
in ``docs/LLM_SIGNAL_INTERFACE.md``:

- **Tier 1** (``LocalClient``): Qwen 72B local via LM Studio, hot path,
  every candidate every cycle. During the pre-workstation bridge period
  Tier 1 is served by ``AnthropicClient`` with model
  ``claude-haiku-4-5`` (the "haiku_stand_in" backend).
- **Tier 2** (``AnthropicClient``): Sonnet 4.5 selective escalation
  fired by ``escalation.escalation_rule``.
- **Tier 3** (``AnthropicClient``): Opus 4.6 offline gold-standard
  labeler. Not in the live signal path — used by the M2 replay harness
  and the weekly live-decision audit job.

Public entry point is ``signal_engine.evaluate``. Everything else is
internal to this package.
"""

from .types import LLMContext, LLMDecision

__all__ = ["LLMContext", "LLMDecision"]
