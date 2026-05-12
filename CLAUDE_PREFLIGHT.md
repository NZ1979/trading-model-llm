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

## Rule 16: Always state where a command/script is to be run

Operational tightening of Rule 4. Every command block must declare its execution context explicitly, before the code, with no ambiguity.

**The rule:**
Every command or script Claude hands the user must be prefixed by one of the following labels (or an equivalent equally-explicit phrase). No naked code blocks.

- **"In a normal PowerShell window on your Windows machine:"** — for local PowerShell (Windows file paths, `curl.exe`, `scp`, etc.)
- **"In the in-browser console connected to `root@trader-prod`:"** — for the Hetzner web/noVNC console session into the VPS (Linux commands, no SSH layer in between, paste mangles shifted symbols)
- **"In an SSH session to the VPS (run from PowerShell):"** — when the user has opened SSH from PowerShell and is at the `root@trader-prod:~#` prompt over SSH (paste works normally, no noVNC quirks)
- **"In Python sandbox (Claude-side, not the user):"** — for code Claude is running on its own, not asking the user to execute
- **"In a file editor:"** — for content that goes into a file, not run as a command

If switching contexts within a single response, label EACH block — never assume the previous label still applies.

**Specific trap already fallen into:** During the 2026-05-02 deploy, Claude alternated between PowerShell commands and in-browser console commands without consistent labeling. The user repeatedly had to ask "where do I type this?" — once explicitly: "do i type those prompts into the in-browser console?" Each ambiguity cost a round-trip and compounded hours of frustration.

**How to apply:** Before sending any code block, ask: "If the user reads only the next message — no scrollback — will they know exactly which terminal window to type this into?" If not, add the label.

## Rule 17: User cannot create PDF files

The user has no PDF creation capability on their machine.

**The rule:**
Never ask the user to produce, export, or save anything as a `.pdf` file. When suggesting a destination format for user-authored content (notes, references, screenshots, exports), pick from formats they CAN create:

- `.docx` — Word documents (their preferred format for compiled screenshots/notes)
- `.md` — Markdown
- `.txt` — plain text
- `.png` / `.jpg` — individual images

When in doubt, default to `.docx`. Claude (with the docx skill) can read and reason about Word documents directly, so there's no functional cost vs PDF.

**Specific trap already fallen into:** Suggested the user save Finnhub API documentation screenshots as a `.pdf` for review on 2026-05-03. They corrected: "I can't create a .pdf file." Forced them to repeat themselves and broke the flow.

**How to apply:** When a future suggestion is "save this as X for me to review," verify X is in the user-creatable list above before sending. If you need a multi-page screenshot bundle, say `.docx`. Never say `.pdf`.

## Rule 18: Error handling philosophy — fail loud, never fake

Prefer a visible failure over a silent fallback. This applies to every piece of code, script, monitoring, or operational output Claude produces or recommends.

**The rule:**

Priority order, top to bottom:

1. **Works correctly with real data.**
2. **Falls back visibly** — clearly signals degraded mode (banner, log warning, annotated output, status field).
3. **Fails with a clear error message** — exception, non-zero exit code, explicit "FAILED" log line that's easy to grep.
4. **Silently degrades to look "fine"** — NEVER do this.

Specific applications to this project:

- Never silently swallow exceptions to "keep the loop running" without an `ERROR` log entry that names what swallowed and why.
- Never substitute placeholder data (zeros, last-known values, mocked responses) in production code paths without an explicit `WARN` or `DEGRADED` log line that the user can see in `journalctl`.
- Fallbacks are acceptable ONLY when disclosed. If a backfill misses 4/503 tickers, the EOD journal must say "499/503 backfilled, 4 missing: X, Y, Z" — not "Backfill complete."
- Tests that pass against mocked I/O are not proof of production readiness; label them `unit-tested` (per Rule 11) and disclose what was NOT tested (per Rule 12).
- Status outputs that hide failures behind aggregated success counts are anti-patterns. Show denominators and lists of failures.

