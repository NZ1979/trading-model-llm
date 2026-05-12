"""One-shot smoke test for Alpaca paper API credentials.

Hits https://paper-api.alpaca.markets/v2/account with the APCA-API-KEY-ID
and APCA-API-SECRET-KEY headers, prints account_number / status / equity /
buying_power, and exits 0 on HTTP 200.

Use this to verify Alpaca credentials right after rotation. It exercises
the same auth path that AlpacaBarStream, AlpacaNewsFeed, and
submit_bracket_order all rely on, so a 200 here means all three will
authenticate when the platform starts.

Exits 0 on HTTP 200 with status=ACTIVE and no account blocks.
Exits 1 on any failure (missing env var, network error, non-200 response,
non-ACTIVE status, or any blocked flag).

Per PROJECT_BLUEPRINT.md the platform is paper-only. PAPER_ENDPOINT below
is intentionally hardcoded — never flip to live without an explicit user
request and a separate confirmation pass.

Run locally (Windows PowerShell, after activating venv):

    $env:ALPACA_API_KEY = "PK..."
    $env:ALPACA_API_SECRET = "..."
    python scripts/verify_alpaca.py

Run on the VPS (the env file is normally injected by systemd; for a
manual run, source it as root first):

    set -a && . /etc/trading-platform/env && set +a
    /opt/trader/.venv/bin/python /opt/trader/app/scripts/verify_alpaca.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Paper-only by design. Per PROJECT_BLUEPRINT.md and CLAUDE.md, never flip
# to live without an explicit user request + separate confirmation pass.
PAPER_ENDPOINT = "https://paper-api.alpaca.markets/v2/account"
TIMEOUT_SECONDS = 10


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(
            f"FAIL: {name} not set in env. On the VPS, source "
            f"/etc/trading-platform/env first. Locally, set "
            f"$env:{name} in PowerShell before running.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def main() -> int:
    key = _require_env("ALPACA_API_KEY")
    secret = _require_env("ALPACA_API_SECRET")

    # Print non-sensitive metadata so a failure leaves a paper trail of
    # what was attempted. 2-char prefix is public info (PK=paper, AK=live).
    print(f"Endpoint     : {PAPER_ENDPOINT}")
    print(f"Key length   : {len(key)} (prefix={key[:2]})")
    print(f"Secret length: {len(secret)}")

    req = urllib.request.Request(
        PAPER_ENDPOINT,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"\nFAIL: HTTP {exc.code} from Alpaca", file=sys.stderr)
        print(f"Response body: {body[:200]}", file=sys.stderr)
        if exc.code == 401:
            print(
                "\n401 unauthorized usually means credentials don't match "
                "what Alpaca has stored. Most common cause during a rotation "
                "is a typo from hand-typing into nano. Don't visually compare "
                "70 random characters — push the keys via scp from a paste-safe "
                "channel (browser → PowerShell → scp → surgical env-file merge).",
                file=sys.stderr,
            )
        elif exc.code == 403:
            print(
                "\n403 forbidden usually means an account-state restriction "
                "(trading_blocked, account_blocked, ACH return holds, etc.), "
                "not a credential issue. Check the Alpaca dashboard.",
                file=sys.stderr,
            )
        return 1
    except urllib.error.URLError as exc:
        print(f"\nFAIL: network error reaching Alpaca: {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nFAIL: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if status != 200:
        print(f"\nFAIL: unexpected HTTP {status} (expected 200)", file=sys.stderr)
        return 1

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"\nFAIL: response was 200 but body was not JSON: {exc}", file=sys.stderr)
        return 1

    print()
    print("OK — Alpaca paper auth verified")
    print(f"  account_number  : {data.get('account_number')}")
    print(f"  status          : {data.get('status')}")
    print(f"  equity          : ${float(data.get('equity') or 0):,.2f}")
    print(f"  buying_power    : ${float(data.get('buying_power') or 0):,.2f}")
    print(f"  trading_blocked : {data.get('trading_blocked')}")
    print(f"  account_blocked : {data.get('account_blocked')}")
    print(f"  created_at      : {data.get('created_at')}")

    # Fail-loud on any account state that would block live trading even
    # though auth succeeded. Per Rule 18 of CLAUDE_PREFLIGHT.md.
    if data.get("status") != "ACTIVE":
        print(
            f"\nFAIL: account status is {data.get('status')!r}, expected 'ACTIVE'",
            file=sys.stderr,
        )
        return 1
    if data.get("trading_blocked") or data.get("account_blocked"):
        print(
            f"\nFAIL: account is blocked "
            f"(trading_blocked={data.get('trading_blocked')}, "
            f"account_blocked={data.get('account_blocked')})",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
