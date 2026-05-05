# Setup on a New Machine

This document is the canonical setup guide for getting a working development environment for the Trading Platform on a fresh Windows machine. It assumes you have GitHub access to `NZ1979/trading-platform` and an existing trader-prod VPS at `5.161.199.155` (Hetzner CPX21).

The setup is split into three layers: a local development environment that lets you edit code and run sandbox tests, VPS access so you can deploy and inspect production, and (optional) Claude memory continuity so a fresh Claude install on the new machine inherits the operational lessons learned over time.

---

## Prerequisites — install before cloning

The following must be installed and on PATH before any of the steps below work. Versions matter; mismatches cause subtle errors that are not worth diagnosing.

**Git for Windows.** Download from https://git-scm.com/download/win. During install, accept defaults but choose "Use Git from the Windows Command Prompt" so PowerShell can call `git`. Verify with `git --version`.

**Python 3.12.** Specifically 3.12.x — matches the VPS. Download from https://www.python.org/downloads/. During install, check "Add Python 3.12 to PATH" on the first screen. Verify with `python --version` (must print `Python 3.12.x`).

**OpenSSH client.** Already included with modern Windows 10/11. Verify with `ssh -V` from PowerShell — should print an OpenSSH version. If missing, install from Settings → Apps → Optional Features → "OpenSSH Client".

**A code editor.** VS Code, PyCharm, or whatever you prefer. Not strictly required for setup but you will want one. If using VS Code, install the Python extension after first launch.

---

## Step 1 — Clone the repo

Open PowerShell and run:

```powershell
mkdir C:\trading
cd C:\trading
git clone git@github.com:NZ1979/trading-platform.git
cd trading-platform
```

If the clone errors with "Permission denied (publickey)", your new machine's SSH key is not yet registered with GitHub. Skip ahead to "VPS and GitHub SSH key setup" below, complete that, then come back here.

You can clone anywhere. `C:\trading\` is suggested because it sidesteps OneDrive sync issues that bit us during the 2026-05-04 deploy (OneDrive locks `.git/index.lock` files and breaks git operations from sandboxed Linux processes). If you keep your code in a non-OneDrive path, this entire class of bug stays away from you.

---

## Step 2 — Set git identity (one-time, machine-wide)

```powershell
git config --global user.name "Neale"
git config --global user.email "nealezingle@gmail.com"
```

This applies to every git repo on this machine, not just this one. Otherwise commits will be attributed to "Your Name" / "your-email@example.com" — the placeholder defaults that bit us before.

---

## Step 3 — Create Python virtual environment and install dependencies

From inside `C:\trading\trading-platform`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

If `Activate.ps1` errors with "running scripts is disabled on this system", run this once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then re-run `Activate.ps1`. This is a one-time Windows policy change and only affects scripts you write yourself.

Verify the install with:

```powershell
python -c "import alpaca, anthropic, polygon, pandas; print('ok')"
```

Should print `ok`. Any ImportError means a dependency didn't install — check `requirements.txt` matches the imports.

---

## Step 4 — VPS and GitHub SSH key setup

### Generate a key (skip if `C:\Users\<you>\.ssh\id_ed25519` already exists)

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\id_ed25519 -N '""'
```

Empty passphrase keeps `scp`/`ssh` frictionless. The trade-off is anyone with file system access to your machine could use your key — accept that risk for personal dev or use a passphrase if you're worried.

### Add the public key to GitHub

```powershell
Get-Content $HOME\.ssh\id_ed25519.pub
```

Copy the entire output line (starts with `ssh-ed25519 AAAA...`), then go to https://github.com → Settings → SSH and GPG keys → New SSH key. Title it something specific to this machine (e.g. "Windows-Desktop-Office"), paste the key, save. Test with:

```powershell
ssh -T git@github.com
```

Expected: `Hi NZ1979! You've successfully authenticated...`

### Add the public key to trader-prod

The VPS only accepts SSH keys that have been authorized. Each new machine needs its key added once. There are two ways:

**Easy path (you still have password access):** SSH in once with the password, append your new key to `authorized_keys`. From the new machine's PowerShell:

```powershell
Get-Content $HOME\.ssh\id_ed25519.pub | curl.exe --data-binary "@-" https://paste.rs
```

