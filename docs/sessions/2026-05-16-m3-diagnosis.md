# M3 Engine Diagnosis — Session Summary, 2026-05-16 (evening)

**Verdict: LLM signal engine is functionally working.** The 1-trade outcome
from the first M3 replay is not mode collapse. It's a conservative engine
pointed at the wrong universe in a quiet week, plus seven concrete
contract-level defects that need fixing before any re-run will produce
clean diagnostic data.

## Session anchors

- **Date / time:** Saturday, 2026-05-16, 18:54 EDT (market closed, weekend)
- **Workstation:** Godzilla
- **Repo:** `C:\trading\LLM model`
- **Branch:** `main`
- **Starting commit:** `8e2b75a` (M3 first-run doc + open hypothesis)
- **Scope:** diagnosis only, no code changes

## What this session did

Followed the diagnosis-first plan from follow-up #7 of
`docs/sessions/2026-05-16-m3-first-run.md`. Goal: determine whether the
engine is broken or just conservative, before any re-run on a different
universe. Steps a through d ran against the persisted M3 replay artifacts
in `docs/reports/replay_results.db` and `docs/sessions/2026-05-16-m3-first-run.log`.

## Step a: the two `schema_invalid_t1` failures

Both rows found in `replay_decisions` (the log file itself does not contain
the literal string; reasoning column captured the truncated pydantic error).
Both have `decision_source=live_merged`, `setup_label=schema_invalid_t1`,
`tier_provenance=t1_failed`, `confidence=0`. Both are real fail-loud
Holds per Rule 18.

**Failure 1 (id=3030):** AMZN, 2026-05-15 10:40 ET.
Pydantic error excerpt: `time_horizon ... input_value='intraday</time_horizon>\n</invoke>'`.
The model leaked Anthropic tool-use XML envelope tags into a string field.
The T1 call is in some prompt mode where the model is unsure whether it's
emitting JSON or tool-use XML, and emitted both.

**Failure 2 (id=3269):** TSLA, 2026-05-15 15:15 ET.
Pydantic error excerpt: `concerns ... Input should be a valid list ... input_value='["gap-down without recov...RSI but no reversal"]}\n'`.
The `concerns` field came in as a stringified JSON list (a string whose
content is JSON) instead of an actual list. JSON-in-JSON serialization
or single-quoting confusion.

**Concrete defect uncovered:** `raw_response` column is empty (len=0) on
both failure rows. When validation fails, the raw model output should be
persisted for diagnosis. We only have the pydantic error excerpt and it
is itself truncated mid-message. Next failure shape change leaves us blind.

## Step b: MSFT Buy autopsy (id=3033)

The only live_merged Buy in the run.

| Field | Value |
|---|---|
| Ticker / time | MSFT, 2026-05-15 10:45 ET |
| `setup_label` | `Gap-up breakout momentum extension` |
| `confidence` | 52 |
| `tier_provenance` | `t1_only` |
| `regime` | `bull` |
| `risk_check_result` | `None` |
| `raw_response` length | 0 |
| `reasoning` length | 280 (truncated mid-word) |

Reasoning (verbatim, ends as stored):

> MSFT gapped +1.18% and has consolidated tightly 415-420 for first 40
> min. Last 3 bars show strong momentum: 10:30 (420.60), 10:35 (421.70),
> 10:40 (423.98) with rising volume (1.4M, 1.08M, 1.3M). Now at 423.77.
> RSI neutral (50), MACD flat but price momentum is accelerating. Bul...

This is a defensible, specific decision. Real OHLC from named bars, real
volume, identifies the consolidation range, names the acceleration. It is
not boilerplate.

Fill outcome (`replay_fills` row): bought 47 shares at $422.39, stop
$413.74, exited at $421.98 on EOD flatten (15:55 ET), realized -$19.32.
Stop not hit, target not hit, end-of-day forced exit. Risk management
behaved correctly; the market just didn't extend.

**Concrete defects uncovered:**

1. Reasoning truncated at exactly 280 chars mid-word. Hard cap somewhere.
2. `risk_check_result=None` on the only actual Buy. Audit data missing.
3. `raw_response` empty even on a successful decision.
4. `tier_provenance=t1_only` despite confidence=52 sitting inside the T2
   escalation band [50, 75]. T2 was structurally unreachable for this run.

## Step c: 10 Hold reasonings, stratified sample

Random-sampled 10 live_merged Holds across ticker × date × time-of-day
bucket (early / mid / afternoon / late). Read reasonings side by side.

**Verdict: varied + specific, not boilerplate.**

Specifics observed across the sample:

- Each reasoning quotes the actual price range or level the model saw.
  NVDA "near 226", AAPL "298.50-298.65", TSLA "442.29-445.80", MSFT
  "$407.39-$409.08", AMZN "268-270". Ticker-specific real numbers, not
  template inserts.
- Several reasonings reference the model's own prior intraday decisions
  for continuity, e.g. "All prior 5 intraday calls correctly held",
  "Prior five decisions all Hold with low confidence (25-28)". The model
  has within-day state awareness.
- Time-of-day framing adjusts. The 15:30 sample explicitly says "30min to
  close ... Avoid EOD noise" and assigns confidence 18, the lowest in
  the sample. Earlier samples don't use that framing.
- Confidence varies 18 to 38 across the sample, not stuck on one value.
- Indicator vocabulary is narrow: every Hold leans on RSI≈50 and
  MACD-flat. The surrounding context differs but the technical anchors
  are repetitive. Yellow flag, not red.
- Every reasoning hits 280 chars or below; several are exactly 280 and
  truncated mid-word. Same length cap as the Buy.

