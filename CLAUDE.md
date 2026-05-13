# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

Two project-specific docs in the repo root contain non-negotiable context — read them before any operational instruction:

- `CLAUDE_PREFLIGHT.md` — 26 numbered rules covering credential handling (Rules 21–22), credential-leak prevention in logs (URL-based auth + httpx logging), execution-context labelling for every command block (Rule 16), placeholder/assumption auditing (Rule 20), fail-loud error handling (Rule 18), verification-before-conclusion (Rule 14), bash-mount-staleness on Windows-side files (Rule 24), session-anchor verification at the start of every chat — date/time, working directory `C:\trading\LLM model`, workstation "Godzilla" (Rule 25), and the hard partition between the LLM-model fork (Godzilla) and the gap-and-go fork (old laptop + VPS at `5.161.199.155`) — no cross-fork SSH, DB access, repo ops, or operational context (Rule 26). These are corrections to prior failures; treat them as hard requirements, not style suggestions.
- `PROJECT_BLUEPRINT.md` — the running platform's deployment state, locked-in vendor stack, daily timeline, signal logic, and "do not re-debate" facts (e.g. Polygon Stocks Starter is 15-min delayed and used for historical only; Databento was canceled).
- `docs/LLM_MODEL_CHARTER.md` and `docs/LLM_SIGNAL_INTERFACE.md` — what makes this fork different from the base. The fork replaces the rule-based signal engine with a tiered LLM signal generator.

## Hard rules — quoted verbatim from CLAUDE_PREFLIGHT.md

These are not stylistic preferences. Each one was added after a specific failure cost real time and trust. The full rationale, trap history, and "how to apply" notes are in `CLAUDE_PREFLIGHT.md`; this section is the loud-and-visible version so the rules can't be skipped because the file wasn't opened.

### Rule 14: Verification before conclusion

NEVER present a diagnostic claim, root cause, or fix as a conclusion until it has been tested and verified against real data or output **in this session**. Until verified, mark every finding explicitly as `HYPOTHESIS:` or `UNVERIFIED:` in the message itself. End-to-end claims require end-to-end execution, not module-level inference.

### Rule 16: Always state where a command/script is to be run

Every command block must declare its execution context explicitly, before the code, with no ambiguity. No naked code blocks. When switching shells between consecutive blocks (e.g., from local PowerShell into an SSH session, or back out), say it loudly — "open a NEW PowerShell window — do NOT use the open SSH session" — before the next code block. The context label alone is not enough on its own.

### Rule 18: Error handling — fail loud, never fake

Priority order, top to bottom: 1) Works correctly with real data; 2) Falls back visibly with a banner/log warning/annotated status; 3) Fails with a clear error message (exception, non-zero exit, "FAILED" log line); 4) Silently degrades to look "fine" — **NEVER do this**. Never substitute placeholder data, swallow exceptions silently, or hide failures behind aggregated success counts.

### Rule 20: Audit output for placeholders and unstated assumptions

Before sending a command/script to the user, scan for: literal placeholder strings (`<paste-your-key>`, `REPLACE_ME`), unstated input assumptions (HOW/WHERE did they save it?), path assumptions, tool-availability assumptions (is `python` on PATH?), and state assumptions (is the service running, env var exported, prior step completed?). Either resolve them with concrete values (asking first if needed) or call them out explicitly. **Project-specific corollary**: the Hetzner VPS at `5.161.199.155` is NOT necessarily the LLM model's deployment. As of 2026-05-12 it runs the gap-and-go fork (`llm.enabled: false`) on account `PA3REQ1LMPKO`. The Large Cap account `PA3QAZ941NFN` is reserved for when the LLM model eventually deploys, on the workstation or a separate VPS, and must not co-exist with gap-and-go on a single `trader.service`. Verify which fork's code runs where before recommending any infrastructure action.

### Rule 21: Never request command output that would expose credentials

Before asking the user to paste back the output of any command, ask: "Could the output contain a secret?" If yes, redesign the command to extract only the non-secret information. Length checks (`awk '{print length($2)}'`), existence counts (`grep -c`), last-4-chars fingerprints, hash prefixes — never the raw value. Applies to `cat`/`tail`/`head`/`grep`-without-redaction, `env`/`printenv`/`Get-ChildItem env:`, process listings, service unit files (`systemctl cat`), and any log dump that might include credentials in URLs or headers.

### Rule 22: Audit logging behavior for credential leaks

`httpx` (and the `anthropic` SDK on top of it) logs full URLs at INFO by default. Polygon passes its API key as a URL query param. The `setup_logging` block forcing `httpx, httpcore, aiohttp, anthropic, urllib3` loggers to `WARNING` MUST stay in place — removing it leaks credentials into journalctl on the next deploy. Any new HTTP-client dependency triggers a fresh audit of its default logging behavior before the deploy that introduces it ships.

