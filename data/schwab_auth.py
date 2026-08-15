"""Schwab Trader API authentication. Spec: docs/FEED_SPEC_V4.md §4.

The whole point of this module is the 7-day refresh-token expiry. Schwab
rejects any request whose refresh token is older than seven days with
`invalid_client`, and there is no programmatic renewal path — re-auth is an
interactive browser flow. This is the single most likely thing to break on a
Monday morning, so the module's job is to make the expiry visible days before
it bites rather than at the moment it does.

Credential handling
-------------------
Credentials live in %LOCALAPPDATA%\\trading\\schwab.env, deliberately OUTSIDE
the repo. A secret inside the working tree ends up in a Claude Code context
window, and from there in a log or a commit. The directory ACL is restricted
to the workstation user with inheritance stripped.

Nothing in this module logs, returns, or formats a credential value. Token
age and auth state are exposed; the token itself is not. See Rule 21.

Note on logging: schwab-py pulls in flask+werkzeug for the OAuth callback
server, and Schwab returns the authorization code as a URL query parameter.
Werkzeug logs full request lines at INFO. main.py's setup_logging suppresses
werkzeug/flask/authlib/schwab for that reason (Rule 22 audit, 2026-08-14).
schwab-py's own `register_redactions` covers its debug JSON dumps but NOT the
werkzeug request line, so both mitigations are needed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "trading" / "schwab.env"

# Schwab hard-expires refresh tokens at 7 days. schwab-py's easy_client
# defaults max_token_age to 561600s (6.5 days), which leaves only twelve hours
# of margin — a Friday-evening warning you do not see until Monday. Six days
# puts the warning on a weekday.
SEVEN_DAYS_S = 7 * 24 * 3600
MAX_TOKEN_AGE_S = 6 * 24 * 3600
WARN_TOKEN_AGE_S = 5 * 24 * 3600


class AuthState(str, Enum):
    OK = "OK"
    WARN_EXPIRING = "WARN_EXPIRING"     # >5 days: re-auth on your schedule
    AUTH_EXPIRED = "AUTH_EXPIRED"       # >7 days: every call will fail
    NO_TOKEN = "NO_TOKEN"               # never authenticated on this machine
    NO_CREDENTIALS = "NO_CREDENTIALS"   # env file missing or incomplete


@dataclass(frozen=True)
class SchwabCredentials:
    api_key: str
    app_secret: str
    callback_url: str
    token_path: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        """Never let a credential reach a log, traceback, or REPL echo."""
        return (f"SchwabCredentials(api_key=<redacted {len(self.api_key)} chars>, "
                f"app_secret=<redacted>, callback_url={self.callback_url!r}, "
                f"token_path={self.token_path!r})")

    def missing_fields(self) -> list[str]:
        """Names of blank required fields. Names only, never values."""
        return [name for name, value in (
            ("SCHWAB_API_KEY", self.api_key),
            ("SCHWAB_APP_SECRET", self.app_secret),
            ("SCHWAB_CALLBACK_URL", self.callback_url),
            ("SCHWAB_TOKEN_PATH", self.token_path),
        ) if not value]

    def is_complete(self) -> bool:
        return not self.missing_fields()


def parse_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Minimal KEY=VALUE parser. No python-dotenv dependency.

    Tolerates blank lines, `#` comments, and a UTF-8 BOM (Notepad on Windows
    writes one by default, and a BOM silently corrupts the first key name).
    Values are taken verbatim after the first `=`, with surrounding whitespace
    stripped — a trailing space in a secret produces an auth failure that
    looks like a bad credential.
    """
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def load_credentials(
    env_path: str | os.PathLike | None = None,
) -> SchwabCredentials | None:
    """Load from the env file, falling back to process environment.

    Returns None when anything required is missing — callers surface
    NO_CREDENTIALS rather than crashing, so get_health can report it.
    """
    values: dict[str, str] = {}
    path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    if path.is_file():
        try:
            values = parse_env_file(path)
        except OSError:
            logger.exception("Cannot read Schwab env file at %s", path)
            return None
    else:
        logger.warning("Schwab env file not found at %s; falling back to "
                       "process environment", path)

    def get(name: str) -> str:
        return values.get(name) or os.environ.get(name, "")

    api_key = get("SCHWAB_API_KEY")
    app_secret = get("SCHWAB_APP_SECRET")
    callback_url = get("SCHWAB_CALLBACK_URL")
    token_path = get("SCHWAB_TOKEN_PATH")

    missing = [n for n, v in (
        ("SCHWAB_API_KEY", api_key), ("SCHWAB_APP_SECRET", app_secret),
        ("SCHWAB_CALLBACK_URL", callback_url),
        ("SCHWAB_TOKEN_PATH", token_path),
    ) if not v]
    if missing:
        # Names only. Never the values (Rule 21).
        logger.error("Schwab credentials incomplete, missing: %s", missing)
        return None

    return SchwabCredentials(api_key, app_secret, callback_url, token_path)


def token_age_seconds(token_path: str | os.PathLike) -> float | None:
    """Age of the token file in seconds, or None if it does not exist.

    Uses mtime rather than parsing the token: schwab-py owns that file's
    format, and mtime advances on every refresh write, which is exactly the
    quantity the 7-day rule is measured against.
    """
    p = Path(token_path)
    if not p.is_file():
        return None
    return max(0.0, time.time() - p.stat().st_mtime)