paste.rs returns a URL. Then SSH into trader-prod from your old machine (where key auth works) and run:

```bash
curl -s <paste.rs URL> >> /root/.ssh/authorized_keys
```

**Disaster path (no machine has working key auth):** Use the Hetzner web UI in-browser console. Same `curl >> authorized_keys` approach but you type the commands into the noVNC console. See `docs/SSH_KEY_SETUP.md` for the full procedure with paste-safe character handling.

### Configure the SSH alias

Create or edit `C:\Users\<you>\.ssh\config` and add:

```
Host trader-prod
    HostName 5.161.199.155
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

Test with:

```powershell
ssh trader-prod "echo OK; hostname"
```

Expected: `OK\ntrader-prod`. Now `ssh trader-prod` is your shortcut for everything.

---

## Step 5 — Local secrets (only if you want to run the platform locally)

The platform reads secrets from environment variables at startup. On the VPS they live in `/etc/trading-platform/env` and are loaded by systemd. On your dev machine you have two options:

**Option A: Don't run the platform locally at all.** This is the simplest workflow — make code changes locally, run unit tests in the sandbox, deploy to the VPS for any integration testing. The VPS is paper-account-only, so there's no live-money risk to running real API calls there.

**Option B: Run locally with a `.env` file.** Create `.env` in the repo root (it is already in `.gitignore` and will not be committed):

```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ANTHROPIC_API_KEY=...
POLYGON_API_KEY=...
FINNHUB_API_KEY=...
DATABENTO_API_KEY=...     # optional, only if futures.enabled=true
```

You will need to retrieve these from your password manager or by rotating them in each vendor's dashboard. Anthropic specifically only displays a key value at creation time, so if you don't have it saved you must create a new one.

Then load with `python-dotenv` (already in requirements.txt) at the top of `main.py` if not already present. Or set them per-shell with `$env:ALPACA_API_KEY = "..."`.

**Pitfall to avoid:** never copy keys through Microsoft Word — Word silently autoformats with smart quotes and invisible characters that break API calls. Copy directly from the vendor dashboard to PowerShell, or use a password manager that preserves plain text.

---

## Step 6 — Sandbox sanity check

Activate the venv if not already, then:

```powershell
python -m pytest tests/ -v
```

All tests should pass. If any fail, do not proceed to deploy work — fix the local environment first. The most common cause is a Python version mismatch (the project assumes 3.12 features) or a missing dependency (re-run `pip install -r requirements.txt`).

---

## Step 7 — Confirm VPS deploy access

```powershell
ssh trader-prod "systemctl is-active trader && systemctl status trader --no-pager | head -20"
```

Should print `active` followed by service status. If you see `inactive` or `failed`, something happened on the VPS that's unrelated to your machine setup — investigate the VPS state before deploying.

To pull recent logs:

```powershell
ssh trader-prod 'journalctl -u trader -n 50 --no-pager'
```

To deploy a code change (only after the WAVE_DEPLOY_CHECKLIST gates pass):

```powershell
scp main.py trader-prod:/opt/trader/app/main.py
ssh trader-prod "chown trader:trader /opt/trader/app/main.py && systemctl restart trader"
```

Read `docs/WAVE_DEPLOY_CHECKLIST.md` before any actual deploy. Specifically gates A–E run pre-deploy on your dev machine; gates F–I run on the VPS. Skipping gates is what caused the 2026-05-04 outage.

---

## Step 8 — Optional: Claude memory continuity

Claude's persistent memory (the file-based memory system) lives at:

```
C:\Users\<you>\AppData\Roaming\Claude\local-agent-mode-sessions\<session-id>\spaces\<space-id>\memory\
```

This memory holds operational lessons (e.g. "VPS file edits must be file-based, never inline shell hacks"), user preferences, and project context. On a fresh Claude install, the memory folder is empty and Claude starts blank.

To carry memory across machines, copy the entire `memory/` folder from your current machine to the equivalent path on the new machine after Claude is installed and a session has been initiated (so the parent session/space directories exist).

If you don't sync memory, Claude on the new machine will rebuild context by reading `CLAUDE_PREFLIGHT.md`, `docs/WAVE_DEPLOY_CHECKLIST.md`, and `docs/SSH_KEY_SETUP.md`. Those documents capture the operational rules in writing, which is the durable form. The memory folder gives Claude richer context (your role, your preferences, the specific failure modes from past sessions) but is recoverable if lost.

---

## Step 9 — Daily workflow

Once setup is complete, the working pattern is:

1. Open the repo in your editor
2. Make code changes
3. Run `python -m pytest tests/ -v` to confirm nothing breaks
4. Commit: `git add -A && git commit -m "Brief message"` (use prose for the body if the change is substantive)
5. Push: `git push`
6. If the change needs to go to production, walk the WAVE_DEPLOY_CHECKLIST gates, then deploy via `scp` + `ssh trader-prod` per Step 7

Always commit before deploying. The git history is your only safety net if a deploy needs to be rolled back.

---

## Common pitfalls

**"git push" fails with "Permission denied (publickey)"**: your new machine's SSH key wasn't added to GitHub. Re-do Step 4's GitHub key section.

**"ssh trader-prod" fails with "Permission denied (publickey)"**: your new machine's SSH key wasn't added to the VPS. Re-do Step 4's VPS key section.

**Python imports fail**: virtual environment isn't activated. Run `.\.venv\Scripts\Activate.ps1` from the repo root before any python command.

**OneDrive locking `.git/index.lock`**: don't put the repo inside OneDrive. Use a path like `C:\trading\` instead. If you must use OneDrive, do all git operations from PowerShell on Windows directly (never from a Linux subsystem or sandboxed bash), and accept that occasional lock-file errors will require manual `Remove-Item .\.git\index.lock` cleanup.

**Word document mangling API keys**: never copy API keys through Word. Smart quotes and invisible characters break authentication silently. Copy directly from vendor dashboards or use a password manager that preserves raw text.

**Inline ssh commands corrupting VPS files**: avoid `ssh trader-prod "sed -i ..."` patterns. Cross-shell quoting (PowerShell → ssh → bash → sed) silently loses or transforms characters. For any non-trivial VPS edit, write a bash script locally, `scp` it, run with `ssh trader-prod 'bash /tmp/script.sh'`, then `rm` it. This is captured in `docs/SSH_KEY_SETUP.md` and the project memory.

---

## Reference: where things live

| Concern | Location |
|---|---|
| Source code | This repo (`C:\trading\trading-platform`) |
| Production deploy | trader-prod VPS at `/opt/trader/app/` |
| Production secrets | trader-prod VPS at `/etc/trading-platform/env` |
| Production logs | `journalctl -u trader` on trader-prod |
| Production database | trader-prod VPS at `/opt/trader/app/trading.db` (SQLite) |
| Operational rules | `CLAUDE_PREFLIGHT.md` (project root) |
| Deploy procedure | `docs/WAVE_DEPLOY_CHECKLIST.md` |
| SSH setup | `docs/SSH_KEY_SETUP.md` |
| Project narrative | `docs/NARRATIVE_OVERVIEW.md` |
| Patch records | `docs/patches/` |
| Reusable building blocks for new strategies | `data/`, `analysis/`, `execution/`, `strategy/` modules |

---

## Reusing this codebase for new trading platforms

The modules under `data/`, `analysis/`, and `execution/` are designed to be lifted into other strategies with minimal modification. The `data/watchlist_builder.py`, `data/polygon_news.py`, and `data/finnhub_feed.py` are vendor abstractions that can be reused wholesale. The `analysis/sentiment.py` Claude-Haiku scoring framework is parameterizable. The 9-gate `WAVE_DEPLOY_CHECKLIST.md` is itself a reusable artifact for any production system.

For a new strategy (e.g. swing trading instead of intraday), the pattern is:

1. Branch this repo or fork it into a new repo
2. Replace `strategy/signal_engine.py` with new logic
3. Adjust `config/settings.yaml` for the new risk/sizing profile
4. Reuse the data ingestion, the orchestration in `main.py`, and the deploy infrastructure
5. Walk the WAVE_DEPLOY_CHECKLIST gates before going live

Every piece of this codebase is supposed to compose. Setup time for the second strategy should be a fraction of the first.
