# Claude Pre-Flight Checklist

This file exists because Claude wasted hours of my time during deployment with instructions that were stale, untested, or confidently wrong. Future Claude sessions: read this before giving me ANY operational instruction. Apply every rule that's relevant. No exceptions.

## Rule 1: Search before claiming current state about ANY external service

Before telling me to click X in service Y, search the web for the current UI of service Y. Services rebrand, reorganize, and rename their menus constantly. Your training data is months out of date.

**Examples of services this applies to:**
- Hetzner Cloud Console — they recently renamed CPX21 to CX23 (cost-optimized tier), and Cost-Optimized isn't available in all locations
- Anthropic Console — Billing moved from "Manage" sidebar to nested under "Settings"
- Alpaca — paper vs live URLs, dashboard layouts change
- Polygon — they rebranded to "Massive" but kept polygon.io
- Databento — pricing tiers and license questionnaire flow change
- AWS — Lightsail vs EC2 dashboard layout, IAM roles
- GitHub — UI shifts often

**The rule:** Always search "current [service] [task] 2026" before instructing. Direct URLs to a known landing page are safer than navigation instructions. If giving navigation, give it as one option of several, with a fallback direct URL.

## Rule 2: Verify pricing AND tier eligibility, both, every time

Pricing tiers change. Geographic availability of tiers changes. Don't assume a plan exists in a location until you've checked.

**Specific traps I've already fallen into:**
- Polygon Stocks Starter is delayed 15-min, not real-time (told user wrong initially)
- Databento CME pricing went from usage-based ($25/mo estimate) to flat ($179/mo) in April 2025
- Hetzner Cost-Optimized isn't available in Ashburn

**The rule:** When recommending a paid tier, search for: (a) current price, (b) what's actually included, (c) location/region availability. Lock in the choice with explicit cost numbers; don't say "approximately."

## Rule 3: Test every script before pasting it to me

You have a Python sandbox. Use it.

**Mandatory checks before sending any script that modifies config files or persistent state:**
1. Run the script in the sandbox with mock inputs
2. Re-parse/re-load the output to verify the file is still valid
3. Test edge cases relevant to the data (e.g., is the input list known to contain reserved keywords?)

**Specific traps already fallen into:**
- Watchlist update script didn't quote tickers → YAML parsed `ON` (ON Semiconductor) as boolean
- First sed regex required exact line match, didn't account for trailing comments → silently failed to edit

**The rule:** If you generated a regex or a YAML/JSON/TOML mutator, you owe me a sandbox-test of it BEFORE I run it on production.

## Rule 4: Know the difference between sandbox/local and the user's environment

Don't tell me to run commands "locally" without first asking which OS and shell I'm on.

**Specific traps already fallen into:**
- Gave Linux-style commands assuming user was on macOS/Linux when user was on Windows PowerShell
- Suggested heredocs (`cat <<EOF`) for a user pasting into PowerShell, which doesn't support heredocs natively
- Assumed `~/.ssh/` directory existed; it didn't
- Confused running commands "in PowerShell" vs "in SSH session connected to VPS" — let user run Linux commands in Windows PowerShell

**The rule:** When introducing a new command, say WHERE it runs ("in PowerShell on Windows", "in the SSH session to the VPS", "in the Python REPL"). Distinguish clearly. Never assume.

## Rule 5: Distinguish "what I think is true" from "what I just verified"

Use these markers explicitly in instructions:
- "Verified just now via search:" — followed by what you confirmed
- "From training (may be stale):" — followed by what you're recalling
- "Best guess:" — followed by reasoned inference

If I'm about to spend money or time on an instruction, I deserve to know which category it falls into.

## Rule 6: When the user reports something doesn't work, don't iterate blindly

If a command fails, don't immediately give a slightly different command. Instead:
1. Read the actual error message carefully
2. Identify the exact failure mode
3. Decide: is this a typo (OK to retry), a missing prerequisite (must fix prerequisite first), an environmental issue (must change approach), or a bug in my instruction (must apologize + fix)
4. State which category and act accordingly

**Specific trap already fallen into:**
- User pasted my zip-attached file instructions, the file presentation didn't render in their UI, I retried the same approach 3 times before switching to base64

## Rule 7: Don't lock in defaults silently

If I'm offered a choice early in a conversation and we pick option A, don't keep using option A 50 turns later without checking whether it still makes sense.

**Specific trap already fallen into:**
- Watchlist of 30 symbols was a "safe paper validation" default I picked at Phase 1
- Never raised it again as a thing to revisit
- User had to ask "wait, why only 30?" themselves at the end

**The rule:** When making a default choice, mark it as "decision: <X>, revisit before going live" so it surfaces at the natural review point.

## Rule 8: Token efficiency rules for this user specifically

User preferences from this project:
- Direct/concise. No filler. No "great question."
- Less em dashes.
- Step-by-step with confirmation between steps.
- Specific to their situation, not generic.
- Recommendations not "it depends."

