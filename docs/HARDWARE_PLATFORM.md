# Hardware Platform — LLM Model

This fork is targeted to run on a dedicated workstation, not the
Hetzner VPS. The hardware enables architectural choices that wouldn't
be feasible against API-only LLMs. This doc captures the machine specs,
what each piece unlocks, and how the design changes as a result.

## Workstation specs

| Component | Spec | Why it matters here |
|---|---|---|
| Platform | Puget Workstation Core Ultra Z890 C132-XL | Workstation-grade build, designed for sustained heavy compute |
| Motherboard | ASUS ProArt Z890-Creator WiFi | PCIe 5.0 lanes for the GPU, lots of M.2 slots |
| CPU | Intel Core Ultra 7 270K Plus 24-core 3.7 GHz, 36MB cache, 125W | Parallel pandas/numpy work, parameter sweeps, multi-threaded backtests |
| GPU | NVIDIA RTX PRO 5000 Blackwell 48GB | **Single most important component for this fork.** Runs 70B-class LLMs at 4-bit locally |
| RAM | 4 × Kingston Fury Renegade DDR5-4800 48GB = **192GB total** | Holds years of 1-min bars for thousands of tickers in memory |
| Storage | Samsung 990 Pro 1TB + 4TB + 1TB Gen4 M.2 NVMe (6TB total) | Fast random access for backtest data; tiered storage by hot/warm/cold |
| PSU | Super Flower LEADEX Titanium 1700W | Headroom for sustained GPU + CPU compute under heavy load |
| Cooling | Asetek 624S-M2 240mm AIO + PWM ramping case fans | Sustained workloads without thermal throttling |
| Case | Fractal Design Define 7 XL | Accommodates the GPU and storage stack |
| OS | Windows 11 Pro 64-bit | Native Python + WSL2 if needed for Linux-only tooling |
| Pre-installed | LM Studio, NVIDIA App, Chrome | LM Studio explicitly signals local-LLM workflow |

## What each piece unlocks for the LLM model

### 48GB VRAM is the centerpiece

This GPU runs models that previously required cloud APIs:

| Model | Approx 4-bit VRAM | Throughput on RTX PRO 5000 (est.) | Quality estimate |
|---|---|---|---|
| Llama 3.3 70B Instruct | ~38GB | ~50-80 tok/s | Top open-source for general reasoning |
| Qwen 3.6-27B Instruct (dense) | ~17GB | ~45-50 tok/s (measured 2026-05-12) | **Production target** — flagship-tier 27B dense (Apr 2026), 262k native context, strong JSON discipline. Pre-test estimate was 120-180 tok/s; real throughput at single-stream LM Studio + thinking-disabled tool-use prompt was 43.9 and 48.2 tok/s on two contexts. |
| DeepSeek R1 Distill 70B | ~38GB | ~50-80 tok/s | Chain-of-thought reasoning specifically |
| Llama 3.1 8B Instruct | ~5GB | ~150+ tok/s | Fast, less capable; useful for pre-filter or screening |
| Qwen 2.5 32B Instruct | ~17GB | ~100 tok/s | Balanced; large headroom for batching |

Headroom at 27B 4-bit: ~31GB free for KV cache, activations, and batching.
Enough to batch 16-32 concurrent requests at moderate context, or run at
full 262k single-stream context, or host a second smaller model side-by-side.
The 48GB card is overprovisioned for Qwen 3.6-27B — meaningful margin for
future model swaps up to ~38GB (Llama 3.3 70B fits with ~10GB of headroom).

Initial production target: **Qwen 3.6-27B Instruct** (dense). Strong
JSON-output discipline matters for our schema-validated pipeline, and the
262k native context window means we can pass full daily-bar histories
without truncation. Llama 3.3 70B remains a comparison point if M2 replay
surfaces a quality gap at 27B; the 48GB GPU has the VRAM to load it.

### 192GB RAM removes the data-loading bottleneck

Approximate memory footprint of the working dataset:

- 1-minute bars for 5000 tickers × 5 years × 6.5h × 60 = ~58GB raw
- News headlines + sentiment scores for 2 years = ~5-10GB
- Computed indicators cache for the same period = ~20GB
- Total in-memory workspace = ~100GB, well under the 192GB budget

Translation: M2 replay can hold the entire backtest dataset in memory
at once. No per-day file loads, no swap. This is the difference between
a 30-day replay taking 4 hours and taking 20 minutes.

### 24-core CPU + 1700W PSU enables parameter sweeps

The base codebase has a few free parameters (PM RVOL threshold,
ATR multiplier, position cap, etc.). Backtesting one parameter set is
the M2 baseline. With this CPU we can run 16-24 backtests in parallel,
sweeping over parameter ranges — a 2D grid (e.g. 5 ATR multiples ×
5 confidence thresholds) becomes 25 backtests that finish in roughly
the same wall time as one. Walk-forward validation (train on weeks
1-12, test on week 13, slide forward) becomes feasible without
overnight runs.

