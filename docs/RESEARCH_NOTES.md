# Research Notes — LLM Model

A running log of external research, papers, and competing approaches
that inform this fork's strategy and architecture decisions. Each entry
records the source, the relevant claim, our take on it, and where (if
anywhere) it has changed our design.

This is a living document. Append; do not edit prior entries.

---

## 2026-05-07 — ML-based momentum strategies (initial research input)

### Sources

- ScienceDirect, "A characteristic-managed approach to momentum"
  https://www.sciencedirect.com/science/article/pii/S0378426625001852
- IJFMR, ML in financial markets paper
  https://www.ijfmr.com/papers/2025/4/50375.pdf
- MDPI Algorithms 11(11), regime-aware ML for trading
  https://www.mdpi.com/1999-4893/11/11/170
- ScienceDirect S0952197625022067 (regime-switching ML)

### Key claims

1. **Characteristic-Managed Momentum (CMM)** — an ML-enhanced version of traditional momentum strategies. Reportedly more profitable and less susceptible to "momentum crashes" (the well-known failure mode where momentum strategies blow up during sharp reversals).

2. **Returns up to 239%** reported in some ML-based momentum studies. (Caveat: very likely in-sample, cherry-picked, or backtest-overfit. Treat as upper bound, not realistic expectation.)

3. **Regime-dependent model selection**:
   - LSTM (long short-term memory) recurrent networks perform well in *fluctuating* (normal-volatility) markets
   - SVM (support vector machines) outperform LSTM during *market crashes* — primarily by being more conservative and avoiding the large losses LSTMs incur during regime shifts

4. **Implementation toolchain mentioned**: Backtrader (Python backtest framework), scikit-learn (SVM, RF), TensorFlow/PyTorch (LSTM), Trade Ideas Money Machine (commercial momentum identifier).

### Our take

**On CMM specifically**: Not directly applicable — CMM is rules-driven momentum enhanced by ML feature engineering, not LLM-driven. Our approach uses Claude to make holistic decisions rather than apply ML to optimize individual factors. Different paradigm. But the *insight* — that momentum strategies have crash-failure modes that need explicit handling — is universal and applies to us.

**On regime-dependent model selection**: This is the most directly actionable insight. The strong claim is "no single model dominates across regimes." For us:

- Our current `LLM_SIGNAL_INTERFACE.md` includes a `market_regime_label` field in `LLMContext` that's passed to the LLM. The expectation has been that Claude, given the regime context, would adapt its decision style. **We have not validated that.** It's possible Claude is regime-blind in practice and we'd need explicit regime-aware prompts (separate templates per regime) instead.
- The replay harness (M2) needs a **regime-stratified comparison report**: bucket decisions by regime label (trending_up, trending_down, choppy, crash) and report performance per bucket. This was implicit in M2 but should be made explicit.

**On ensemble approaches**: Single-strategy bets are fragile. Long-term, the most robust system likely combines:
- LLM for setup identification and context-aware decisions
- A regime-detection layer (could itself be a small ML classifier)
- A "crash protection" layer that overrides the LLM in extreme conditions (high VIX, gap-down market, etc.)

This is **out of scope for M2-M4** but should not be designed out of the
system. The signal-engine plug-in architecture (strategy/signals/) we
already inherited from the base supports this — multiple signal modules
can register and the dispatcher can combine their outputs.

**On the 239% return claim**: Skeptical. Real-world deployment rarely
matches paper returns. We treat any "X% return" claim as a signal that
the underlying approach is *interesting*, not as a target.

**On the recommended toolchain**: Backtrader is well-known but heavy;
our M2 replay harness is lighter and tailored to our pipeline. We don't
need it. scikit-learn and PyTorch may become relevant if we add the
ensemble layer described above.

### Design changes from this input

1. **`docs/LLM_MODEL_CHARTER.md`**: added a "Related research and approaches" section that mentions CMM, regime-aware ML, and ensemble methods as comparison points. The LLM-only approach is one bet; we explicitly note that ensemble/regime-aware variants are research extensions.

2. **`docs/M2_REPLAY_HARNESS_DESIGN.md`**: added "regime-stratified comparison" as an explicit section of the report (decisions and P&L bucketed by regime label). Also added "crash-period replay" as a specific recommended test (replay through a known volatile window like 2026-04 if it qualifies, to validate crash behavior).

3. **`docs/LLM_MODEL_CHARTER.md` open questions**: added "should the LLM prompt be regime-aware (separate templates per regime) vs regime-passive (single template, regime as a context field)?" — already had this as an open question in the interface spec; promoting to charter level.

### Things deliberately *not* changed

- We are not switching from LLM-driven signals to rules+ML signals based on this research. Our hypothesis is that LLM holistic reasoning may capture what hand-crafted ML feature engineering can't. CMM is a credible alternative paradigm worth knowing about, but pivoting now would be premature — we don't yet have results from the LLM approach to compare against.

- We are not building a Backtrader-based backtest. Our M2 harness is lighter and integrated with our existing pipeline; introducing Backtrader would add dependency weight without obvious benefit.

- We are not yet introducing scikit-learn or PyTorch dependencies. If we add an ensemble or regime-detection layer in M5+, we revisit.

---

## Template for future research entries

```
## YYYY-MM-DD — Topic

### Sources
- [link] [paper title]

### Key claims
- claim 1
- claim 2

### Our take
- analysis

### Design changes from this input
- file: change

### Things deliberately not changed
- decision: rationale
```
