# trading-model-llm

LLM-driven signal generation for intraday equity trading. Forked from
[trading-platform](https://github.com/NZ1979/trading-platform) at tag
`v0.9-pre-phase-c-deploy`.

## What this fork does differently from the base

| Aspect | Base (trading-platform) | This fork (LLM model) |
|---|---|---|
| Signal generation | Technical indicators (gap-and-go, pullback) computed from price/volume | Claude (or other LLM) reads market context + news + indicators and decides Buy/Sell/Hold with reasoning |
| Sentiment role | Filter on top of technical signal (sentiment ≥ 3 to allow Buy) | Sentiment is one of many inputs the LLM weighs holistically; not a hard gate |
| Setup library | Fixed: gap-and-go, pullback (mean-reversion-in-trend) | Dynamic: the LLM identifies setups from context, not from a hard-coded rule list |
| Why | Reference implementation; deterministic; testable in isolation | Explores whether an LLM's holistic judgment outperforms rule-based intraday strategies. The base codebase already uses Claude Haiku for sentiment scoring — this fork extends Claude's role from "score this headline" to "decide what to do." |

The two repos share infrastructure (data feeds, bracket order execution,
risk validation, deploy procedures, ATR stops, Phase C per-ticker PM
RVOL thresholds, all the Bug F/G/H fixes). The fork diverges
intentionally on:

1. The signal-engine layer (`strategy/signal_engine.py` + `strategy/signals/`) will be replaced with an LLM-based signal generator that calls Claude per-evaluation.
2. README content (this file) + a project charter at `docs/LLM_MODEL_CHARTER.md`.

That's the planned divergence. As of the initial fork commit, no code
has been changed yet — the inherited reference signals (gap-and-go +
pullback) still run. The first divergence commits will land as the LLM
strategy is designed and built.

## Why a separate repo instead of a config flag in the base

Two reasons:

1. **Strategy divergence, not configuration.** The LLM-as-signal-generator approach has fundamentally different testing requirements (LLM evals, prompt regression suites, output schema validation) and operational characteristics (latency budget for LLM calls, fallback behavior on API failure, cost tracking per signal evaluation). Embedding all that behind a feature flag would entangle two very different code paths.

2. **Cross-pollination via `git pull upstream`**. The base repo is set up as `upstream` here; when bug fixes or shared improvements land in `trading-platform`, this fork can `git fetch upstream && git merge upstream/main` to inherit them without automatically inheriting any base-specific strategy decisions.

## Setup

Same as the base — see [SETUP_NEW_MACHINE.md](SETUP_NEW_MACHINE.md).
The infra (Alpaca, Polygon, Anthropic, Finnhub, Hetzner VPS) is
identical to the base. The Anthropic API key gets exercised more
heavily here (one full-context Claude call per signal evaluation, vs
one cheap headline-scoring call per news item in the base). Cost
implications are tracked in [LLM_MODEL_CHARTER.md](docs/LLM_MODEL_CHARTER.md).

## Pulling base improvements

When bug fixes land in `trading-platform`:

```powershell
cd "C:\trading\LLM model"
git fetch upstream
git log HEAD..upstream/main --oneline   # what's new in the base
git merge upstream/main                  # pull them in
git push                                 # push to this fork's origin
```

If a base change conflicts with the fork's strategy-specific overrides
(typically the signal_engine/signals modules), resolve in favor of the
fork's version and re-confirm py_compile and tests pass.

## Status

Pre-development. As of the fork creation, no LLM-specific code exists
yet. The next milestones are documented in [LLM_MODEL_CHARTER.md](docs/LLM_MODEL_CHARTER.md):

- Define the LLM signal-generator interface (input schema, output schema, fallback behavior)
- Build a sandbox harness that replays historical bars + news against a Claude prompt and produces decisions
- Backtest the LLM signal generator against the same period the base ran
- Compare results (win rate, average win/loss, Sharpe, drawdown) before deciding whether to deploy

## License / status

Private. Paper trading only on Alpaca. Not financial advice, not for
distribution.
