"""Simulation utilities for the M2 replay harness.

The replay harness runs two parallel simulated portfolios — one driven
by the LLM signal engine's merged decisions, one driven by the base
rule-based signals — over the same historical bar+news stream. These
modules implement the simulation primitives:

- ``portfolio``: ``SimulatedPortfolio`` — cash + positions bookkeeping,
  mark-to-market, realized-P&L tracking, max-drawdown tracking.
- ``fills``: ``SimulatedFill`` dataclass + ``simulate_fill`` — entry/exit
  price determination with configurable slippage and fill timing.
- ``comparison`` (M2.3): metrics comparing the two portfolios.
- ``tier_analysis`` (M2.3): Tier 1 vs Tier 3 agreement metrics for the
  report's § 5d.

Per ``docs/M2_REPLAY_HARNESS_DESIGN.md``.
"""
