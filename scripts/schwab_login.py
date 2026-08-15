"""One-time (weekly) interactive Schwab OAuth login.

Schwab refresh tokens hard-expire after 7 days with no programmatic renewal.
Run this whenever `get_health` reports AUTH_EXPIRED or WARN_EXPIRING — ideally
Sunday before the open, so re-auth happens on your schedule rather than
mid-session on Monday.

Run from C:\\trading\\LLM model with the venv active:

    python -m scripts.schwab_login
    python -m scripts.schwab_login --status    # check without authenticating

A browser window opens to Schwab's login page. Log in and approve; the local
callback server at the registered callback URL catches the redirect and
schwab-py writes the token file. Nothing is printed that could reveal a
credential.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.schwab_auth import (  # noqa: E402
    DEFAULT_ENV_PATH, AuthState, auth_state, get_client, health,
    load_credentials,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Rule 22: Schwab returns the OAuth authorization code as a URL query
    # parameter, and werkzeug logs full request lines at INFO. Without this
    # the auth code lands in whatever captures stdout.
    for noisy in ("werkzeug", "flask", "authlib", "schwab", "httpx",
                  "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true",
                    help="report auth state and exit without authenticating")
    ap.add_argument("--manual", action="store_true", default=True,
                    help="paste-the-URL flow, no local callback server "
                         "(default; required on this machine)")
    ap.add_argument("--auto", dest="manual", action="store_false",
                    help="automatic flow with a local Flask callback server. "
                         "Known to fail on Godzilla under Python 3.14 because "
                         "pip's vendored truststore patches ssl.wrap_socket "
                         "and breaks server-side sockets.")
    args = ap.parse_args()

    setup_logging()

    creds = load_credentials()
    if creds is None:
        print(f"FAILED: Schwab credentials not found.\n"
              f"  Expected: {DEFAULT_ENV_PATH}\n"
              f"  Required keys: SCHWAB_API_KEY, SCHWAB_APP_SECRET, "
              f"SCHWAB_CALLBACK_URL, SCHWAB_TOKEN_PATH", file=sys.stderr)
        return 2

    before = health(creds)
    print("Auth state before:")
    print(json.dumps(before, indent=2))

    if args.status:
        return 0 if before["auth_state"] in (
            AuthState.OK.value, AuthState.WARN_EXPIRING.value) else 1

    state, _ = auth_state(creds)
    if state is AuthState.OK:
        print("\nToken is current. Re-authenticating anyway resets the 7-day "
              "clock, which is usually what you want before a trading week.")

    print(f"\nCallback URL registered for this app: {creds.callback_url}\n"
          f"It must match exactly or Schwab returns 'invalid URI specified'.")

    if args.manual:
        print("\nMANUAL FLOW. schwab-py will print a Schwab URL below.\n"
              "  1. Open it, log in, and approve.\n"
              "  2. The browser will land on a 127.0.0.1 page that fails to "
              "load. That is expected — nothing is listening there.\n"
              "  3. Copy the FULL address bar contents and paste it here.\n"
              "It contains a single-use authorization code, so do not paste "
              "it anywhere else.\n")
    else:
        print("\nAUTOMATIC FLOW. A browser opens and a local callback server "
              "catches the redirect. Blocks until it arrives or times out.\n")

    try:
        client = get_client(creds, allow_interactive=True, manual=args.manual)
    except Exception as exc:  # noqa: BLE001 - surface anything, loudly
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("\nCommon causes:\n"
              "  - callback URL mismatch (scheme, host, port must be exact)\n"
              "  - a stale process still bound to the callback port\n"
              "  - App Key / Secret copied with a trailing space",
              file=sys.stderr)
        return 1

    after = health(creds)
    print("\nAuth state after:")
    print(json.dumps(after, indent=2))

    if after["auth_state"] != AuthState.OK.value:
        print("\nWARNING: login completed but state is not OK. The token file "
              "may not have been written where SCHWAB_TOKEN_PATH points.",
              file=sys.stderr)
        return 1

    print(f"\nOK. Token valid for ~{after['days_until_expiry']} more days. "
          f"Client: {type(client).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
