# Scrub and Scaffold Plan — 2026-05-17

**Verified anchors:** Sun 2026-05-17 09:43 EDT, working in `C:\trading\LLM model`, workstation Godzilla.
**Predecessor:** `docs/STRATEGY_RESET_2026-05-16.md` (decision #3: park intraday work; non-viable).
**This plan operationalizes that decision and prepares a clean scaffold for the multi-day catalyst-driven LLM architecture.**

> **Approach updated 2026-05-17 per Neale's direction:** Method changed from "branch-and-purge inside the existing repo" to "migrate preserved files to a new sibling project folder `C:\trading\LLM_SWING_MODEL\` and start fresh there." The legacy intraday repo at `C:\trading\LLM model\` is retained as read-only archive (Rule 26). No `git rm` against the legacy. No destructive operations in this plan. See §1 (revised) and §6 (revised) below.

---

## 1. Method — migrate to new sibling project folder

The active development location moves from `C:\trading\LLM model\` to a fresh sibling at `C:\trading\LLM_SWING_MODEL\`. The legacy folder stays intact, read-only, for reference. The new folder is a clean slate.

**Why this is cleaner than branch-and-purge:**

- **No risk of accidental deletion of legacy reference material.** Branch-and-purge needs `git rm` against ~90 files. The migration approach copies the small preservation set forward and never touches the legacy.
- **The legacy folder remains independently navigable.** When you want to look at how `evaluate_trade()` was structured or how the bracket order client handled Alpaca 422 errors, you `cd C:\trading\LLM model\` and read. No git checkout dance.
- **Fresh git history.** The new project starts with a clean `git init` rather than carrying forward the legacy commit history that includes gap-and-go's evolution. The legacy repo still has its full history at its remote.
- **Visual separation at the filesystem level, not just inside git.** Two folders, two destinies. Cowork session selection (which folder is mounted) becomes the active-project switch.

**What this requires that branch-and-purge didn't:**

- The new folder needs its own git remote. Options at first push: (a) push to a new branch on the existing remote `https://github.com/NZ1979/trading-model-llm.git` (e.g., `swing-main`), or (b) create a new GitHub repo `trading-model-llm-swing.git` and push there. Decision deferred until first commit; recommendation is (b) for clean separation.
- The new folder needs its own `.venv`. Python virtual environments hardcode the parent path; copying the legacy `.venv` is fragile. Fresh `python -m venv .venv` plus `pip install -r requirements.txt` is the safer move.
- The legacy `trading.db` (gap-and-go's data file) is NOT copied to the new project. The new architecture's databases will be created fresh in Phase R2 with the new schemas (vector DB, time-series DB, relational DB). If specific data from the legacy DB ever needs to be lifted forward (e.g., a historical sentiment series), that's a deliberate, narrow-scoped read operation, not a blanket copy.

## 2. Preservation list — files migrated to `C:\trading\LLM_SWING_MODEL\`

These files are copied forward from `C:\trading\LLM model\` to the new project. They are operational rules, hardware documentation, or this week's strategic foundation that the new work builds on. The legacy folder keeps its copies; the new folder gets fresh copies.

### 2a. Operational rules and platform context

| Source file (legacy) | Destination (new) | Notes |
|---|---|---|
| `CLAUDE_PREFLIGHT.md` | **`CLAUDE_PREFLIGHT_SWING.md`** (renamed + edited) | The 27 numbered rules with Rules 9 and 10 reserved, Rules 16/25/26 cross-references updated, historical trap narratives preserved. See `CLAUDE_PREFLIGHT_SWING.md` already written to the legacy folder this session. |
| `docs/HARDWARE_PLATFORM.md` | `docs/HARDWARE_PLATFORM.md` | Godzilla specs, Qwen 3.6-27B target, LM Studio integration. Already LLM-native. Preserve verbatim. |
| `.gitignore` | `.gitignore` | |
| `.gitattributes` | `.gitattributes` | |
| `pyproject.toml` | `pyproject.toml` | Project metadata. Update `name` field to reflect new project name. |

### 2a-bis. Files NOT migrated (created fresh in new folder)

| Legacy path | Why not migrated |
|---|---|
| `.venv/` | Python venv hardcodes parent path. New `python -m venv .venv` in new folder. Reinstall from `requirements.txt`. |
| `.git/` | New project starts with fresh `git init`. Legacy's full history stays at its remote, untouched. |
| `trading.db` | Legacy gap-and-go data. Stays in legacy folder. New project starts with no DBs until Phase R2 creates them. |
| `.env` | 47-byte stub at legacy root. Inspect contents before deciding; likely a placeholder. New `.env` created as needed in new folder. |

### 2b. Strategic foundation (this week's decisions)

| File | Status | Notes |
|---|---|---|
| `docs/STRATEGY_RESET_2026-05-16.md` | Preserve | The new architecture decision doc. |
| `docs/STRATEGY_STATUS_2026-05-16.md` | Preserve | Pre-reset status snapshot. Historical record. |
| `docs/sessions/2026-05-16-strategy-review.md` | Preserve | External-analysis response. |
| `docs/sessions/2026-05-16-m3-handoff.md` | Preserve | Final M3 fix-session handoff. Inert now (M3 is parked) but useful as historical record. |
| `docs/SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md` | Preserve | This document. |

### 2c. Reusable infrastructure (migrated to new folder, refined later)

| Source file (legacy) | Destination (new) | Notes |
|---|---|---|
| `requirements.txt` | `requirements.txt` | Base Python deps. Migrated as-is; pruned in Phase R2 when new modules define actual import needs. |
| `requirements-llm.txt` | `requirements-llm.txt` | LLM-stack deps. Migrated; refined when LM Studio + Qwen + vector DB clients land. |

## 3. Non-migration list — stays in legacy archive only

Every file in this section is NOT copied to `C:\trading\LLM_SWING_MODEL\`. The original copy stays at `C:\trading\LLM model\` as read-only reference (Rule 26). No `git rm`, no deletion, no destructive operation. The legacy folder remains fully browsable.

The "Reason for removal" column below is now "Reason for non-migration" — these files do not belong in the new project's active codebase. They can be inspected in place at the legacy path if a specific pattern needs to be lifted forward (always as a fresh copy with edits, never as a direct migration).

### 3a. The gap-and-go orchestrator and signal layer

| Path | Reason for removal |
|---|---|
| `main.py` | 55KB asyncio orchestrator built around 5-min bar evaluation, EOD flatten at 15:55 ET, gap-and-go watchlist scanning. Entire structure is intraday-shaped. |
| `strategy/signals/gap_and_go.py` | The literal gap-and-go signal. |
| `strategy/signals/pullback.py` | Companion rule-based intraday signal. |
| `strategy/signals/__init__.py` | Package marker for the parked signals. |
| `strategy/signal_engine.py` | `evaluate_trade()` combiner that fuses gap-and-go + pullback + walls into a Buy/Sell/Hold. Intraday cadence. |
| `strategy/llm/signal_engine.py` | LLM signal engine for the intraday architecture. Wrong shape for daily catalyst evaluation. |
| `strategy/llm/policy.py` | Intraday tier-routing policy. |
| `strategy/llm/escalation.py` | Premarket-RVOL-gated T2 escalation logic. Doesn't apply to daily catalyst flow. |
| `strategy/llm/merge.py` | T1+T2 merge logic for intraday. |
| `strategy/llm/metrics.py` | Intraday metrics collection. |
| `strategy/llm/context_builder.py` | Builds 5-min-bar-flavored context. |
| `strategy/llm/prompts.py` | The intraday Hold-default prompts the strategy review flagged. |
| `strategy/llm/types.py` | `LLMDecision` schema with intraday-specific fields (`expected_holding_minutes`). New schema will be catalyst-aware. |
| `strategy/llm/analysis.py` | Intraday analysis fields. |

### 3b. Intraday data feeds

| Path | Reason for removal |
|---|---|
| `data/bar_aggregator.py` | 1-min → 5-min rollup. Not needed for daily/swing strategy. |
| `data/bar_types.py` | `MinuteBar`, `FiveMinBar` dataclasses. |
| `data/watchlist_builder.py` | Gap-and-go's daily dynamic watchlist generator. |
| `data/pm_rvol_thresholds.py` | Per-ticker premarket RVOL gates. Gap-and-go-specific. |
| `data/databento_feed.py` | Dormant Databento integration (futures walls). Subscription was cancelled on 2026-05-17 per §10; the swing model does not use Databento. |
| `data/news_feed.py` | Alpaca News WebSocket. KEEP THE CONCEPT but rewrite for the new arch's news flow. Removing this file forces a clean rewrite rather than accidental reuse. |
| `data/news_pipeline.py` | Sentiment pipeline that batched to Haiku every 60s. Will be rebuilt as a continuous research pipeline. |
| `data/finnhub_feed.py` | Earnings calendar adapter. Concept transfers; file will be rewritten as part of the catalyst detection layer. |
| `data/polygon_feed.py` | Polygon REST client. Will be rebuilt for daily-bar bulk fetch at universe scale, not 5-min backfill. |
| `data/polygon_news.py` | Polygon News REST. Concept transfers; rebuilt. |
| `data/replay/` (entire directory) | Replay infrastructure built for intraday tick replay. New replay harness is daily-bar driven. |
| `data/__init__.py` | Package marker. Will be recreated. |

### 3c. Intraday analysis

| Path | Reason for removal |
|---|---|
| `analysis/indicators.py` | SMA/EMA/RSI/MACD/Bollinger/ADX/VWAP on intraday timeframes. Will be rebuilt for daily timeframes. |
| `analysis/futures_walls.py` | Dormant Databento walls detection. |
| `analysis/sentiment.py` | Haiku batch scorer. Concept transfers; file will be rewritten for the continuous research pipeline. |
| `analysis/__init__.py` | Package marker. |

### 3d. Execution and journal

| Path | Reason for removal |
|---|---|
| `execution/alpaca_orders.py` | Bracket order submission. Concept transfers — bracket orders are the right execution primitive — but the file gets rewritten with GTC support and multi-day position management. Removing forces clean rewrite. |
| `execution/__init__.py` | Package marker. |
| `journal/eod_report.py` | 16:30 ET EOD report generator. Doesn't apply to swing. |
| `journal/__init__.py` | Package marker. |

### 3e. Scripts (mixed)

| Path | Status |
|---|---|
| `scripts/analyze_shadow_outcomes.py` | Remove. Intraday-specific. |
| `scripts/manual_build_watchlist.py` | Remove. Gap-and-go watchlist. |
| `scripts/manual_trigger_earnings_refresh.py` | Remove. Intraday Finnhub integration. |
| `scripts/smoke_test_haiku.py` | Remove. Specific to intraday prompts. |
| `scripts/smoke_test_qwen_decision.py` | Remove. Will be rebuilt against new prompts. |
| `scripts/diagnose_anthropic_connection.py` | Remove. Will be rebuilt. |
| `scripts/test_databento_connection.py` | Remove. Dormant. |
| `scripts/test_finnhub_endpoints.py` | Remove. Will be rebuilt. |
| `scripts/verify_anthropic_client.py` | Remove. Will be rebuilt. |
| `scripts/verify_signal_engine.py` | Remove. Intraday. |
| `scripts/verify_prompts.py` | Remove. Intraday prompts. |
| `scripts/verify_metrics.py` | Remove. Intraday metrics. |
| `scripts/verify_fred_vix.py` | Remove. Was for intraday regime context; new arch handles regime differently. |
| `scripts/verify_regime.py` | Remove. Intraday regime. |
| `scripts/build_overview_doc.js` | Remove. Doc build artifact. |
| `scripts/__init__.py` | Remove. Will be recreated. |

### 3f. Tests

| Path | Reason for removal |
|---|---|
| `tests/` (entire directory) | All tests target intraday modules: gap-and-go signals, bar aggregation, 5-min context, intraday replay, intraday fill simulator, intraday LLM policy. New test suite gets built against the new architecture. The test framework patterns (pytest, async testing) are well-understood; preserving the test files themselves locks in patterns that don't apply. |

### 3g. Documentation

The gap-and-go-era docs in `docs/` are extensive. All move to legacy-intraday and get removed from main.

| Path | Reason |
|---|---|
| `PROJECT_BLUEPRINT.md` | Describes the gap-and-go platform's VPS deployment, dedup bug, 15:55 ET schedule. Replaced by new `PROJECT_CHARTER.md`. |
| `README.md` | Anchored to base project. Rewritten. |
| `SETUP_NEW_MACHINE.md` | Workstation setup, may be partially relevant. Rewriting from scratch is cleaner than partial edit. |
| `SESSION_RESUME_2026-05-12.md` | Old session resume artifact. |
| `boot_2026-05-14_*.log` (4 files) | Boot logs from intraday platform. Total ~4.2MB. Pure noise on new arch. |
| `docs/LLM_MODEL_CHARTER.md` | Explicitly defines LLM as drop-in replacement for gap_and_go.py. The contaminated origin doc. |
| `docs/LLM_MODEL_OVERVIEW.md` | Same era, same contamination. |
| `docs/LLM_MODEL_V2_REFINEMENTS.md` | Same era. |
| `docs/LLM_SIGNAL_INTERFACE.md` | Intraday signal interface spec. |
| `docs/M2_REPLAY_HARNESS_DESIGN.md` | Intraday replay design. |
| `docs/NARRATIVE_OVERVIEW.md` | Same era. |
| `docs/NEXT_SESSION_MENU.md` | Stale planning artifact. |
| `docs/RESEARCH_NOTES.md` | Intraday research. |
| `docs/WAVE_DEPLOY_CHECKLIST.md` | VPS deploy checklist for intraday. New deploy plan (Godzilla as primary) will get its own checklist. |
| `docs/SSH_KEY_SETUP.md` | VPS SSH setup. Not relevant on Godzilla-primary deploy. |
| `docs/Finnhub_api.html` | Old API capture. |
| `docs/finnhub_api_compiled.md` | Old API compilation. |
| `docs/finnhub_api_reference.docx` | Same. |
| `docs/finnhub_gap_and_go_evaluation.md` | Gap-and-go evaluation. |
| `docs/API Documentation.docx` | Stale. |
| `docs/patches/` (entire directory, 6 files) | All gap-and-go-era patches. |
| `docs/audits/` (entire directory) | Gap-and-go-era audits. |
| `docs/deploy/` (entire directory) | VPS deploy logs. |
| `docs/reports/` (entire directory, including `replay_results.db`) | Intraday replay reports + DB. |
| `docs/sessions/2026-05-15-*.md` (M2.2 sub-task logs) | Intraday session logs. |
| `docs/sessions/2026-05-16-m2.2-*.md` (12 sub-task logs) | Same. |
| `docs/sessions/2026-05-16-m3-first-run.md` | M3 (parked). |
| `docs/sessions/2026-05-16-m3-diagnosis.md` | M3 (parked). |
| `journals/` (entire directory) | Intraday EOD journals. |

### 3h. Caches and ephemeral artifacts

| Path | Reason |
|---|---|
| `__pycache__/` | Regenerable. |
| `.pytest_cache/` | Regenerable. |
| `.replay_cache/` | Intraday replay cache. |
| `sim/` (per filesystem listing) | Will check contents in execution phase; likely intraday simulation artifacts. |

### 3i. CLAUDE.md — special case

`CLAUDE.md` is preserved as a file but its contents will be REWRITTEN on main as part of the scaffold creation. The legacy-intraday branch keeps the current content. The rewrite is in §5 below.

## 4. Removal summary by category

| Category | Files removed | KB removed (approx) |
|---|---|---|
| Orchestrator (main.py) | 1 | 55 |
| Strategy code (signals + LLM engine + signal_engine) | 14 | ~80 |
| Data feeds + replay | ~12+ | ~150 |
| Analysis (indicators, sentiment, walls) | 4 | ~40 |
| Execution + journal | 4 | ~30 |
| Scripts | 17 | ~50 |
| Tests | entire directory | ~200 |
| Documentation | 30+ files | ~500 |
| Boot logs | 4 | 4200 |
| Caches | 4 directories | varies |
| **Total** | **~90 files + caches** | **~5300 KB** |

For comparison: the new scaffold §5 creates roughly 15 files totaling ~80KB. The repo gets dramatically smaller and visibly LLM-native.

## 5. New scaffold — what gets created on main

After the purge, the main branch will contain the preservation list from §2 plus this scaffold.

### 5a. New root files

```
PROJECT_CHARTER.md            # Replaces PROJECT_BLUEPRINT.md. Defines the new
                              # strategy thesis (catalyst-driven multi-day swing),
                              # the four-layer architecture (research / decision /
                              # execution / measurement), the locked decisions
                              # from STRATEGY_RESET, and pointers to the
                              # operational rules in CLAUDE_PREFLIGHT.md.

README.md                     # Brief overview, repo structure, how to read in
                              # order: CLAUDE.md → CLAUDE_PREFLIGHT.md →
                              # PROJECT_CHARTER.md → docs/ARCHITECTURE.md.

CLAUDE.md                     # REWRITTEN. Describes the new architecture instead
                              # of the old. References preserved rules. Points to
                              # PROJECT_CHARTER.md for current state.

config/settings.yaml          # NEW. Skeleton config for the new architecture.
                              # Sections: universe, knowledge_base, research_loop,
                              # decision, risk, execution, llm_tiers. No intraday
                              # cadence, no flatten time, no gap_and_go params.
```

### 5b. New module directories with `__init__.py` stubs

```
data/
├── __init__.py
└── README.md                 # "Knowledge-base ingestion lives here. SEC EDGAR,
                              #  transcripts, news flow, market data. Each source
                              #  is its own module."

knowledge_base/               # NEW top-level module for the persistent KB
├── __init__.py
└── README.md                 # "Vector DB (Qdrant), time-series DB (Postgres+
                              #  Timescale), relational DB (Postgres) wrappers
                              #  live here. See docs/ARCHITECTURE.md §2d."

research/                     # NEW top-level module for the continuous research loop
├── __init__.py
└── README.md                 # "Daily research loop. Reads new docs from the KB,
                              #  scores candidates, generates ranked watchlist."

decision/                     # NEW top-level module for the decision engine
├── __init__.py
└── README.md                 # "Pre-market decision engine. Reads research output,
                              #  routes through T2/T3, sizes positions, generates
                              #  trade decisions."

execution/                    # KEPT as a module location, all files removed
├── __init__.py
└── README.md                 # "Multi-day position manager + Alpaca GTC bracket
                              #  order client. Held positions tracked here."

strategy/                     # KEPT as a module location, all files removed
├── __init__.py
└── README.md                 # "Risk validation, position sizing, portfolio heat
                              #  caps, sector concentration caps. Cross-cutting
                              #  rules that apply at decision time."

measurement/                  # NEW top-level module for analytics
├── __init__.py
└── README.md                 # "Daily P&L attribution, calibration metrics (Brier
                              #  score, decile Sharpe), regime tagging, weekly
                              #  audits."

scripts/
├── __init__.py
└── README.md                 # "Verifiers, smoke tests, one-off data builders.
                              #  Each script declares its execution context per
                              #  CLAUDE_PREFLIGHT.md Rule 16."

tests/
├── __init__.py
└── README.md                 # "New test suite. Per-module unit tests under
                              #  test_<module>/, integration tests under
                              #  test_integration/."
```

### 5c. New documentation skeleton

```
docs/
├── HARDWARE_PLATFORM.md            # PRESERVED from §2
├── STRATEGY_RESET_2026-05-16.md    # PRESERVED from §2
├── STRATEGY_STATUS_2026-05-16.md   # PRESERVED from §2
├── SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md  # PRESERVED from §2
├── ARCHITECTURE.md                  # NEW. The four-layer system in detail.
│                                    # Data sources, KB schemas, research loop
│                                    # cadence, decision flow, execution path,
│                                    # measurement loop. Successor to
│                                    # LLM_SIGNAL_INTERFACE.md.
├── DATA_SOURCES.md                  # NEW. Per-source: SEC EDGAR (free, REST),
│                                    # Polygon (daily bars + news), Alpaca News,
│                                    # Finnhub (earnings calendar), transcript
│                                    # source TBD (paid). Ingestion cadence and
│                                    # error handling.
├── ROADMAP.md                       # NEW. Phase R1 → R6 from STRATEGY_RESET
│                                    # broken into specific sessions with
│                                    # checkpoints.
└── sessions/
    ├── 2026-05-16-strategy-review.md      # PRESERVED from §2
    └── 2026-05-16-m3-handoff.md           # PRESERVED from §2 (historical)
```

### 5d. What is deliberately NOT in the scaffold

- No `main.py`. The orchestrator is the wrong abstraction for a strategy that runs research overnight and makes decisions pre-market. The new entry point will be a research daemon plus a CLI (e.g., `python -m research.daily_loop`). Not creating a main.py prevents accidental import-cycle copying from the legacy version.
- No `strategy/llm/` directory. The new LLM client and prompt code lives under `research/llm/` and `decision/llm/` because the LLM has different roles in each layer (research = read filings, decision = evaluate candidates). Forcing the separation in the directory structure prevents the monolithic-LLM-module pattern that contaminated the prior architecture.
- No tests yet. Test scaffolding gets added module-by-module as actual code lands. Empty tests directory plus a README is the placeholder.
- No prompts yet. Prompts get created when the research loop and decision engine are scoped in detail in Phase R3 (per STRATEGY_RESET).

## 6. Execution sequence — migrate to new folder

Ordered steps. Each is small and verifiable. Numbered for Rule 16 / 24 / 27 hygiene.

### Step 1 — User creates the new project folder
**(In a normal PowerShell window on Windows (Godzilla))**

```powershell
# Create the new sibling folder
New-Item -ItemType Directory -Path "C:\trading\LLM_SWING_MODEL" -Force

# Verify it exists, empty
Get-ChildItem C:\trading\LLM_SWING_MODEL\
# Expected: empty listing

# Confirm legacy folder is untouched
Get-ChildItem C:\trading\"LLM model"\ | Select-Object Name | Measure-Object
# Expected: same count as before this session
```

### Step 2 — User adds the new folder to Cowork
**(User side, in the Cowork application UI)**

In Cowork, switch the selected folder from `C:\trading\LLM model` to `C:\trading\LLM_SWING_MODEL`. This is a Cowork application action, not a shell command. After switching, Claude's next session will be anchored at the new folder. The legacy folder remains on disk; Cowork just stops mounting it.

**Important:** the user will need to re-open Cowork or trigger the folder selection flow. Different Cowork UI paths exist; the user knows their own UI. The verification is: when the next Cowork session opens, `pwd` (or equivalent anchor check per Rule 25) shows `C:\trading\LLM_SWING_MODEL`.

### Step 3 — User migrates preserved files
**(In a normal PowerShell window on Windows (Godzilla))**

Run the following block to copy preserved files from the legacy folder to the new one. The legacy folder is not modified.

```powershell
$src = "C:\trading\LLM model"
$dst = "C:\trading\LLM_SWING_MODEL"

# Top-level files (CLAUDE_PREFLIGHT renamed to _SWING variant)
Copy-Item "$src\CLAUDE_PREFLIGHT_SWING.md" "$dst\CLAUDE_PREFLIGHT_SWING.md"
Copy-Item "$src\.gitignore" "$dst\.gitignore"
Copy-Item "$src\.gitattributes" "$dst\.gitattributes"
Copy-Item "$src\pyproject.toml" "$dst\pyproject.toml"
Copy-Item "$src\requirements.txt" "$dst\requirements.txt"
Copy-Item "$src\requirements-llm.txt" "$dst\requirements-llm.txt"

# Docs to preserve
New-Item -ItemType Directory -Path "$dst\docs" -Force | Out-Null
New-Item -ItemType Directory -Path "$dst\docs\sessions" -Force | Out-Null
Copy-Item "$src\docs\HARDWARE_PLATFORM.md" "$dst\docs\HARDWARE_PLATFORM.md"
Copy-Item "$src\docs\STRATEGY_RESET_2026-05-16.md" "$dst\docs\STRATEGY_RESET_2026-05-16.md"
Copy-Item "$src\docs\STRATEGY_STATUS_2026-05-16.md" "$dst\docs\STRATEGY_STATUS_2026-05-16.md"
Copy-Item "$src\docs\SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md" "$dst\docs\SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md"
Copy-Item "$src\docs\sessions\2026-05-16-strategy-review.md" "$dst\docs\sessions\2026-05-16-strategy-review.md"
Copy-Item "$src\docs\sessions\2026-05-16-m3-handoff.md" "$dst\docs\sessions\2026-05-16-m3-handoff.md"

# Verify
Get-ChildItem -Recurse $dst | Select-Object FullName, Length | Sort-Object FullName
```

Expected: roughly 13 files copied, all readable. The new folder mirrors the preserved-files structure described in §2.

### Step 4 — User creates fresh venv and installs deps
**(In a normal PowerShell window on Windows (Godzilla))**

```powershell
cd C:\trading\LLM_SWING_MODEL

# Fresh venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install deps from migrated requirements
pip install -r requirements.txt
pip install -r requirements-llm.txt

# Sanity check
python -c "import sys; print(sys.executable); print(sys.version)"
```

Expected: venv created, packages installed, Python from `C:\trading\LLM_SWING_MODEL\.venv\Scripts\python.exe`. Installation will take 5-15 minutes depending on disk speed and which packages compile from source.

### Step 5 — Claude writes the new scaffold files (next Cowork session, after Cowork is switched to new folder)
**(Claude side, file tools, in the next session anchored at `C:\trading\LLM_SWING_MODEL`)**

In the NEXT Cowork session (after Step 2's folder switch lands), Claude writes the new scaffold files described in §5 of this plan: `PROJECT_CHARTER.md`, `README.md`, `CLAUDE.md` (new, swing-specific), `config/settings.yaml`, `docs/ARCHITECTURE.md`, `docs/DATA_SOURCES.md`, `docs/ROADMAP.md`, and the seven module-directory `__init__.py` + `README.md` stubs.

This is a separate session from the one writing this plan. The plan exists so the new session has clear marching orders.

### Step 6 — User initializes git in the new folder
**(In a normal PowerShell window on Windows (Godzilla))**

```powershell
cd C:\trading\LLM_SWING_MODEL

# Fresh git init
git init
git add .
git status  # Verify what's being committed: scaffold + preserved files

# Initial commit
git commit -m "init: LLM Swing Model scaffold

Migrated from C:\trading\LLM model\ on 2026-05-17 per docs/STRATEGY_RESET_2026-05-16.md
and docs/SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md.

Preserved from legacy:
- CLAUDE_PREFLIGHT_SWING.md (27 rules, 9+10 reserved)
- docs/HARDWARE_PLATFORM.md (Godzilla specs)
- docs/STRATEGY_RESET_2026-05-16.md and three related session docs
- requirements.txt, requirements-llm.txt
- .gitignore, .gitattributes, pyproject.toml

New scaffold:
- PROJECT_CHARTER.md, README.md, CLAUDE.md (swing-specific)
- config/settings.yaml (swing-trading shape)
- docs/ARCHITECTURE.md, DATA_SOURCES.md, ROADMAP.md
- Module directories: data/, knowledge_base/, research/, decision/,
  execution/, strategy/, measurement/, scripts/, tests/ (each with
  __init__.py and README.md stub)

Legacy intraday project retained read-only at C:\trading\LLM model\."

# Remote setup — decide between two options
# Option (a): new branch on existing remote
# git remote add legacy https://github.com/NZ1979/trading-model-llm.git
# git push -u legacy main:swing-main
#
# Option (b, recommended): new GitHub repo
# Create https://github.com/NZ1979/trading-model-llm-swing.git in GitHub UI
# git remote add origin https://github.com/NZ1979/trading-model-llm-swing.git
# git branch -M main
# git push -u origin main

# Verify clean (Rule 27)
git status
git log -1 --oneline
```

### Step 7 — Session wrap (Rule 27)
**(Claude + User)**

The session that completes the migration generates a wrap line:
**`Committed and pushed: <new SHA>`** with the SHA from Step 6's `git log -1 --oneline`. The legacy folder remains untouched at `C:\trading\LLM model\` per Rule 26. Future Cowork sessions on the swing model anchor at `C:\trading\LLM_SWING_MODEL\`.

### Note on session boundaries

This execution sequence spans at least two Cowork sessions because the Cowork folder switch (Step 2) happens between Steps 1-4 (legacy folder anchor) and Step 5 (new folder anchor). Specifically:

- **This session (now, anchored at `C:\trading\LLM model\`):** Steps 1-4 instructions written here. CLAUDE_PREFLIGHT_SWING.md already written to disk in the legacy folder, ready for Step 3 to copy forward. The legacy folder needs to retain that file as a copy until Step 3 runs.
- **Between sessions:** User runs Steps 1-4 in PowerShell, then switches Cowork to the new folder.
- **Next session (anchored at `C:\trading\LLM_SWING_MODEL\`):** Steps 5-7. Claude writes scaffold files. User runs Step 6 (git init + commit + push).

## 7. What this DOES NOT do

This plan does NOT:

- Touch the gap-and-go fork on the old laptop or the VPS at `5.161.199.155`. Per Rule 26 those are off-limits from this session.
- Build any new functionality. The scaffold is empty structural placeholders. Real code lands in Phase R2 onward per STRATEGY_RESET.
- Set up Qwen on Godzilla. That's Phase R2 work.
- Stand up the knowledge-base infrastructure. Phase R2 work.
- Backfill historical data. Phase R4 work.
- Delete `trading.db`. The legacy data asset stays on main as a reference (and as recovery insurance — even if legacy-intraday were lost, the data file persists).

## 8. Risk and rollback

**Risk of data loss: zero.** The migration is purely additive (copy forward, create new). The legacy folder at `C:\trading\LLM model\` is never modified. If the new folder gets corrupted or the scaffold goes wrong, delete the new folder and start over; the legacy folder is unaffected.

**Risk of breaking the legacy .venv or imports: zero.** The legacy folder is read-only by discipline (Rule 26). No operations in this plan touch the legacy `.venv`, the legacy `.git`, or any legacy Python files.

**Risk of git state confusion: minimal.** Legacy `.git` is its own repo with its own history at its own remote (`https://github.com/NZ1979/trading-model-llm.git`). New folder gets fresh `git init` with its own (eventually new) remote. The two repos do not share history and cannot accidentally cross-contaminate.

**Risk of "wrong folder" anchor in Cowork sessions:** Mitigated by Rule 25's anchor verification. Every session opens with a `pwd` / Windows-equivalent check. If a future session lands in `C:\trading\LLM model\` by accident, Rule 26 §8 says treat as misanchor and re-anchor to the new folder before continuing.

## 9. Approval gates

Before executing the migration, explicit go-ahead is needed on these decisions. Most can be answered in this session.

**Gate 1 — Preservation list (§2).** The list now reflects the new approach. Specific decisions to confirm:

- ✅ `CLAUDE_PREFLIGHT.md` → migrate as `CLAUDE_PREFLIGHT_SWING.md` (already created in legacy folder this session).
- Decision needed: `trading.db` — confirmed staying in legacy only (not migrated). Any objection?
- Decision needed: `.env` (47-byte file) — confirmed staying in legacy only. New `.env` created in new folder when needed.

**Gate 2 — Non-migration list (§3).** Every file listed in §3 stays in legacy only. Any specific file in §3 you want migrated forward instead? Most likely candidates to reconsider:

- `journals/` — daily EOD journals from the intraday platform. Currently staying in legacy. Any reason to migrate?
- `tests/test_*.py` — most are intraday-specific. Currently staying in legacy. Any specific test patterns you want lifted?
- Anything else.

**Gate 3 — Scaffold structure (§5).** The seven-module layout for the new folder:
- `data/` (raw ingestion)
- `knowledge_base/` (KB wrappers)
- `research/` (continuous research loop)
- `decision/` (decision engine)
- `execution/` (orders + positions)
- `strategy/` (risk + policy)
- `measurement/` (analytics)

Confirm this layout, or propose a different split.

**Gate 4 — Remote setup.** When git init lands in Step 6, the new project needs a remote. Two options:

- (a) Push to a new branch (`swing-main`) on the existing remote `https://github.com/NZ1979/trading-model-llm.git`. Same GitHub repo, two distinct branches for two distinct projects. Slightly cluttered but uses what's already there.
- (b) Create a new GitHub repo `trading-model-llm-swing.git` (or similar name) and use it as origin for the new folder. Clean separation. Recommended.

Recommendation is (b). Confirm or override.

**Gate 5 — Approve and execute.** When Gates 1-4 clear:
- I provide Steps 1-4 PowerShell scripts (the user executes them this session, before switching Cowork).
- User runs Steps 1-4 and switches Cowork to the new folder.
- Next Cowork session (anchored at the new folder): I write Step 5's scaffold files.
- User runs Step 6 (git init + commit + push).

Note: Step 5 cannot happen in this session because Cowork is currently anchored at `C:\trading\LLM model\`. Writing scaffold files to the new folder requires Cowork to be switched first.

---

## 10. Design decisions captured this session (2026-05-17)

### 10a. Databento subscription audit and cancellation

**Audit finding:** Multiple project documents (`CLAUDE.md`, `PROJECT_BLUEPRINT.md`, the original `CLAUDE_PREFLIGHT.md`, `docs/NARRATIVE_OVERVIEW.md`, and several derived files in `docs/sessions/`) consistently stated that the Databento CME Globex MDP3.0 subscription was cancelled on 2026-04-28. On 2026-05-17, Neale confirmed the subscription was in fact still active and being billed at $179/month. The "canceled" claim was a documentation error — likely a planned cancellation that got documented as a completed action without verification follow-through. The error propagated through every doc that copied the original claim forward.

**Rule classification:** This is a Rule 7 violation (silently locked-in default — "we decided to cancel" became "we cancelled" without revisit) compounded by a Rule 14 violation (verification before conclusion — the conclusion "subscription cancelled" was never tested against billing or the Databento dashboard). It is also the exact failure mode Rule 1 was written to prevent: never claiming current state about an external service without verifying it.

**Decision:** Neale is cancelling the Databento subscription on 2026-05-17 because (a) the legacy intraday architecture is parked and the futures-walls scanner code is dormant, and (b) the new LLM Swing Model architecture does not consume Databento data (rationale in §10b below). Cancellation is the user's action; this plan does not include cancellation steps because Databento UI navigation is subject to Rule 1 (the UI may have changed since training data).

**Documentation correction scope:** The following migrated docs had stale "Databento cancelled" claims that were corrected this session, with audit-trail notes pointing here:
- `CLAUDE_PREFLIGHT_SWING.md` — Rule 10 reserved-placeholder narrative
- `docs/STRATEGY_STATUS_2026-05-16.md` — architecture section
- `docs/SCRUB_AND_SCAFFOLD_PLAN_2026-05-17.md` (this doc) — §3b file description

Docs that stay in the legacy archive (`docs/NARRATIVE_OVERVIEW.md`, the m2.2 session series, the original `PROJECT_BLUEPRINT.md`, the original `CLAUDE_PREFLIGHT.md`) retain the stale claims as historical record. The legacy archive is read-only per Rule 26; future swing-model sessions will not be misled by those docs because they will not be read in operational context.

`docs/sessions/2026-05-16-strategy-review.md` retains the original "Databento was canceled" framing in its analytical response to ChatGPT, as historical record of conclusions reached under the incorrect assumption. The substantive analysis (microstructure data unusable at 5-min cadence regardless of source) is not affected by the correction.

### 10b. Macro context data layer — to be included in swing architecture

**Decision:** The LLM Swing Model architecture will include a macro context layer that incorporates three free data sources as inputs to the daily research loop and the decision engine. This decision replaces the prior implicit framing where Databento futures data was treated as a potential macro context source.

**Sources (all free):**

| Source | Vendor | What it provides | Cadence |
|---|---|---|---|
| **SPY ETF** | Alpaca Daily (free) or Polygon Stocks Starter ($29/mo, already subscribed) | S&P 500 directional context; overnight gap; intraday-RTH change | Daily bars; intraday available if useful |
| **VIX index** | FRED (free), Yahoo (free), or Polygon (already subscribed) | Volatility regime — implied 30-day forward vol on S&P | Daily close, plus intraday if helpful |
| **TLT ETF (or 10Y yield via FRED)** | Alpaca Daily (free) or FRED 10Y daily series (free) | Rate environment, rate-sensitive sector tilt input | Daily |

**Computed metrics (designed in Phase R3, implemented in Phase R3):**

- **SPY trend regime:** distance from 50-day SMA, distance from 200-day SMA, 20-day return percentile vs 1-year history
- **SPY overnight gap:** prior-day close to today's pre-market open (as %, signed)
- **VIX regime:** current level percentile vs trailing 1-year; classify as low (<20), normal (20-25), elevated (25-35), stressed (>35)
- **VIX delta:** 5-day change in level (rapid spikes are catalyst signals in their own right)
- **TLT direction:** 5-day, 20-day price change; signals rate expectations shift
- **Yield curve slope (optional):** if 2Y yield is also pulled from FRED, compute 10Y-2Y as a recession indicator

**How these factor into the model:**

1. **Daily research loop input.** Every pre-market research run starts with a macro snapshot. The LLM is told "VIX is at 28 (elevated regime), SPY is below its 50-day SMA, TLT rallied 1.2% yesterday on rate-cut speculation." This shapes how it interprets individual-name catalysts.

2. **Position-sizing modifier.** In elevated-VIX regimes, base position sizes get scaled down (e.g., multiply by 0.7). In low-VIX trending regimes, sizes can run closer to the cap. Specific multipliers tuned during Phase R4 backtest.

3. **Catalyst-weight modifier.** Risk-on catalysts (growth-sector positive guidance) get up-weighted in risk-on regimes and down-weighted when macro is stressed. Risk-off catalysts (defensive-sector strength, rate-sensitive moves) tilt the other way.

4. **Sector rotation signal.** TLT direction informs which sectors get bias adjustments. Falling TLT (rising rates) favors financials, hurts utilities and REITs. Rising TLT (falling rates) reverses that.

5. **Circuit-breaker pre-condition.** If VIX is in stressed regime AND SPY is in a defined drawdown (e.g., >5% from recent high), trigger a "reduce activity" mode that suppresses all new entries until regime normalizes. This is on top of the 25% portfolio drawdown circuit breaker from `STRATEGY_RESET_2026-05-16.md`.

**Implementation footprint (added to scaffold work in next session):**

The macro layer adds these to the seven-module scaffold:

- **`data/macro_feed.py`** (new file) — pulls daily SPY, VIX, TLT bars/values. Lightweight; reuses Polygon or Alpaca clients we already need.
- **`knowledge_base/macro_state.py`** (new file) — computes the metrics above from raw data, stores in the time-series DB, provides query API for the research loop and decision engine.
- **`docs/ARCHITECTURE.md`** — section on macro context layer, the five uses above.
- **`docs/DATA_SOURCES.md`** — entries for SPY, VIX, TLT specifying source vendor, cadence, refresh logic.
- **`config/settings.yaml`** — macro-context section: source choices (which free source per metric), thresholds (VIX regime boundaries), sizing multipliers.

These are minor additions, not architectural changes. They drop in cleanly to Phases R2 (ingestion) and R3 (research loop + decision engine integration). No impact on the timeline in `STRATEGY_RESET_2026-05-16.md`.

### 10c. Process note for future sessions

Two lessons from today's session worth tracking:

1. **Documentation cascade audit.** When a project document contains a claim about external service state (subscription status, account balance, deploy state, API key validity), any other document that references that claim inherits the error. The Databento "canceled" claim propagated across at least seven docs without anyone verifying the underlying account. Periodic audits should treat "current state of external service" claims as expiring assertions that require re-verification.

2. **The free-alternative test.** Before adding a paid data source to an architecture, ask: "What free source captures 80%+ of the value?" In this case, SPY+VIX+TLT capture roughly 90% of the macro context value of Databento ES/VX/ZN futures, at $0 vs $179/month. The expensive part of Databento (MBP-10 Level 2) is the part the swing model architecturally cannot use. Pre-paid optionality on capability the architecture cannot consume is the worst kind of subscription.
