"""Per-run cost budget for the Tier 3 (Opus) labeling pass.

Mirrors the role ``strategy.llm.escalation.EscalationBudget`` plays for
Tier 2: caller-owned, threaded through ``run_replay``, reset semantics
managed by the caller. Distinct from EscalationBudget because:

- T3 budget is per-run (not per-day); the design doc gives a per-run
  cap (``ReplayConfig.t3_max_dollars_per_run``).
- T3 tracks dollars, not calls. Opus pricing is non-trivial.
- T3 has TWO skip reasons (budget vs sample-rate) that the summary
  report wants distinguished.

Cost estimation is intentionally crude in this sub-task (M2.2 #20):
a fixed ``per_call_estimate`` (default 0.05 USD, conservative vs the
design doc's ~$0.003/call after caching) lets us enforce the cap
loud-and-early without depending on per-call token usage. Refinement
to actual post-call usage is a future sub-task once the
``AnthropicClient`` exposes a ``last_call_cost`` hook.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class T3Budget:
    """Cost-cap tracker for the Tier 3 Opus pass.

    Attributes:
        cap_dollars: hard upper bound on cumulative estimated cost.
            When the next call would exceed this, the caller is
            expected to invoke ``record_skip_budget`` and not place
            the call (Rule 18: visible degradation via the counter,
            not silent throttling).
        per_call_estimate: pre-call cost estimate in USD. Default
            0.05 is conservative -- the design doc projects ~$0.003
            per call after prompt caching, so a 100% over-estimate
            keeps the cap safely above actual usage on the first
            cache-miss-heavy run. Override via the CLI for a tuned
            value.
        used_dollars: cumulative estimated cost recorded so far.
        n_calls: number of T3 calls successfully placed.
        n_skipped_budget: candidates that would have been sampled
            but the budget was exhausted.
        n_skipped_sample: candidates that the sample-rate gate
            filtered out before reaching the budget check.

    The counters are summary-report inputs (``summary_json`` in
    ``replay_runs``) and persist to the DB at end-of-run.
    """

    cap_dollars: float
    per_call_estimate: float = 0.05
    used_dollars: float = 0.0
    n_calls: int = 0
    n_skipped_budget: int = 0
    n_skipped_sample: int = 0

    def has_capacity(self) -> bool:
        """True iff the next call's estimate fits under ``cap_dollars``."""
        return self.used_dollars + self.per_call_estimate <= self.cap_dollars

    def record_call(self) -> None:
        """Increment ``used_dollars`` by ``per_call_estimate`` and bump
        ``n_calls``. Called after a successful T3 invocation."""
        self.used_dollars += self.per_call_estimate
        self.n_calls += 1

    def record_skip_budget(self) -> None:
        """A candidate was sampled in but skipped because the budget
        cap would have been exceeded. Caller logs WARNING; this just
        bumps the counter."""
        self.n_skipped_budget += 1

    def record_skip_sample(self) -> None:
        """A candidate was rejected by the deterministic sample-rate
        gate before the budget check ran."""
        self.n_skipped_sample += 1


__all__ = ["T3Budget"]