### Rule 23: Verify actual system date/time before any time-anchored claim

Before ANY statement that includes "today", "tomorrow", "this morning", "market is open/closed", "we have N hours", a deadline, or a market-session reference, run `date && TZ=America/New_York date` and reason from the fresh values. Session env headers drift over long sessions; remembered framing from earlier in the conversation goes stale. State the verified time inline so the claim is auditable: e.g., "It is now Tue 10:33 AM EDT; market has been open for 1h 3m." Trading-platform market hours are 09:30–16:00 ET on weekdays excluding US market holidays.

### Rule 24: The Cowork bash mount can serve stale snapshots of Windows-side files

The file tools (Read/Write/Edit) and the bash sandbox mount at `/sessions/<id>/mnt/LLM model/` can disagree about file content for paths under `C:\trading\LLM model\`. The bash mount can be hours stale even after a successful Edit returns "file updated." NEVER claim "verified on disk" based on a Read spot-check; Read and Edit share the same in-process view, so a clean Read after a clean Edit proves only that the in-process buffer is consistent, not that the Windows disk received the write. NEVER run `git add`/`commit`/`push` from the bash sandbox against Windows-side files. Verification of disk state for Windows-side files runs from PowerShell on the user's workstation: `(Get-Content <file> | Measure-Object -Line).Lines` plus `Get-Content -Tail 5`, compared against the Edit-tool's expected values. Bash `sync` does not refresh the mount. Include `Remove-Item .git\index.lock -ErrorAction SilentlyContinue` in any PowerShell commit block since the bash sandbox often leaves a 0-byte `.git/index.lock` that Windows-side git operations cannot work around until cleared.

### Rule 25: Verify session anchors at the start of every new chat or task

Three anchors silently drift between sessions: current date/time, project working directory, and physical workstation. Before recommending any command, edit, or operational step, confirm all three and state them inline: (1) run `date && TZ=America/New_York date` per Rule 23 and quote the verified ET time; (2) confirm the working directory is `C:\trading\LLM model` (the LLM fork — distinct from the upstream base and from `/opt/trader/app/` on the VPS); (3) confirm the local workstation is "Godzilla", the new box intended to host the tier-1 Qwen LLM via LM Studio. Godzilla is NOT the Hetzner VPS at `5.161.199.155` (that runs the gap-and-go fork on account `PA3REQ1LMPKO` with `llm.enabled: false`, per Rule 20). Recommendations involving local GPU, LM Studio, CUDA, or workstation `.venv` apply to Godzilla; recommendations involving systemd, journalctl, or `/etc/trading-platform/env` apply to the VPS. Label every command block per Rule 16 with which machine it runs on. Opening line of a session should look like: "Verified: Wed 2026-05-13 09:21 EDT; working in `C:\trading\LLM model`; workstation Godzilla."

### Rule 26: Hard partition between LLM-model (Godzilla) and gap-and-go (old laptop + VPS)

The LLM-model fork (this repo, `C:\trading\LLM model\` on Godzilla, account `PA3QAZ941NFN` reserved for future deploy) and the gap-and-go fork (old laptop dev root + Hetzner VPS at `5.161.199.155`, account `PA3REQ1LMPKO`, currently paper-trading with `llm.enabled: false`) are operationally separate. From any session anchored in `C:\trading\LLM model\`: NEVER SSH to the VPS, NEVER read/query/copy/modify `/opt/trader/app/trading.db`, NEVER run LLM-fork scripts (anything under `scripts/`, including `backfill_shadow_outcomes.py`, `analyze_shadow_outcomes.py`, `verify_*.py`) against VPS data, NEVER commit/push/pull/fetch against the gap-and-go repo, NEVER use gap-and-go operational state (decisions, orders, balances, journalctl) as context for LLM-model work. Symmetric prohibition applies from gap-and-go sessions toward Godzilla and the LLM-model repo. The only legitimate crossing is the planned future LLM-model deploy to account `PA3QAZ941NFN`, which gets its own session, its own checklist, its own atomic cutover. Treat the strings `5.161.199.155`, `/opt/trader/app/`, `hetzner_trader`, `PA3REQ1LMPKO`, `trader.service`, `trader-prod` as tripwires inside an LLM-model session — their appearance in a command or recommendation means a partition check is required before sending. If realistic data is needed for LLM-fork dev (testing shadow_outcomes, validating `policy.py`), use synthesized fixtures or a deliberately-curated local sample DB — never the gap-and-go production DB.

---

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
