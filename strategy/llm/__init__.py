"""Shared LLM-strategy utilities.

In the base trading-platform repo, this package contains only ``metrics``
(pure-deterministic forward-return / MAE-MFE / Calmar math for shadow
analytics). The full LLM signal generator (types, clients, escalation,
merge, signal_engine, prompts) lives in the trading-model-llm fork.

This package is the home of the metrics module so that both forks
(gap-and-go, llm) inherit identical shadow-analytics utilities via
``git fetch upstream``. The metrics module is signal-source-agnostic;
it computes outcomes against any (decision, bars) pair regardless of
whether the decision came from rule-based logic or an LLM.
"""
