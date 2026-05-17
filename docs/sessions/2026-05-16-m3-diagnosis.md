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

**Session wrap (round 1):** No code changes. One new doc to commit
(`docs/sessions/2026-05-16-m3-diagnosis.md`). Rule 27 durability block
to follow.

Committed and pushed round 1: `adafc17`.

---

## Round 2 diagnostics: locate the defects at line-number precision

Continuation in the same chat after round 1 wrap. Pure file-reading
diagnostics to give the next chat exact line numbers for each fix
instead of re-deriving them. No code changes in round 2 either.

### Round 2 finding 1: the 280-char cap (defect #1)

Two locations, must move in lockstep:

- **Hard cap:** `strategy/llm/types.py:132` — `reasoning: str = Field(max_length=280)`
- **Truncation validator:** `strategy/llm/types.py:268-273` — `@field_validator("reasoning", mode="before")` truncates at 277 chars and appends `"..."`, producing exactly 280 chars. This is what produced the mid-word truncation observed in every row (e.g. `...accelerating. Bul...`).
- **Soft prompt instruction:** `strategy/llm/prompts.py:65` — `Keep reasoning under 280 characters; concerns to <=5 short tags.`

The validator is the hard enforcer. The prompt is just guidance. To
widen, change all three numbers together (Field max_length, validator
truncation length and slice index, prompt instruction). Suggested
target: 800-1200 chars. The same pattern applies to
`setup_label` (max 50, validator at lines 261-266) and
`alternative_view` (max 140, validator at lines 275-279), but those
are deliberately short categorical/summary fields and don't need
widening.

### Round 2 finding 2: confidence anchoring is intentional and explicit (defect #6)

Read `strategy/llm/prompts.py:34-68`. The T1 system prompt aggressively
biases toward Hold. Verbatim excerpts:

- Line 36: `You make conservative, high-conviction decisions. You prefer Hold over forcing a marginal trade.`
- Line 40-41: `Hold is the default. Only return Buy or Sell if you see a setup worth ~0.5% account risk.`
- Line 42-43: `Confidence below 40 should always be Hold. Low-conviction trades lose money historically.`
- Line 44-45: `Counter-trend trades ... need exceptional justification, typically a clear catalyst.`

**Verdict:** the 52-confidence ceiling and the 1,427-of-1,742-calls-at-conf<30
distribution observed in step d are NOT emergent. They are precisely
what the prompt instructs. The model is following its instructions.

**Two second-order issues this surfaces:**

(a) The prompt sets a 40 floor for directional calls; the T2 escalation
band is [50, 75]. There is a 10-point gap (40-49) where the model will
fire a Buy/Sell but never escalate to T2. The MSFT Buy at conf=52
barely crossed into the T2 band; structurally, the prompt's incentive
pushes directional commits right against the lower escalation edge.

(b) "Low-conviction trades lose money historically." Based on what
historical data? This fork is new. If inherited from the base
gap-and-go engine's stats, the empirical basis may not apply to the
LLM model's setup space. Worth tracing the provenance of this claim
before deciding whether to keep, soften, or replace it.

**Implication for the small-cap re-run:** the confidence anchor will
travel with the model into the new universe. A wider universe alone
will not break the ceiling. If the goal is to gather signal-quality
data on directional calls (the M3 entry-sequence requirement), the
prompt's bias toward Hold needs to be addressed at the same time as
or before the universe change. Otherwise the next run produces the
same 1-trade-per-1,742-calls outcome on a different watchlist.

### Round 2 finding 3: tool-use config is correct, schema failures are model hallucinations (defect #2 refined)

Read `strategy/llm/clients.py` end-to-end. The Anthropic client is
correctly configured:

- `clients.py:252-260` sets `tool_choice={"type": "tool", "name": "submit_decision"}` (forces tool use) with the schema generated from `LLMDecision.model_json_schema()` minus platform-only fields.
- `clients.py:202-213` correctly parses the tool_use block from the response.
- `clients.py:216-230` constructs `LLMDecision` from `tool_block.input`, attaching usage metadata as `raw_response`.

**There is no JSON-mode-vs-tool-use config bug.** The two
schema_invalid_t1 failures are Haiku producing malformed string
content *inside* a valid tool-use envelope:

- AMZN: `time_horizon='intraday</time_horizon>\n</invoke>'`. The model wrote XML closing tags as part of the literal string value for an enum field. The SDK passed it through; pydantic rejected it because it's not in the enum.
- TSLA: `concerns='["gap-down without recov...RSI but no reversal"]}\n'`. The model wrote a stringified JSON list (a string whose content is JSON) instead of an actual list, with extra `}\n` trailing garbage.

**Suggested mitigations (in order of robustness):**

1. Add a string-cleanup `field_validator(..., mode="before")` for
   string fields that strips `</[a-z_]+>` tag patterns and
   `</invoke>` artifacts before the literal-enum check fires.
2. Add a `field_validator("concerns", mode="before")` that
   detects stringified-JSON-list inputs (starts with `[`,
   contains `]`) and `json.loads` them. This pattern is already
   implemented for the Qwen path at `clients.py:337-363`
   (`_coerce_qwen_param`) — port the same logic to a Pydantic
   validator so it works for both backends.
3. Add a one-shot JSON-repair retry: on `SchemaInvalidError`,
   re-call the model with a follow-up message showing the
   pydantic error and asking for a corrected response. This is a
   larger change and may not be worth it for a 0.11% failure
   rate.

(1) and (2) together would catch both observed failure shapes with
small, well-contained code changes that fit the existing validator
pattern.