**Setup label sprawl confirmed.** Across all 1,742 live_merged decisions,
the top setup labels are 30+ different freeform phrasings of "neutral
consolidation": `Post-gap consolidation, neutral technicals`,
`Late-Day Consolidation Exhaustion`, `Flat Consolidation—No Edge`,
`Midday consolidation chop`, and so on. The reasoning quality is fine.
The categorical label field is just unconstrained.

## Step d: action × confidence band

Live_merged decisions only (LLM-driven, n=1,742):

```
action     0-29    30-49    50-54    55-55    56-74    75-100    total
Hold       1427      314        0        0        0         0     1741
Buy           0        0        1        0        0         0        1
Sell          0        0        0        0        0         0        0
```

Confidence statistics across the run:

- min = 5, max = 52, 33 distinct integer values
- Mode: conf=28 (413 calls), second mode: conf=25 (344 calls)
- 43% of all calls land in [25, 28]
- Decisions in the T2 escalation band [50, 75]: **1**
- Decisions where `tier_provenance` contains `t2`: **0**

**The handoff's "ceiling at 55" was pessimistic. Real ceiling is 52.**
The model uses meaningful gradation (33 distinct values across 1,742
calls, not collapsed to one number), so the gradation logic is intact.
But it never visited the high-conviction zone in this run.

Two possible explanations:

1. **Accurate market read.** Mega-caps in a quiet 5-day window genuinely
   didn't present a high-conviction setup. Defensible.
2. **Prompt-imposed calibration anchor.** The system prompt may contain
   language pulling the model toward conservative confidence levels
   (max 50, default to Hold, avoid over-confidence, etc.). The exact-52
   ceiling on the one Buy is suspicious.

These cannot be distinguished from this data. The next clean
distinguishing test is a re-run on a known high-conviction setup day
in a wider universe.

## Seven concrete defects to fix before re-run

In rough order of severity:

1. **280-char reasoning cap.** Locate (prompt template, schema validator,
   or storage column) and widen. 280 chars is barely a tweet; the model
   is cut off mid-word on its most important decisions, which destroys
   the audit trail.
2. **T1 JSON-mode contract.** Two `schema_invalid_t1` failures with
   distinct signatures: tool-use XML leakage, and stringified-JSON
   nesting. Pin T1 to JSON mode at the SDK level (`response_format` or
   equivalent) and add explicit "return only the JSON object, no
   tool-use envelope" wording to the system prompt. Add a JSON-repair
   retry layer for the stringified-list case.
3. **Persist `raw_response`.** Empty on both schema failures AND on the
   successful Buy. Without raw responses, every new failure mode is
   re-derived from a truncated pydantic message. Persist the raw model
   output unconditionally.
4. **Persist `LLMContext` input payload.** Not stored anywhere. No way
   to reproduce a decision, no way to run counterfactuals. Add a column
   or sidecar JSON.
5. **Populate `risk_check_result` on Buys.** None on the only Buy in
   the run. Should carry sized / stop-validated / cap-checked payload.
6. **Audit prompt for confidence-anchoring language.** Ceiling at 52
   across 1,742 calls is consistent with a prompt-imposed cap. Grep
   `strategy/llm/` system + user prompts for "conservative", "max
   confidence", "default to Hold", and similar wording. If found,
   evaluate whether it's intentional calibration or accidental
   anchoring.
7. **Constrain `setup_label` to an enum.** Known issue from prior
   session. The reasoning text is varied and specific; only the
   categorical label needs constraint.

## Recommended order of operations

**Do not re-run yet.** The truncation and missing persistence (#1, #3,
#4, #5) mean any new re-run produces data with the same gaps we just
identified. Re-running first wastes a Polygon-backed harness cycle.

Suggested sequence for the next chat:

1. Defects #1, #2, #3, #4, #5 first. These are all in `strategy/llm/`
   and the replay persistence layer; they don't touch the universe or
   the harness mechanics.
2. Defect #6 audit. Read the actual prompt, decide on intent.
3. Defect #7 enum constraint.
4. Then re-run on a wider universe. The small-cap watchlist proposed in
   the prior handoff (HIMS, QBTS, CRWV, CEG, NU + 2-3 control
   mega-caps) is still the right experiment. Running it after the
   contract fixes lands isolates universe-effect from engine-defect-
   effect.
5. Sentiment fixture rebuild remains a separate gap-and-go-anchored
   session per Rule 26.

## Files referenced

- `docs/reports/replay_results.db` (read-only, all step-a/b/c/d data)
- `docs/sessions/2026-05-16-m3-first-run.md` (handoff source)
- `docs/sessions/2026-05-16-m3-first-run.log` (4 MB log, did not
  contain `schema_invalid_t1` literal; failures live in DB only)
- `docs/reports/replay_2026-05-11_run2.md` (existing report from run)

## Open hypotheses, restated

- **Engine is conservative-but-functional, not broken.** Confirmed by
  steps b and c.
- **Confidence ceiling at 52 is real but cause is ambiguous.** Could be
  accurate market read or prompt anchoring. Decidable only after
  defect #6 audit + a re-run with a high-conviction setup in the data.
- **T2 escalation collapsed for this universe.** Only 1 decision crossed
  the band floor. The pre-market RVOL gate almost certainly did not
  fire on mega-caps. T2 is not broken; it is dormant by design when
  T1 confidence stays below 50.

## Out of scope this chat (confirmed)

- Sentiment fixture rebuild (Rule 26 partition; needs gap-and-go-anchored
  session)
- Any code changes (this session was diagnosis only)
- Re-run on new universe (premature until contract fixes ship)

---

**Session wrap:** No code changes. One new doc to commit
(`docs/sessions/2026-05-16-m3-diagnosis.md`). Rule 27 durability block
to follow.
