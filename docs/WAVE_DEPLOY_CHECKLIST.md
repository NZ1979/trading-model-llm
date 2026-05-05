# Wave Deploy Checklist

Required for every Wave / Phase / Bug-fix deploy that touches:
- Outbound HTTP requests
- New vendor SDK or HTTP library
- New env vars / credentials
- Service restart

This checklist supersedes ad-hoc deploy plans. Every step must complete (or be explicitly marked N/A with reason) before the service is restarted.

---

## Pre-deploy gates (do all, in order)

### Gate A — Code review (Claude-side, before any user-facing command)

- [ ] `py_compile` clean on every modified `.py` file
- [ ] All sandbox tests pass; results pasted in chat as proof
- [ ] **Rule 14 audit**: every "the bug is X" or "this fixes Y" claim has a runnable reproducer. No hedge words like "should work" in deploy narrative.
- [ ] **Rule 11/12 audit**: each modified function tested. Explicit list of what was NOT tested. Integration tests documented separately from unit tests.
- [ ] **Rule 19 audit**: any reference document touched does not contain "verify in live docs" / "assumes shape similar to" / placeholder language. Hedge phrases mean contamination; rebuild the source instead.

### Gate B — Logging audit (Rule 22)

- [ ] Identified every HTTP client library in the request path of new code
- [ ] Each library checked against the known-offenders list:
  - `httpx`, `anthropic` SDK → log URLs at INFO; **must suppress**
  - `aiohttp`, `urllib3`, `urllib.request` → safe at INFO by default
- [ ] If any vendor passes credentials via URL query params (e.g. Polygon's `?apiKey=...`), the application has the logger-suppression block in `setup_logging`:

  ```python
  for noisy_logger in ("httpx", "httpcore", "aiohttp", "anthropic", "urllib3"):
      logging.getLogger(noisy_logger).setLevel(logging.WARNING)
  ```

- [ ] If new vendor SDK is being added, its dependency chain is audited (the SDK might use httpx internally)

### Gate C — Credential surface audit

- [ ] No new credentials are passed via URL query strings in code paths
- [ ] All env vars referenced in code are documented in deploy notes
- [ ] If new env var is required, the deploy plan includes how it gets onto the VPS without exposure (SSH-key piping or in-browser console with paste-safe encoding)
- [ ] No verification command in the deploy plan would emit a credential to chat (Rule 21). Use `awk` length / `grep -c` existence instead.

### Gate D — Execution-context label audit (Rule 16)

- [ ] Every command block in the deploy plan is prefixed with one of the explicit context labels:
  - "In a normal PowerShell window on your Windows machine:"
  - "In the in-browser console connected to `root@trader-prod`:"
  - "In an SSH session to the VPS (run from PowerShell):"
  - "In Python sandbox (Claude-side, not the user):"
  - "In a file editor:"
- [ ] No naked code blocks. No "run this" without specifying where.

### Gate E — Placeholder + assumption audit (Rule 20)

- [ ] No literal placeholder strings in commands (`paste_your_key_here`, `<your_value>`, etc.)
- [ ] Path assumptions stated explicitly
- [ ] Tool-availability assumptions noted (Python on PATH, sqlite3 CLI, etc.)
- [ ] If user-supplied values are needed, they are obtained from the user FIRST and baked into the command before sending

---

## Deploy execution gates (do all, in order)

### Gate F — Pre-flight on VPS

- [ ] SSH or in-browser console reachable
- [ ] No fail2ban block on the user's IP (`fail2ban-client status sshd` shows the IP is not banned, OR password-less SSH key auth eliminates this concern entirely — see SSH_KEY_SETUP.md)
- [ ] Disk space sufficient (`df -h /opt/trader/app`)
- [ ] Service status pre-deploy noted

### Gate G — Atomic deploy

- [ ] All files transferred (paste.rs URLs verified character-by-character)
- [ ] `py_compile` validates each new/changed `.py` file ON THE VPS (not just sandbox — VPS pandas/Python versions may differ)
- [ ] `chown trader:trader` applied to all new files
- [ ] Service restart issued

### Gate H — Post-restart verification (within 5 min of restart)

- [ ] Boot logs grep'd for: `Watchlist:`, `booted`, `equity:`, `Finnhub client init`, `AttributeError`, `Traceback`
- [ ] **Logging audit verification**: grep journalctl for vendor URL patterns to confirm no credential leak
  - `journalctl -u trader --since "5 minutes ago" -g "apiKey=" --no-pager` should return nothing
  - `journalctl -u trader --since "5 minutes ago" -g "?token=" --no-pager` should return nothing
- [ ] Decisions table check (Python query, not SQLite CLI): non-zero count after first 5-minute bar window passes
- [ ] No `ERROR` lines in the first 60 seconds of post-restart logs

### Gate I — 24-hour soak

- [ ] Service still running 24h after deploy (`systemctl status` active)
- [ ] No new ERROR-level log entries in 24h
- [ ] Decisions table shows expected daily volume
- [ ] If failure surfaces during soak, the rollback path is: revert to previous main.py via git or paste.rs backup; restart; verify

---

## Specific application to upcoming waves

### Wave 1B (Finnhub News Sentiment + Major Press Releases + Company News)

Every endpoint goes through the existing `FinnhubClient` (data/finnhub_feed.py). FinnhubClient uses `aiohttp` (lower risk per Gate B) AND passes the API key via URL query parameter `?token=...` (HIGH risk per Gate C if any future logging change exposes URLs).

**Wave 1B-specific Gate B requirement:** verify the existing `setup_logging` already suppresses `aiohttp` (we added it as part of the 2026-05-04 fix). If yes, no additional logger work needed for Wave 1B. If for any reason `setup_logging` regresses, Wave 1B must not deploy until that block is restored.

**Wave 1B-specific Gate H requirement:** after deploy, verify `journalctl -u trader --since "5 minutes ago" -g "token=" --no-pager` returns nothing. If it returns lines, the Finnhub token leaked — rotate immediately.

### Phase B (Dynamic watchlist) — already partially deployed 2026-05-04

Pending re-deploy after current production issues are resolved. Same Gate B applies: the `_fetch_grouped_daily` function in `data/watchlist_builder.py` uses `aiohttp` AND passes Polygon's API key via URL query string. The `setup_logging` suppression block already in place protects this — must remain.

### Phase C (Per-ticker PM RVOL threshold)

No new outbound HTTP. Gate B is N/A. Other gates apply.

### Future vendor additions (Track 3 if any)

Any new vendor SDK addition triggers the full Gate B audit before deploy.

---

## Rollback plan (must be ready before deploy)

- [ ] Previous version of changed files saved (paste.rs backup OR git tag before deploy)
- [ ] Rollback procedure documented in the patch doc
- [ ] One-command rollback path: `curl -o <file> <previous_paste_rs_url>` → `chown` → `systemctl restart trader`

If any post-deploy check fails, execute rollback within 10 minutes. Don't iterate forward on broken state.

---

## Rule references (read CLAUDE_PREFLIGHT.md for full context)

This checklist enforces:
- **Rule 11**: Label every claim with testing depth
- **Rule 12**: Before deploy, list what was NOT tested
- **Rule 14**: Verification before conclusion
- **Rule 16**: Always state where a command runs
- **Rule 18**: Fail loud, never fake
- **Rule 19**: Stop on incomplete input
- **Rule 20**: Audit output for placeholders + unstated assumptions
- **Rule 21**: Never request output that exposes credentials
- **Rule 22**: Audit logging behavior for credential leaks before deploy