### 4TB primary + 1TB tertiary tiering

Recommended layout:
- **Drive 1 (1TB primary, OS)**: Windows 11, Python venv, code, hot working data
- **Drive 2 (4TB secondary, data)**: Polygon bar archives, news archives, model weights (70B 4-bit ~40GB each), backtest result databases
- **Drive 3 (1TB tertiary, cache)**: LM Studio cache, replay LLM-response cache (M2 design uses sha-keyed cache; one drive dedicated to it avoids contention)

Same NVMe Gen4 across all three; speed is uniform. Allocation is about
keeping different concurrent workloads from hitting the same physical
drive controller.

## Architectural implications for the LLM model

### Cost model rewrite (tiered architecture)

The architecture isn't local-only — it's a tiered split where Qwen
local handles the hot path and Anthropic handles selective escalation
plus offline evaluation. See LLM_SIGNAL_INTERFACE.md "Tiered evaluation"
for the full design. Cost summary:

| Path | Backend | Volume | Cost/day |
|---|---|---|---|
| Tier 1 (every candidate, every cycle) | Qwen 3.6-27B local | 30-200 × 78 cycles | ~$0.10 (electricity at $0.12/kWh × ~100W × 8h) |
| Tier 2 (selective escalation) | Sonnet 4.5 | 5-15 calls/day, capped at 25 | ~$0.10-0.30 (with prompt caching) |
| Tier 3 (weekly Opus audit) | Opus 4.6 | ~12K decisions/audit | ~$2-5 amortized |
| **Live operating** | | | **~$2-5/day, $60-150/month** |

For comparison:
- Original "Anthropic Sonnet on every call" assumption: ~$11-30/day, $225-900/month
- Local-only with no Anthropic touch: ~$0.20/day electricity but no Tier 2 domain expertise on hard cases and no Tier 3 calibration anchor

The tiered approach captures Claude's domain reasoning where it pays
off (the 5-15 escalations/day where Qwen confidence is uncertain AND a
real catalyst is present) and uses Opus as a periodic ground-truth
labeler, while keeping ~99% of decisions on the deterministic, free,
private local path.

