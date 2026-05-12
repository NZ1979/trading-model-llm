# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

Two project-specific docs in the repo root contain non-negotiable context — read them before any operational instruction:

- `CLAUDE_PREFLIGHT.md` — 22 numbered rules covering credential handling (Rules 21–22), credential-leak prevention in logs (URL-based auth + httpx logging), execution-context labelling for every command block (Rule 16), placeholder/assumption auditing (Rule 20), fail-loud error handling (Rule 18), and verification-before-conclusion (Rule 14). These are corrections to prior failures; treat them as hard requirements, not style suggestions.
- `PROJECT_BLUEPRINT.md` — the running platform's deployment state, locked-in vendor stack, daily timeline, signal logic, and "do not re-debate" facts (e.g. Polygon Stocks Starter is 15-min delayed and used for historical only; Databento was canceled).
- `docs/LLM_MODEL_CHARTER.md` and `docs/LLM_SIGNAL_INTERFACE.md` — what makes this fork different from the base. The fork replaces the rule-based signal engine with a tiered LLM signal generator.

## What this repo is

This is a fork of `trading-platform` (added as git remote `upstream`). The base codebase provides shared infrastructure (data feeds, bracket order execution, ATR stops, news+sentiment pipeline). This fork replaces only `strategy/signals/` with an LLM-driven signal generator. To inherit base bug fixes: `git fetch upstream && git merge upstream/main`. Conflicts in `strategy/signal_engine.py` or `strategy/signals/` resolve in favor of the fork.

Currently `llm.enabled: false` in `config/settings.yaml`; the inherited rule-based signals (`strategy/signals/gap_and_go.py`, `pullback.py`) still run. The LLM signal engine code in `strategy/llm/` exists but is not yet wired into `main.py`'s evaluation loop.

## Architecture

Asyncio orchestrator pattern. `main.py` boots subsystems and wires their callbacks:

```
News firehose (Alpaca WS) → keyword filter → queue → Haiku batch (60s) → SQLite sentiment
Equity bars (Alpaca SIP WS) → bar aggregator (1m→5m) → on_5min_bar handler
Daily routine (timer) → Polygon REST → DailyContext + PremarketContext per symbol

For each new 5-min RTH bar:
  on_5min_bar(bar)
    → compute_intraday_indicators(symbol_df)
    → generate_signal(df, daily_ctx, premarket_ctx)   # rule-based today; LLM-replaced in M3
    → latest_sentiment(db, ticker, max_age=86400)
    → evaluate_trade(ticker, sentiment, tech, walls)  # walls=None since Databento canceled
    → if Buy/Sell: validate_order → submit_bracket_order → log to SQLite
```

Module layout:

- `data/` — feed adapters: Alpaca SIP bars, Alpaca News, Polygon REST (historical), Polygon News, Finnhub (earnings calendar), Databento (dormant). `bar_aggregator.py` rolls 1m→5m. `watchlist_builder.py` builds dynamic watchlists (Phase B). `pm_rvol_thresholds.py` is the per-ticker pre-market RVOL threshold table (Phase C).
- `analysis/` — `indicators.py` (SMA/EMA/RSI/MACD/Bollinger/ADX/VWAP + `generate_signal` dispatcher), `sentiment.py` (Haiku batch scorer with prompt caching), `futures_walls.py` (dormant).
- `strategy/` — `signal_engine.py` (`evaluate_trade` combiner that merges technical+sentiment+walls into a Buy/Sell/Hold), `signals/` (rule-based gap-and-go and pullback), `risk.py` (`validate_order` enforces 20%/90%/2% caps + ATR-based stop sizing), `llm/` (the new tiered LLM signal generator — see below).
- `execution/alpaca_orders.py` — bracket order submission with 30s equity cache, error handling per Bug D/E.
- `journal/eod_report.py` — markdown EOD report writer (15:55 ET flatten, 16:30 ET journal).
- `scripts/` — diagnostics, verifiers, and one-off data builders. `verify_*.py` scripts validate single modules in isolation; `analyze_shadow_outcomes.py` aggregates post-backfill shadow analytics.
- `tests/` — pytest suite, named per-bug (`test_bug_a_fix.py` through `test_bugs_d_and_e.py`) and per-feature (`test_pm_rvol_thresholds.py`, `test_watchlist_universe_parameterization.py`).

### The LLM signal generator (`strategy/llm/`)

Three-tier architecture, per `docs/LLM_SIGNAL_INTERFACE.md`:

