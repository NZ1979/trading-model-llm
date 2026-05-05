# SSH-key auth setup for VPS access

**Goal:** stop hitting password / fail2ban traps on every VPS access. Replace SSH password auth with SSH key auth, then optionally disable password auth entirely.

**Why this hasn't happened yet:** earlier attempts to install an SSH public key on the VPS used the noVNC console paste mechanism, which mangles shifted symbols (`_`, `=`, `+`, `@`). The public key contains all of these. The walkthrough below uses paste.rs as the transit channel — it never touches noVNC paste with the key content.

**When to execute:** after market close (16:00 ET) on any day. Service restart is NOT required at any step. The walkthrough does not affect running trader.service.

---

## Pre-flight check

### In a normal PowerShell window on your Windows machine:

```powershell
Test-Path $HOME\.ssh\id_ed25519
```

- If `True`: you already have an SSH key from prior setup attempts. Skip Step 1.
- If `False`: do Step 1.

---

## Step 1 — Generate SSH key pair (skip if pre-flight returned True)

### In a normal PowerShell window on your Windows machine:

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\id_ed25519 -N '""'
```

The `-N '""'` sets an empty passphrase so it works frictionlessly with scp/ssh. After this, two files exist:

- `C:\Users\kings\.ssh\id_ed25519` — private key, stays on your machine, never leaves
- `C:\Users\kings\.ssh\id_ed25519.pub` — public key, safe to share

---

## Step 2 — Upload public key to paste.rs (transit channel, paste-safe)

### In a normal PowerShell window on your Windows machine:

```powershell
Get-Content $HOME\.ssh\id_ed25519.pub | curl.exe --data-binary "@-" https://paste.rs
```

paste.rs returns a URL like `https://paste.rs/AbCdE`. Copy that URL.

The public key is safe to put on paste.rs — it is, by design, public information. The corresponding private key never leaves your local machine.

---

## Step 3 — Install public key on VPS

### In the in-browser console connected to `root@trader-prod`:

Replace `<URL>` below with the paste.rs URL from Step 2:

```
mkdir -p /root/.ssh && chmod 700 /root/.ssh
```

```
curl -s https://paste.rs/<URL> >> /root/.ssh/authorized_keys
```

```
chmod 600 /root/.ssh/authorized_keys
```

Verify the key landed by checking the file's line count (should increase by 1 from before):

```
wc -l /root/.ssh/authorized_keys
```

(no credential exposure — just a count)

---

## Step 4 — Test passwordless SSH from PowerShell

### In a normal PowerShell window on your Windows machine:

```powershell
ssh -i $HOME\.ssh\id_ed25519 -o PasswordAuthentication=no root@5.161.199.155 "echo SSH_KEY_OK; hostname"
```

- The `-o PasswordAuthentication=no` flag forces SSH to use the key only — no fallback to password. This is the test for whether key auth works.
- If you see `SSH_KEY_OK\ntrader-prod`, key auth is working.
- If it errors with `Permission denied (publickey)`, the key isn't installed correctly — re-do Step 3.

---

## Step 5 — Set up SSH config alias (optional but recommended)

This lets you type `ssh trader-prod` instead of `ssh -i $HOME\.ssh\id_ed25519 root@5.161.199.155`.

### In a file editor — create or edit `C:\Users\kings\.ssh\config`:

Append this block (don't replace existing content):

```
Host trader-prod
    HostName 5.161.199.155
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

After saving, test:

```powershell
ssh trader-prod "echo OK"
```

Should print `OK` with no password prompt.

---

## Step 6 (optional, after key auth proven) — Disable password auth entirely

This eliminates fail2ban from the picture for SSH and prevents brute-force attempts.

### In an SSH session to the VPS (run from PowerShell):

```powershell
ssh trader-prod
```

Then on the VPS:

```
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
```

Verify the line:

```
grep PasswordAuthentication /etc/ssh/sshd_config
```

Should show: `PasswordAuthentication no`

Apply the change:

```
systemctl reload sshd
```

Then test from a NEW PowerShell window (not the existing SSH session):

```powershell
ssh trader-prod "echo OK"
```

Should still work (key auth). Then:

```powershell
ssh -o PreferredAuthentications=password root@5.161.199.155 "echo SHOULD_FAIL"
```

Should print `Permission denied (publickey)` — confirms password auth is disabled.

If anything goes wrong with Step 6, you can revert immediately FROM the existing SSH session:

```
sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl reload sshd
```

Don't disconnect the existing SSH session until you've verified key auth works from a new window. If you disconnect prematurely and key auth has any issue, you'd lock yourself out.

---

## After completion — what changes for future deploys

Every future VPS command becomes:

- `ssh trader-prod "<command>"` — runs the command and returns immediately
- `ssh trader-prod` then interactive — same as before

No more:
- Password prompts that get mangled by noVNC keyboard handling
- fail2ban locking your IP after 3 failed attempts
- Clipboard copy/paste of credentials

The deploy plans in `WAVE_DEPLOY_CHECKLIST.md` and future patch docs will reference `ssh trader-prod` as the canonical VPS access pattern, replacing in-browser-console-based deploy steps wherever possible. The in-browser console remains as the emergency fallback if SSH itself breaks.

---

## Rollback (if something breaks during setup)

If at any point during Steps 3-6 you can't access the VPS via SSH:

### In the Hetzner web UI in-browser console (always works regardless of SSH state):

```
nano /etc/ssh/sshd_config
```

Find any line starting with `PasswordAuthentication` and ensure it reads `PasswordAuthentication yes`. Save (Ctrl+O, Enter), exit (Ctrl+X).

```
systemctl reload sshd
```

Password auth restored. You can then re-attempt the SSH key install with corrected steps.