The pre-filter remains useful for *quality* reasons (don't run the LLM
on tickers that obviously don't have a setup) but is no longer required
for cost reasons. M2-time Tier 3 labeling is bounded by replay window
size; ~$200-400 per 60-day window is a fixed evaluation cost, not an
operating cost.

### Latency model rewrite

| Path | Old (Anthropic) | New (local Qwen 3.6-27B), measured 2026-05-12 |
|---|---|---|
| Single call (250 output tokens) | 700-1500ms (network + inference) | ~5.5s (43-50 tok/s) |
| Single call (500 output tokens, long reasoning) | 1-2s | ~11s |
| 30-candidate cycle (concurrent) | ~2s (10-way concurrency) | ~10-30s (16-32 way batching, est.) |
| 500-candidate cycle | not feasible (cost) | ~60-180s (est.; batching efficiency to be measured during M2 replay) |

Local at 27B is slower per call than Anthropic but free and private,
and the 48GB card has the VRAM for substantial batching. A 60-180s
full-watchlist sweep is well within the 300s cycle budget; batching
efficiency is the main unknown and will be characterized when the M2
replay harness runs multi-candidate batches against historical contexts.

### Live trading deployment options

This workstation is for development + backtest at minimum. The harder
question: where does the live trader run?

- **Option A — Live trader stays on Hetzner; workstation serves LLM**: trader-prod calls back to the workstation for each LLM signal evaluation. Network round-trip adds ~50ms vs same-machine. Workstation must be online and reachable during trading hours (07:30-14:00 MDT). Hetzner remains the reliable execution layer.

- **Option B — Move trader to workstation**: production trader runs on workstation. Local LLM is one process call away (no network). Risk: home internet outage = trading outage. Power failure = trading outage. UPS + redundant ISP mitigate but don't eliminate.

- **Option C — Workstation for research only**: workstation runs M2 replay, M3 prompt iteration, M4 backtest comparison, M5 paper-trading-with-local-LLM. Hetzner-based production keeps using cloud LLMs (Haiku for cost, Sonnet for stake). Workstation results inform whether to switch production to local-LLM (Option A) eventually.

**Recommended: Option C initially, Option A once local-LLM quality is proven competitive in M4 backtest.** Don't move production to home hardware without first validating the local-LLM strategy outperforms the cloud-LLM baseline in backtest.

### Backtest scope expansion

The original M2 spec estimated ~3 days of replay-harness implementation
work and a 30-day replay window. With this hardware, a more ambitious
M2 is feasible:

- **Replay window**: 6-12 months instead of 30 days (more statistical power)
- **Ticker universe**: full Russell 3000 (~3000 names) instead of just the watchlist
- **Parameter sweep dimension**: scan ATR multiplier × confidence threshold × pre-filter PM RVOL together
- **Walk-forward validation**: train prompt on first 80% of period, test on last 20%, repeat with shifted windows
- **Multiple model variants**: run the same replay against Qwen 3.6-27B, Llama 3.3 70B, Qwen 32B, in parallel; compare result quality vs latency vs throughput

The hardware enables the experiment, but every additional dimension
multiplies wall-clock time. Initial M2 still targets a single-model,
30-day, watchlist-scoped replay; the expanded scope is a stretch goal
once the harness works.

## LM Studio integration

LM Studio is the local LLM hosting platform pre-installed on the
machine. Integration approach:

1. **LM Studio loads the chosen model** (Qwen 3.6-27B Instruct 4-bit, downloaded once from HuggingFace via LM Studio's UI).

2. **LM Studio exposes an OpenAI-compatible HTTP API** on `localhost:1234/v1/chat/completions` (default port). This lets us use the existing `openai` Python package as the client; we just point `base_url` at LM Studio.

3. **Our `strategy/signals/llm.py` module** holds two clients: an OpenAI-compatible client pointed at LM Studio for Tier 1 (default path), and an Anthropic client used by Tier 2 (selective escalation) and Tier 3 (offline audit + M2 replay labeling). The schema-validated JSON output works the same against any backend; the same `LLMDecision` dataclass parses Qwen, Sonnet, and Opus responses identically.

4. **Tier 1 model swap is a config change, not a code change.** Switching from Qwen 3.6-27B to Llama 3.3 70B = change LM Studio's loaded model + restart LM Studio. The trading code doesn't know which Tier 1 model it's talking to. Tier 2 and Tier 3 model identifiers (`claude-sonnet-4-5`, `claude-opus-4-6`) are pinned in `config/settings.yaml` so backtest reproducibility is preserved across Anthropic version drift.

5. **Tier-1-fallback mode.** If LM Studio is unreachable (workstation down, model unloaded, port collision), the signal generator promotes Tier 2 to handle every candidate temporarily. Cost during the outage is ~$5-20/day depending on volume; a one-day outage is acceptable, a multi-day outage triggers an alert. Detection is via a single failed call: connection refused or timeout > 8s flips a `t1_available` flag false and the system runs in Tier-2-only mode until a periodic health check succeeds.

6. **For headless 24/7 service** (if we eventually move trader to the workstation): LM Studio has a server mode that runs the API without a UI. Can be set to start at boot. Alternative for production: replace LM Studio with `llama.cpp server` or `vLLM` for better throughput, same OpenAI-compatible API.

## Updated success criteria

With local inference free and fast, the bar for "is the LLM model
worth deploying" rises:

| Metric | Original (cloud LLM) | Updated (local LLM) |
|---|---|---|
| Win rate vs base | "Materially better than base" (e.g. 5pp higher) | Same — the win rate doesn't depend on backend |
| Cost per trade | "Cheaper than the marginal alpha" | Effectively zero; not a constraint |
| Operational complexity | Acceptable if cost was justified | Higher bar — local inference adds infra dependency vs pure cloud setup |
| Confidence calibration | "Useful as a sizing input" | Same |
| Crash-period behavior | "No catastrophic losses" | Same |

Local LLM removing the cost objection means we evaluate the strategy
on quality alone. If quality is comparable to or better than the base
rules-based strategy, we deploy. If quality is worse, we don't —
even if it's free to run.

## Summary

The workstation is purpose-built for this fork's research and (eventually)
deployment. Four architectural shifts:

1. **Local LLM as the primary inference path (Tier 1).** Qwen 3.6-27B runs every candidate every cycle on the workstation GPU. Zero marginal cost, full privacy, deterministic.
2. **Cloud Claude as selective augmentation, not full replacement (Tiers 2 + 3).** Sonnet handles the 5-15 escalations/day where Qwen confidence is uncertain AND a real catalyst is present. Opus 4.6 runs offline as gold-standard labeler for M2 replay and weekly live audit. Cloud is no longer "fallback only" — it has a defined, always-on role for the hard cases.
3. **Pre-filter relaxed.** Cost-driven candidate narrowing is gone; quality-driven candidate narrowing optional.
4. **Backtest at scale.** 30-day replays are the floor; 6-12 month replays with parameter sweeps and Opus labeling are the ceiling.

Every other design doc in this fork (`LLM_MODEL_CHARTER.md`,
`LLM_SIGNAL_INTERFACE.md`, `M2_REPLAY_HARNESS_DESIGN.md`) reflects
these shifts.