## Rule 9: For VPS / production deployment specifically

This project's VPS already exists at: **5.161.199.155** (Hetzner Ashburn, Ubuntu 24.04).

Trader user, code at `/opt/trader/app`, venv at `/opt/trader/.venv`, secrets at `/etc/trading-platform/env`, systemd unit `trader.service`.

SSH from Windows PowerShell:
```powershell
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155
```

For routine operations (no SSH needed), commands are wrapped via SSH on stdin:
```powershell
ssh -i $env:USERPROFILE\.ssh\hetzner_trader root@5.161.199.155 "<linux command>"
```

Most common operational commands user will run:

| Task | Command |
|---|---|
| Check service health | `systemctl status trader.service --no-pager` |
| Tail live logs | `journalctl -u trader.service -f` |
| Read today's journal | `cat /opt/trader/app/journals/$(date +%F).md` |
| Restart service | `systemctl restart trader.service` |
| Edit settings | `nano /opt/trader/app/config/settings.yaml` then restart |
| Re-enable futures (after Databento fixed) | `sed -i 's/^  enabled: false.*/  enabled: true/' /opt/trader/app/config/settings.yaml && systemctl restart trader.service` |
| Update code from local | `scp -i $env:USERPROFILE\.ssh\hetzner_trader -r * root@5.161.199.155:/opt/trader/app/` then ssh + restart |

## Rule 10: Active deployment state (re-diagnosed 2026-04-29 AM ET)

- ✅ Platform deployed and running (Hetzner CPX21, Ashburn VA)
- ✅ Watchlist: full S&P 500 (503 symbols)
- ✅ News pipeline + Alpaca SIP bars + sentiment scoring: ACTIVE
- ✅ Backfill working (499/503 daily contexts on 2026-04-28; sentiment table has 73/305/169 rows for Apr 27/28/29 confirming the service has been alive across all three days)
- ⚠️ **OBSERVABILITY GAP** (re-diagnosed 2026-04-29): the prior "Alpaca SIP bars WebSocket died silently" claim was a misdiagnosis. It was inferred from a grep that searches keywords (`data.alpaca_market_data`, `data.bar_aggregator`, `Buy|Sell|Hold|Task`) that don't fire during normal runtime, so it was a false-negative regardless of pipeline health. The real blocker is `SymbolState` defaults in `main.py` (`last_decision_action="Hold"`, `last_decision_setup="none"`) matching the steady-state Hold/none decision the engine emits, which causes the dedup gate in `_evaluate_and_execute` to skip the first-decision-per-ticker write. Decisions table has been empty for that reason, not because bars aren't flowing. See `PROJECT_BLUEPRINT.md` section 5 for the full re-diagnosis and the two-line fix.
- ⚠️ Task supervisor patch (`_task_supervisor` in `main.py`, deployed 2026-04-28 ~6 PM ET): `unverified` defensive code per Rule 11. Only catches *completed/raised* tasks; would not detect a WebSocket hung in `await ws.recv()`. Keep, but don't cite as a confirmed fix.
- ⏸ Databento subscription CANCELED on 2026-04-28 (Standard tier excludes live MBP-10, Plus tier $1,500/mo too expensive). Futures wall scanner code is dormant. `enabled: false` in config.
- ⏸ Phase 7 (Polygon options walls): DEFERRED. User wants per-stock call/put walls based on options open interest. Build gated on 1+ week clean paper data + Polygon Options Starter subscription ($29/mo) + scale-tested fetch logic.
- 📋 Anthropic credits: top up via https://console.anthropic.com/settings/billing as needed. Burn rate at 503-watchlist scale ≈ $5-15/day.
- 📋 First clean trading day target: Thursday 2026-04-30 (after dedup fix is deployed Wednesday 2026-04-29 PM after market close).
- 📋 Daily journal: `/opt/trader/app/journals/<YYYY-MM-DD>.md`

## How to use this file

If you (Claude in a future session) see this file in the project, you've read it. Now apply Rules 1-13 to whatever the user asks. If the user reports an instruction failed, check whether you violated one of these rules first. Apologize concretely (which rule, what specific oversight). Don't apologize abstractly ("sorry for the confusion") and move on to a new mistake.

If the user adds a new failure mode I should learn from, append it to this file as Rule N+1 with a specific trap example.

## Rule 11: Label every claim with its testing depth

Never say "tests pass" without specifying which level. Use these labels explicitly:
- `unit-tested`: mocked inputs, synthetic data, single function in isolation
- `integration-tested`: real API or real adjacent component, single-call scope
- `scale-tested`: real API at production volume (e.g., not 30 tickers when production is 503)
- `unverified`: didn't test, best guess from training

**Specific trap already fallen into:** During initial build, I ran 11 unit tests on `evaluate_trade()` and called the system "tested at 95% confidence." When deployed, the backfill failed at 503-ticker scale because I never ran scale-tests with real Polygon API. Sequential code worked fine for 30 tickers and broke catastrophically at 503.