- **Tier 1** (every candidate, hot path): Qwen 3.6-27B local via LM Studio when the workstation arrives; Haiku stand-in (`backend: haiku_stand_in`, pinned to `claude-haiku-4-5`) during the bridge.
- **Tier 2** (selective escalation, ~5–25/day): Sonnet 4.5. Triggered by `escalation.escalation_rule` when T1 confidence is in `[confidence_floor, confidence_ceiling]` (default 50–75) AND a pre-market RVOL gate fires. Budget consumed on *attempt*, not success, so flaky endpoints can't blow the daily cap on retries.
- **Tier 3** (offline only, never live): Opus. Used by the M2 replay harness and weekly audit to label decisions as gold standard.

`signal_engine.evaluate(ctx, clients, budget)` is the live entry point — it must never raise; every failure becomes a synthetic Hold with the failure mode encoded in `setup_label` (`schema_invalid_t1`, `api_failure_t1`, `t1_unexpected`). `merge_tiers(t1, t2)` resolves T1+T2 disagreement (disagreement collapses to Hold). `tier_provenance` on `LLMDecision` records which tier(s) contributed.

`factory.build_tier_clients(llm_config)` is the only place that maps config strings to client classes; `haiku_stand_in` enforces the model pin so accidental Sonnet bills can't sneak in via the T1 path. `LocalClient` (Qwen) raises `NotImplementedError` until the workstation is online — silent substitution to Anthropic is deliberately avoided.

When changing fields on `LLMContext`/`LLMDecision` (`strategy/llm/types.py`), bump `prompt_version` in `config/settings.yaml` so cached responses don't get mis-parsed, and update `docs/LLM_SIGNAL_INTERFACE.md` § "Input context structure" / "Output schema".

## Commands

All from `C:\trading\LLM model` in PowerShell unless noted.

```powershell
# Activate venv (once per shell)
.\.venv\Scripts\Activate.ps1

# Install deps (base + LLM additive layer)
pip install -r requirements.txt -r requirements-llm.txt

# Syntax-check a file (mandatory pre-deploy gate per WAVE_DEPLOY_CHECKLIST Gate A)
python -m py_compile main.py
python -m py_compile strategy/llm/signal_engine.py

# Run full test suite
python -m pytest tests/ -v

# Run one test file or one test
python -m pytest tests/test_bug_a_fix.py -v
python -m pytest tests/test_pm_rvol_thresholds.py::test_specific_function -v

# Verify a single subsystem in isolation (no network calls beyond what's needed)
python -m scripts.verify_signal_engine
python -m scripts.verify_prompts
python -m scripts.verify_llm_factory
python -m scripts.verify_anthropic_client
python -m scripts.verify_metrics

# verify_alpaca.py makes a REAL network call to paper-api.alpaca.markets
# Requires ALPACA_API_KEY and ALPACA_API_SECRET in the environment.
# Use after rotating Alpaca credentials to prove the new pair works.
python scripts/verify_alpaca.py
```

VPS deploy is a separate workflow — see `docs/WAVE_DEPLOY_CHECKLIST.md` for the gated procedure (code review → logging audit → credential surface audit → execution-context label audit → placeholder audit → pre-flight → atomic deploy with `py_compile` ON the VPS → post-restart credential-leak grep → 24h soak). Production lives at `5.161.199.155` (`/opt/trader/app/`, systemd unit `trader.service`).

## Conventions

- All times in `config/settings.yaml` `schedule:` are America/New_York; the orchestrator handles DST.
- Secrets are read from environment variables only — never `settings.yaml`. On the VPS they live in `/etc/trading-platform/env` (mode 0600). See PROJECT_BLUEPRINT.md § 10.
- The Anthropic SDK uses `httpx`, which logs full request URLs at INFO by default. Polygon passes the API key as a URL query param. The logger-suppression block in `setup_logging` (sets `httpx`, `httpcore`, `aiohttp`, `anthropic`, `urllib3` to WARNING) MUST stay in place — removing it leaks credentials into journalctl (Rule 22 trap, May 2026).
- The platform is paper-only by config (`broker.mode: paper`). Never flip to `live` without an explicit user request and a separate confirmation pass.
- Patches and audits are journaled as dated files in `docs/patches/<YYYY-MM-DD>-*.md` and `docs/audits/`. Each documents the trap, the fix, and the verification.
- Bug-specific tests live alongside their fixes and stay in the suite as regression guards (`test_bug_a_fix.py` etc.).

## Notes on the OneDrive trap

The project root sits under `C:\trading\` deliberately to avoid OneDrive sync. OneDrive locks `.git/index.lock` and breaks `py_compile` via FUSE staleness on large files. If `py_compile` of `main.py` fails on Windows but succeeds on the VPS, suspect OneDrive — not the code.