def auth_state(creds: SchwabCredentials | None) -> tuple[AuthState, float | None]:
    """Current auth state and token age in days. Never raises."""
    if creds is None:
        return AuthState.NO_CREDENTIALS, None

    age_s = token_age_seconds(creds.token_path)
    if age_s is None:
        return AuthState.NO_TOKEN, None

    age_days = age_s / 86400.0
    if age_s >= SEVEN_DAYS_S:
        return AuthState.AUTH_EXPIRED, age_days
    if age_s >= WARN_TOKEN_AGE_S:
        return AuthState.WARN_EXPIRING, age_days
    return AuthState.OK, age_days


def health(creds: SchwabCredentials | None = None) -> dict:
    """Auth block for get_health. Contains no secrets."""
    creds = creds if creds is not None else load_credentials()
    state, age_days = auth_state(creds)
    out = {
        "auth_state": state.value,
        "token_age_days": round(age_days, 2) if age_days is not None else None,
        "days_until_expiry": (round(7.0 - age_days, 2)
                              if age_days is not None else None),
        "token_path_configured": bool(creds and creds.token_path),
    }
    if state is AuthState.AUTH_EXPIRED:
        out["action_required"] = (
            "Refresh token older than 7 days. Every Schwab call will fail with "
            "invalid_client until you re-authenticate: run "
            "`python -m scripts.schwab_login` and complete the browser flow."
        )
    elif state is AuthState.NO_TOKEN:
        out["action_required"] = (
            "No token file. Run `python -m scripts.schwab_login` once to "
            "complete the initial OAuth flow."
        )
    elif state is AuthState.WARN_EXPIRING:
        out["action_required"] = (
            "Refresh token expires within 2 days. Re-authenticate on your "
            "schedule rather than mid-session."
        )
    return out


def get_client(
    creds: SchwabCredentials | None = None,
    *,
    asyncio: bool = False,
    allow_interactive: bool = False,
    manual: bool = False,
):
    """Return an authenticated schwab-py client.

    By default this NEVER opens a browser: a daemon that silently blocks on an
    interactive login is worse than one that fails with a clear message
    (Rule 18). Pass allow_interactive=True only from scripts/schwab_login.py,
    where a human is present.

    Raises RuntimeError with an actionable message on any auth problem.
    """
    creds = creds if creds is not None else load_credentials()
    if creds is None or not creds.is_complete():
        # An all-empty SchwabCredentials is not None but is equally unusable.
        # Without this check it falls through to the token branch and reports
        # "No Schwab token at ." — which sends you looking for a token file
        # when the real problem is blank credentials.
        missing = creds.missing_fields() if creds else [
            "SCHWAB_API_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL",
            "SCHWAB_TOKEN_PATH",
        ]
        raise RuntimeError(
            f"Schwab credentials incomplete or not found (missing: {missing}). "
            f"Expected {DEFAULT_ENV_PATH} with SCHWAB_API_KEY, "
            f"SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL and SCHWAB_TOKEN_PATH."
        )

    state, age_days = auth_state(creds)
    if state is AuthState.AUTH_EXPIRED and not allow_interactive:
        raise RuntimeError(
            f"Schwab refresh token is {age_days:.1f} days old; Schwab rejects "
            f"anything over 7. Re-authenticate with "
            f"`python -m scripts.schwab_login`."
        )
    if state is AuthState.NO_TOKEN and not allow_interactive:
        raise RuntimeError(
            f"No Schwab token at {creds.token_path}. Run "
            f"`python -m scripts.schwab_login` once to complete the initial "
            f"OAuth flow."
        )
    if state is AuthState.WARN_EXPIRING:
        logger.warning(
            "Schwab refresh token is %.1f days old — expires at 7. "
            "Re-authenticate on your schedule, not mid-session.", age_days)

    # Imported lazily so importing this module (and reading auth state) does
    # not require schwab-py to be installed.
    from schwab.auth import (
        client_from_manual_flow, client_from_token_file, easy_client,
    )

    if allow_interactive and manual:
        # Manual flow: schwab-py prints the auth URL, you log in, and paste
        # the redirected URL back on stdin. No local callback server.
        #
        # Why this exists (2026-08-14, Godzilla, Python 3.14): the automatic
        # flow spins up a Flask HTTPS server with ssl_context='adhoc', and
        # pip's vendored `truststore` has globally patched
        # ssl.SSLContext.wrap_socket. truststore does CLIENT-side chain
        # verification, so wrapping a SERVER-side socket blows up with
        #   AttributeError: 'NoneType' object has no attribute
        #   'get_unverified_chain'
        # and schwab-py reports the downstream symptom
        # (RedirectServerExitedError) rather than the cause. The manual flow
        # touches none of that machinery.
        return client_from_manual_flow(
            api_key=creds.api_key,
            app_secret=creds.app_secret,
            callback_url=creds.callback_url,
            token_path=creds.token_path,
            asyncio=asyncio,
        )

    if allow_interactive:
        return easy_client(
            api_key=creds.api_key,
            app_secret=creds.app_secret,
            callback_url=creds.callback_url,
            token_path=creds.token_path,
            asyncio=asyncio,
            max_token_age=MAX_TOKEN_AGE_S,
            interactive=True,
        )
    return client_from_token_file(
        token_path=creds.token_path,
        api_key=creds.api_key,
        app_secret=creds.app_secret,
        asyncio=asyncio,
    )