## Rule 12: Before any deploy recommendation, list what was NOT tested

Even one item on the not-tested list means confidence is below 95%. State the list explicitly. Examples of categories that almost always need explicit "not tested" disclosure:
- Real-API integration at production scale
- End-to-end timing under realistic load
- Failure-mode behavior (what happens when the API returns 429? when network drops mid-stream? when one input is malformed?)
- WebSocket long-running stability (zombie connections, silent task death)

## Rule 13: Verify the calendar date before claiming temporal context

Before saying things like "yesterday," "this week," "2 weeks ago," "today is Sunday," check the actual current date from the system. The conversation may span multiple days, the user's calendar context may differ from yours, and made-up time references erode trust faster than other errors.

**Specific trap already fallen into:** Said "2 weeks of confused decisions" about the Databento debacle when it had been 2 days. Called Sunday/Monday wrong on 2026-04-27 even though the system date was visible in context. Both errors landed in messages where I was *trying* to be careful — the unchecked temporal claim slipped through alongside a substantive correct point.

## Rule 14: Verification before conclusion

Tightens Rules 5, 11, and 12 into a single hard prerequisite for any "fix" or "diagnosis" claim.

**The rule:**
1. NEVER present a diagnostic claim, root cause, or fix as a conclusion until it has been **tested and verified against real data or output** in this session.
2. Until verified, mark every finding explicitly as `HYPOTHESIS:` or `UNVERIFIED:` in the message itself. Don't bury the qualifier in a footnote.
3. When the work is "the bug is X" or "the patch fixes Y," produce **a runnable reproducer that demonstrates the failure and a re-run that demonstrates the fix**, both with output captured. No reproducer ⇒ no claim.
4. End-to-end claims ("the platform is working / broken") require an end-to-end execution, not module-level inference.
5. If a verification step is impractical (no creds, no data, can't run safely), say so and downgrade the claim to `UNVERIFIED:` — do not silently restate it as a conclusion later in the conversation.

**Specific trap already fallen into:** Diagnosed "WebSocket died silently on 2026-04-28" as the production blocker based on a grep that searched for log keywords that don't actually fire during normal runtime. The "evidence" was an empty grep result that proved nothing in either direction. Two follow-up "found the bug" claims (the dedup-default issue and the require_walls_for_pullback config flag) were also stated as conclusions before being tested; one of them turned out to be true in the local file but already-correct on the VPS, so the conclusion was wrong. The user pushed back: across 503 stocks × 2 RTH days, zero qualifying setups is statistically near-impossible — meaning the diagnosis must have been incomplete or wrong, and presenting it as a conclusion wasted hours.

**How to apply:** Before any sentence that starts "the bug is," "the cause is," "this fixes," or "the platform is," you owe me an executed reproducer or you owe me the `UNVERIFIED:` label. No third option.

## Rule 15: Shell script authoring

Operational complement to Rule 3 (test scripts before pasting) and Rule 4 (know the environment), shaped by the specific paste-and-quoting failures from this project.

**The rule:**
1. **For any script longer than ~10 lines or with multi-line strings: write it to a file using the Write/Edit tool first, then execute the file.** Do not construct it inline as a heredoc paste from PowerShell into bash. Terminal line-wrapping during paste can split the heredoc and run partial commands.
2. **Validate before running.** For Python: `python3 -m py_compile <file>` (or `import py_compile; py_compile.compile(..., doraise=True)`). For bash: `bash -n <file>`.
3. **For Cowork-paste delivery (running a script on a remote VPS via SSH from Windows PowerShell):** never embed a multi-line bash heredoc in a PowerShell `@'...'@` literal. Instead, either (a) write a small `.py` or `.sh` file via `nano` on the remote, or (b) use `Write` to put it in the workspace and `scp` it. Avoid the PowerShell-→-bash double-quoting layer entirely when content has nested quotes.
4. **Sandbox-test the script in the local sandbox before sending it to the user**, against the documented response shapes of any external API it calls. The script should run end-to-end with mocked inputs and produce expected output. Only after that does it leave my hands.
5. **No multi-line `print(f"... " f"...")` style continuations** in user-pasted scripts. They get mangled by terminal paste even when the surrounding heredoc is intact.

**Specific trap already fallen into:** Repeatedly handed the user shell snippets that broke on paste — first a PowerShell heredoc with nested quoting, then a base64 single-liner that exceeded what the terminal would paste atomically and got line-broken into two separate commands. Each broken paste cost the user a round-trip to send back the failure. The cumulative friction is what ended the diagnostic session, not the underlying bug.

**How to apply:** When the next-step action is "run this on the VPS," default to the file-based path. Write the script. Test it. Then tell the user `nano /tmp/foo.py`, paste, save, run. Don't try to be clever with one-liners on the second attempt; switch tools immediately.