### Round 2 finding 4: defects #3, #4, #5 collapse to a single line-numbered persistence bug

Read `data/replay/persistence.py:421-447`. The INSERT statement for
`replay_decisions` writes 11 columns and 11 placeholders:

```
INSERT INTO replay_decisions
(run_id, trading_date, tick_et, ticker, decision_source,
 action, setup_label, confidence, reasoning,
 tier_provenance, regime)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
```

The table has 13 data columns (per PRAGMA from step b). `raw_response`
and `risk_check_result` are **simply omitted** from both the column
list and the VALUES tuple. The columns exist in the schema; the
INSERT silently never writes to them; every row therefore has NULL in
both columns.

**Diagnosis revision:** the original diagnosis listed three separate
defects:

- #3 persist raw_response
- #4 persist LLMContext
- #5 populate risk_check_result on Buys

Defects #3 and #5 are **the same one-line fix**: amend the INSERT
statement to include the two columns. The data is already being
populated upstream (the Anthropic client populates `raw_response` at
`clients.py:218-229`, and `risk_check_result` populated by the
signal_engine / risk module — to be confirmed but not relevant for the
persistence-layer fix). Defect #4 (LLMContext payload) is a separate
add since the context isn't on the `LLMDecision` model; it needs its
own column or a sidecar JSON file.

The same INSERT bug exists in the T3 path at lines 376-395, which
also omits both columns. Fix in both places.

### Round 2 finding 5: error-path also discards raw input on validation failure (companion to defect #3)

`strategy/llm/clients.py:231-234`:

```python
except ValidationError as exc:
    raise SchemaInvalidError(
        f"LLMDecision validation failed: {exc}"
    ) from exc
```

When `LLMDecision(**tool_block.input, raw_response=...)` raises on bad
input, the raw `tool_block.input` dict is discarded. Only the pydantic
error message string survives the exception chain. That's why the two
`schema_invalid_t1` rows had truncated pydantic error excerpts as their
"reasoning" and nothing else useful for diagnosis.

**Fix:** capture `tool_block.input` (and the usage metadata) before
raising, attach to the exception, and surface to the signal_engine so
it can write the synthetic-Hold row with the original malformed input
preserved in the `raw_response` column. Pairs naturally with the
persistence-INSERT fix above.

### Round 2 finding 6: LocalClient has defensive parsing that should back-port

The dormant `LocalClient` (LM Studio / Qwen path, lines 402-587) is
much more defensive than the AnthropicClient. Specifically:

- `_coerce_qwen_param` (lines 337-363) JSON-decodes stringified
  list/object values. Exactly the TSLA failure shape.
- `_parse_qwen_tool_call` (lines 366-394) parses XML-style tool calls
  from text content as a fallback when the SDK's tool_calls list is
  empty. Adjacent to but not the same as the AMZN failure.
- Lines 514-532 build a rich diagnostic dump on no-tool-call failures
  (finish_reason, tool_calls list, token counts, message dump) instead
  of a one-line error.

The Anthropic path predates some of this. Worth a sweep to bring it
to parity.

### Revised defect inventory after round 2

Net effect of round 2: the seven defects from round 1 reduce to **five**
with much sharper localization. Three of the original five become a
single INSERT-statement fix; what was vague becomes line-numbered.

| # | Defect | Location | Notes |
|---|---|---|---|
| 1 | Reasoning truncated to 280 chars | `types.py:132,268-273` + `prompts.py:65` | Widen all three together. Same pattern for setup_label/alternative_view exists but they're correctly short. |
| 2 | T1 schema_invalid failures (XML tags, stringified-JSON) | `types.py` validators | Add `mode="before"` validators: strip XML tag patterns on string fields, JSON-decode stringified-list values on `concerns`. Pattern already exists for numerics + lengths. |
| 3 | Raw response + risk_check + LLMContext not persisted | `persistence.py:425-444` (and 376-395 for T3) | INSERT omits 2 existing columns. Add them. Then add a sidecar column or JSON for LLMContext (separate concern). |
| 4 | Error-path discards raw input on validation failure | `clients.py:231-234` | Capture `tool_block.input` before raising; surface through exception. |
| 5 | Confidence-anchoring language pulls ceiling to ~52 | `prompts.py:36,40-43` | Intentional, not a bug. Decide whether to soften for the next M3 entry-sequence run, since the existing anchor will produce the same "1 trade per 1,742 calls" outcome on any universe. |
| 6 | setup_label freeform sprawl | `types.py:131` | Already-known. Constrain to enum. Low priority. |
| 7 | (Bonus) AnthropicClient less defensive than LocalClient | `clients.py` | Back-port `_coerce_qwen_param`-style JSON-decode logic and richer no-tool-call diagnostics. Subset of #2 mitigations. |

### Round 2 conclusion

Same overall recommendation as round 1: fix defects 1-4 first, decide
on #5 (the prompt anchoring is the most consequential one), then
re-run on the wider universe. Round 2 made each fix mechanically
specific:

- Defect #1: edit 3 line locations (one Field, one validator, one prompt line)
- Defect #2: add 2 validators in `types.py`
- Defect #3: edit 2 INSERT statements in `persistence.py`, plus a separate small change to add an LLMContext column or sidecar
- Defect #4: change 4 lines in `clients.py` error handler
- Defect #5: prompt edit + a conversation with the user about what the right calibration is

Plus the back-port from finding 6 if there's appetite. None of these
are large changes individually; the full set is probably a half-day of
focused work in a fresh chat.

---

**Session wrap (round 2):** No code changes in round 2 either. Doc
amended in place. Rule 27 durability block to follow.