**Specific traps already fallen into in this project:**
- The original `_run_baseline_backfill` looped sequentially fetching minute bars over 450 days per ticker, then resampled to daily. Most of the 503 tickers silently failed (only ~26 loaded). No log surfaced the failure rate; the user had to dig through SQLite to discover the gap.
- `_evaluate_and_execute` short-circuited on RTH<50 bars before checking the gap-and-go path, hiding the structural unreachability of gap-and-go entirely. The system "ran cleanly" but couldn't fire one of its two strategies. (Bug A.)
- The `SymbolState` defaults (`last_decision_action="Hold"`, `last_decision_setup="none"`) caused the dedup gate to suppress the first-decision-per-ticker log write. The decisions table looked empty for days; the user assumed bars weren't flowing. The pipeline was actually working — the silent dedup hid the activity. (Bug B.)
- The original `submit_bracket_order` raised on HTTP error without capturing the response body, so 422-level rejections looked like generic exceptions. The user couldn't tell which tickers Alpaca rejected and why. (Bug D.)

**How to apply:**
- Every `except Exception:` must end with `logger.exception(...)` or `logger.error(...)` that includes context (which ticker, which API, which iteration).
- Every counter that "skipped" something needs a paired log line listing what got skipped and why, OR a terse summary at the end of the run.
- Every fallback path needs a log entry that distinguishes it from the primary path, ideally with a `DEGRADED:` prefix.
- When showing a "summary" of N items processed, show the failure count and at least 3 example failures alongside, never just the success count.
- When recommending production deploys, list the failure modes that are still silent (per Rule 12).

## Rule 19: Stop on incomplete input — never compile a deliverable from partial data

When the input to a task is corrupted, partially missing, or fails an integrity check (OCR returns blank pages, a file is truncated, a fetch is partial, a parse drops fields, an upload list is short, etc.), Claude must STOP, surface the gap explicitly, and wait for the user's instruction. Never proceed to produce the deliverable with hedge phrases like "verify in live docs" or "assumes shape similar to..." or "this section was unparseable but the surrounding context covers it."

**The rule, hard:**
1. Before starting to compile/synthesize/write the deliverable, run an integrity check on the input. State the result explicitly: "Input integrity: complete" or "Input integrity: incomplete — N gaps in items A, B, C."
2. If integrity is incomplete, STOP. Do not write the deliverable. Output a short message that lists exactly what's missing, why it's likely missing, and present a small set of options for what to do (re-process with different settings, ask the user to re-supply, accept a reduced scope explicitly, etc.).
3. Wait for the user's choice. Do not proceed on inference about what they probably want.
4. Hedge phrases inside a delivered document do not satisfy this rule. "Verify in live docs," "appears similar to," "assumes shape of..." in a reference document mean the reference is contaminated. The bar for a saved reference file is *every claim is solid*, not "mostly solid with footnotes."

**Specific trap already fallen into (2026-05-03):** OCR'd 84 screenshots of Finnhub API documentation. Found that ~16 pages returned blank (image-heavy sections: pages 8, 27-34, 49, 53-58, 75-82). I noted the gaps inside the deliverable as hedged comments ("verify in live docs," "assumes similar shape to...") and pressed on to produce a "compiled" reference that the user explicitly named as the canonical reference for this and future projects. This silently embedded uncertainty into a long-lived artifact. The user — correctly — rejected the file as unfit because a reference document with footnoted gaps is not a reference document; it is a reference document plus a debugging task that future Claude or future user has to remember to do. The right move was: stop after seeing 16 blanks, list them, and ask whether to (a) re-OCR with higher DPI or different engine, (b) request re-capture of those pages, (c) supplement from live docs via WebSearch, (d) accept reduced scope and flag the file as "partial — sections X missing" up top with a TODO, or (e) something else.

**How to apply:**
- After any extract/fetch/parse step, the next action is an integrity check, not synthesis.
- For OCR/extraction tasks: count expected items vs returned items. If they disagree, stop.
- For multi-source fetches: confirm each source returned non-empty content before merging.
- For long-lived deliverables (reference docs, audit reports, plans): the integrity bar is higher than for ephemeral conversation. A hedge in chat is fine; a hedge in a saved file is contamination.
- The phrase "verify in live docs" in a reference document is a smell. If something needs to be verified before use, it does not yet belong in the reference.

## Rule 20: Audit your output for placeholders and unstated assumptions before sending

When generating a command, script, or instruction for the user to run, do a final pass that identifies every placeholder and every unstated assumption baked into the output. Either resolve them with concrete values (asking the user first if needed), or call them out explicitly so the user knows they need to substitute. Never ship an output that assumes the user will silently do the right thing with a placeholder.

**The rule, hard:**

Before sending a command/script to the user, scan the output for these failure modes:

1. **Literal placeholder strings** — e.g., `paste_your_key_here`, `<your_value>`, `REPLACE_ME`, `your_path_here`. If present, the command does NOT work as-pasted; the user must edit it. Either ask the user for the value first and bake it into the command, or call out the placeholder explicitly with a "you need to replace X with Y" line BEFORE the code block. A placeholder buried inside a quoted string is invisible; the user may run it as-is and get a confusing literal-string failure.
2. **Unstated input assumptions** — when you tell the user "save it locally" or "put it somewhere," you don't yet know HOW or WHERE they did. Different storage formats (plain text file, password manager, env var, .env file, sticky note) need different retrieval commands. Ask before writing the command.
3. **Path assumptions** — does the command assume a specific directory layout, file existence, or shell working directory? If so, either prefix with `cd "<absolute path>"` or state the assumption explicitly.
4. **Tool-availability assumptions** — does the command assume `python`, `git`, `curl`, etc. is on PATH? If you haven't verified, say so.
5. **State assumptions** — does the command assume a service is running, an env var is exported in the current shell, a previous step completed? If so, either include the prerequisite step or call it out.

**Specific trap already fallen into (2026-05-03):** After the user said "saved locally" about their Finnhub API key, I generated a PowerShell command containing the literal string `$env:FINNHUB_API_KEY = "paste_your_key_here_no_quotes_in_chat"` and instructed them to run it. The placeholder string was meant to be replaced, but the instruction didn't say so explicitly, and the user — reasonably — asked "how can this command work, we haven't told it where the api key is yet locally saved on my computer? is this an error you made?" The command does work IF you understand to substitute the placeholder, but I never confirmed where the key was actually saved (file path? password manager? in their head?), so I couldn't write a command that auto-reads it. The right move was to ask "where exactly did you save it?" before writing any command — different storage = different retrieval.

**How to apply:**

- Before ANY command/script with user-specific values goes out, ask: "Have I been told the actual value, or am I assuming the user will substitute it?" If assuming → ask first.
- If a placeholder must remain in the output (e.g., a key the user shouldn't paste in chat), wrap it in an explicit callout: "**REPLACE `<placeholder>` with your actual key value before running**" on the line immediately above the code block. Bare placeholders inside quoted strings are too easy to miss.
- For commands that depend on file paths or environment state, name the assumption: "This assumes you saved the key to `C:\Users\kings\...\finnhub_key.txt`. If it's elsewhere, tell me the path."
- When the user gives a vague status update ("done", "saved", "set up"), the next move is a clarifying question, not a command that depends on you guessing what they did.

## Rule 21: Never request command output that would expose credentials

When generating a verification or diagnostic command, audit the *output shape* of that command for credential exposure before sending. If the output would include any API key, password, token, secret, or other credential — even partially — never request the user paste it back. Always provide a redacted-equivalent command instead.

**The rule, hard:**

Before asking the user to paste back the output of any command, ask: "Could the output of this command contain a secret?" If yes, redesign the command to extract only the non-secret information needed for verification.

Specific patterns that ALWAYS need redaction before requesting output:

1. **`cat`, `tail`, `head`, `less`, `more`** on any file that holds credentials — env files (`/etc/*-platform/env`, `.env`), config files with embedded keys, `~/.ssh/`, `~/.aws/credentials`, `~/.kube/config`, password files, browser-saved-credentials exports
2. **`env`, `printenv`, `Get-ChildItem env:`, `set` (cmd.exe)** — these dump all env vars including credential ones
3. **`grep` matches on credential lines without redaction** — e.g., `grep "API_KEY" file` returns the full line
4. **Process listings** with credentials in args (`ps aux | grep ...` may include `--api-key=...`)
5. **Service unit files** with `Environment=KEY=value` lines (`systemctl cat`, `systemctl show -p Environment`)
6. **Application logs** that may include credentials in URLs, headers, error messages, or debug output
7. **HTTP request/response dumps** with `Authorization` headers or token query params
8. **Database dumps** of tables that store user secrets

Safe substitution patterns to verify credential setup without exposing:

- **Length-only**: `awk -F= '$1=="KEY_NAME" {print length($2)}' file` → returns `40` instead of the value
- **Existence-only**: `grep -c "^KEY_NAME=" file` → returns `1` or `0`
- **Last-N-chars only**: `awk -F= '$1=="KEY_NAME" {print substr($2, length($2)-3)}' file` → returns `bf40` (last 4 chars; useful as a fingerprint without exposing the secret)
- **Hash-only**: `awk -F= '$1=="KEY_NAME" {print $2}' file | sha256sum | cut -c1-16` → returns a hash prefix (constant for the same key, leaks nothing about the value)
- **Type-check only**: `awk -F= '$1=="KEY_NAME" {print ($2 ~ /^[a-z0-9]+$/) ? "lowercase-alphanumeric" : "OTHER"}' file` → confirms format without exposing value

**Specific trap already fallen into (2026-05-03):** Asked the user to paste back the output of `tail -3 /etc/trading-platform/env` to verify the new `FINNHUB_API_KEY=` line landed. The user correctly refused: "tail -3 shows Databento api key...I shouldn't paste that here, correct?" That env file contains FIVE credentials (Alpaca key + secret, Anthropic, Polygon, Databento, plus the new Finnhub) and `tail -3` would have exposed three of them in chat. The contradiction with my own earlier guidance ("treat the API key like a password — don't paste it in chat") makes this worse: I gave correct security advice and then asked the user to violate it 30 minutes later because I didn't audit the output shape.

**How to apply:**

- Before every `cat`/`tail`/`head` / `grep`-without-redaction / `env` listing / log dump request, check: would this command output include any credential? If yes, replace with a redacted alternative.
- When verifying that a new env-file line was added correctly, default to `awk` length checks and `grep -c` existence checks instead of pasting the line.
- When the user has multiple credentials in one file, the bar is even higher — adding one new credential should never require exposing the others to verify.
- When debugging service issues, prefer command outputs that show structure/counts/error categories rather than full content. If full content is genuinely needed, ask the user to redact secrets before sending.
- When reviewing logs with the user, suggest grep patterns that exclude credential-bearing log lines (e.g., grep -v 'token\|password\|key' or specific known-noisy patterns).

## Rule 22: Audit logging behavior for credential leaks before any deploy

When code makes outbound HTTP requests to vendor APIs, the logging behavior of every HTTP client library involved must be explicitly audited for credential exposure before the code is deployed. This is non-negotiable for any vendor whose API auth is keyed via URL query parameters, cookies, or any non-Authorization-header mechanism.

**The rule, hard:**

Before any deploy that touches outbound HTTP requests:

1. **Identify every HTTP client library used in the request path.** Common ones in this project: `httpx`, `aiohttp`, `urllib.request`, `urllib3`, plus any vendor SDKs (Anthropic, Polygon, Alpaca, Databento, Finnhub).

2. **Audit each library's default logging behavior.** Specifically check whether they log full URLs at INFO level. Known offenders:
   - **`httpx`** — logs full URLs (including query params) at INFO by default. **HIGH RISK** when paired with vendors that use URL-based auth.
   - **`anthropic` SDK** — uses `httpx` internally. Inherits the same risk.
   - **`aiohttp`** — does NOT log URLs at INFO by default. Lower risk.
   - **`urllib3`** (used by `requests`) — does NOT log URLs at INFO by default. Lower risk.
   - **`urllib.request`** — does NOT log URLs by default. Lower risk.

3. **Audit each vendor's auth scheme.** If the vendor passes credentials via URL query parameters (Polygon's `?apiKey=...`, some legacy APIs' `?token=...`), the risk is HIGH regardless of HTTP client. Switch to header-based auth if the vendor SDK supports it; if not, suppress URL logging in the offending library.

4. **Suppress URL logging at the application level.** Add this to `setup_logging` (or equivalent) for any outbound HTTP library used in the codebase:

   ```python
   for noisy_logger in ("httpx", "httpcore", "aiohttp", "anthropic", "urllib3"):
       logging.getLogger(noisy_logger).setLevel(logging.WARNING)
   ```

   This is a one-line audit — do it once and don't remove it. WARNING level keeps real errors visible while hiding routine request URLs.

5. **Verify in production logs after deploy.** Within 5 minutes of any deploy involving outbound HTTP requests, grep journalctl / log files for vendor URL patterns. Specifically search for `apiKey=`, `?token=`, `Authorization:` in log content. If you see any, the leak is live and the deploy should be rolled back, the credential rotated, AND the journalctl history vacuumed (`journalctl --vacuum-time=10s` to clear the leak from disk before any third party with read access to system logs grabs it).

**Specific trap already fallen into (2026-05-04):** Polygon Stocks Starter passes the API key as a URL query parameter (`?apiKey=...`). The platform's daily-routine backfill runs hundreds of Polygon REST calls per session. The Anthropic SDK (used by sentiment scoring) uses `httpx` internally, which by default logs full request URLs at INFO level. When the daily backfill ran post-restart, every Polygon REST call's URL was written to journalctl with the API key embedded. The leak was discovered when the user pasted a journalctl screenshot into chat for diagnostic purposes — exposing the credential to a third-party logging path. The leaked Polygon key required immediate rotation; the rotation hit a separate Polygon dashboard error that delayed the cutover; and the bug fix for the in-flight Wave 1A AttributeError compounded the deploy chaos. Direct cost: ~45 minutes of remediation, plus key rotation overhead, plus credential exposure in three places (journalctl history, screenshots, chat history).

**How to apply:**

- Before any deploy that adds new outbound HTTP requests to the codebase, run a code review with one specific question: "Will the URL of this request contain a secret?" If yes, ensure the relevant logger is suppressed.
- Whenever a new vendor SDK or HTTP library is added (even transitively as a dependency), audit its logging defaults.
- Treat URL-based auth as a default red flag; prefer Authorization headers when given the choice.
- Add the logger-suppression block to `setup_logging` once and don't remove it.
- For credentials that are already exposed in logs: rotate the credential AND vacuum journalctl history (the leak persists on disk until vacuumed).
- When working with user on log diagnostics, before asking them to paste any log lines, predict which vendors' calls would have triggered logging in the time window. If any of those vendors use URL-based auth and logger suppression isn't in place, redirect to a redacted query (per Rule 21) instead of asking for raw log paste.


## Rule 23: Verify actual system date/time before any time-anchored claim

Time changes during long sessions. Session env headers, prior messages, and recalled state go stale. Any claim that depends on the current date, time, day-of-week, or market session state must be grounded in a fresh `date` check at the moment the claim is made, not in remembered framing from earlier in the conversation.

**The rule, hard:**

Before any statement that includes phrases like "today", "tomorrow", "this morning", "market is closed/open", "we have N hours/minutes", "after market close", "before X happens", a deadline, or any market-session reference:

1. Run `date && TZ=America/New_York date` (or the equivalent on the user's shell) to capture actual current UTC + ET time.
2. Reason from those values, not from the session's env header (which is set at session start and drifts over multi-hour sessions) and not from anything you said earlier in the conversation.
3. State the verified time inline so the claim is auditable: e.g., "It is now Tue 10:33 AM EDT; market has been open for 1h 3m" rather than "Market is open."
4. For any trading-platform recommendation that depends on market state, the math is: regular hours 09:30–16:00 ET on weekdays, excluding US market holidays. Note holiday exceptions explicitly when relevant.

**Specific trap already fallen into (2026-05-12):** A long session started Monday evening EDT. By the next morning at 10:33 AM EDT, Claude told the user "Market closed since 16:00 ET. No rush — anytime before Tuesday 9:30 ET gets you back in time for tomorrow's open." Wrong on every clause: it was Tuesday at the time of the statement, market was open and had been for an hour, the user had asked to stop a service before market open and we were already past it. The session env header at start did contain the correct date (Tuesday May 12, 2026) but Claude carried forward Monday-evening framing without rechecking. Real impact: trading hours were lost while the user investigated.

**How to apply:**

- For ANY response that includes a time/date reference, run `date` first or use a time-aware tool. Never state market state from memory.
- For long sessions specifically, recheck the time periodically — what was "evening" when the session started may be "morning" hours later.
- If a user reports an unexpected state ("the service isn't trading"), the FIRST diagnostic is `date` followed by `systemctl is-active` or equivalent. Don't theorize from stale time-context.
- Phrase time-anchored statements with the verified value embedded. The embedded timestamp makes the claim auditable and forces the verification step to actually happen.
