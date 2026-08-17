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

import json
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
    # Added 2026-08-16 after a two-day silent outage. The token file existed,
    # was 1.9 days old, and this module reported OK with 5.11 days remaining
    # for 46 hours after every Schwab call had started failing. The file
    # contained no refresh_token at all, so there was never anything to renew.
    TOKEN_INCOMPLETE = "TOKEN_INCOMPLETE"   # file exists, no refresh_token
    TOKEN_UNREADABLE = "TOKEN_UNREADABLE"   # file exists, cannot be parsed


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


@dataclass(frozen=True)
class TokenFile:
    """Facts about the token file. Never holds a token value.

    `created_at` comes from schwab-py's own `creation_timestamp` field, which
    is written once at authentication and NOT updated on subsequent access-
    token refreshes. That is the quantity the 7-day refresh rule is actually
    measured against.
    """

    exists: bool
    readable: bool
    has_refresh_token: bool
    created_at: float | None
    access_expires_at: float | None
    age_source: str  # "creation_timestamp" | "file_mtime" | "none"

    @property
    def access_token_expired(self) -> bool | None:
        if self.access_expires_at is None:
            return None
        return time.time() >= self.access_expires_at

    @property
    def age_seconds(self) -> float | None:
        if self.created_at is None:
            return None
        return max(0.0, time.time() - self.created_at)


def read_token_file(token_path: str | os.PathLike) -> TokenFile:
    """Inspect the token file's structure. Returns facts, never values.

    Reads `creation_timestamp` rather than the file's mtime. The previous
    implementation used mtime with the stated rationale that "mtime advances
    on every refresh write, which is exactly the quantity the 7-day rule is
    measured against." That is backwards: mtime tracks the last ACCESS-token
    write, so a token in active use looks perpetually young while its refresh
    token's 7-day clock runs out underneath.

    Falls back to mtime only when `creation_timestamp` is absent, and records
    which was used in `age_source` so a caller can tell a measured age from a
    guessed one.
    """
    p = Path(token_path) if token_path else None
    if p is None or not p.is_file():
        return TokenFile(False, False, False, None, None, "none")

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # Rule 18: do not silently treat an unparseable token as absent. The
        # two need different fixes — one is "log in", the other is "something
        # corrupted this file."
        logger.error("Schwab token file at %s is unreadable: %s", p, exc)
        return TokenFile(True, False, False, None, None, "none")

    if not isinstance(raw, dict):
        return TokenFile(True, False, False, None, None, "none")

    token = raw.get("token")
    token = token if isinstance(token, dict) else {}
    created = raw.get("creation_timestamp")
    source = "creation_timestamp"
    if not isinstance(created, (int, float)):
        created = p.stat().st_mtime
        source = "file_mtime"

    expires_at = token.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_at = None

    return TokenFile(
        exists=True,
        readable=True,
        has_refresh_token=bool(token.get("refresh_token")),
        created_at=float(created),
        access_expires_at=float(expires_at) if expires_at is not None else None,
        age_source=source,
    )


def token_age_seconds(token_path: str | os.PathLike) -> float | None:
    """Age of the REFRESH token in seconds, or None if unavailable.

    Kept for callers that predate `read_token_file`. Now measured from
    `creation_timestamp`, not the file's mtime — see `read_token_file`.
    """
    return read_token_file(token_path).age_seconds


def auth_state(creds: SchwabCredentials | None) -> tuple[AuthState, float | None]:
    """Current auth state and refresh-token age in days. Never raises.

    This inspects a FILE. It does not prove Schwab will accept the token —
    use `verify_live()` for that. The distinction is not academic: on
    2026-08-14 this function reported OK for 46 hours after every API call
    had begun failing.
    """
    if creds is None:
        return AuthState.NO_CREDENTIALS, None

    tf = read_token_file(creds.token_path)
    if not tf.exists:
        return AuthState.NO_TOKEN, None
    if not tf.readable:
        return AuthState.TOKEN_UNREADABLE, None

    age_days = (tf.age_seconds / 86400.0) if tf.age_seconds is not None else None

    # Checked BEFORE age. A token with no refresh_token is unusable at any
    # age, and reporting "OK, 5.11 days remaining" for one is the exact
    # failure this rewrite exists to prevent.
    if not tf.has_refresh_token:
        return AuthState.TOKEN_INCOMPLETE, age_days

    if age_days is None:
        return AuthState.TOKEN_UNREADABLE, None
    age_s = age_days * 86400.0
    if age_s >= SEVEN_DAYS_S:
        return AuthState.AUTH_EXPIRED, age_days
    if age_s >= WARN_TOKEN_AGE_S:
        return AuthState.WARN_EXPIRING, age_days
    return AuthState.OK, age_days


def verify_live(creds: SchwabCredentials | None = None) -> dict:
    """Make one real Schwab call and report whether it worked.

    The ONLY function here that proves anything about access. Everything else
    inspects a file, and a file that looks correct is not working access —
    that conflation cost two days of silent outage.

    Costs one network round trip, so it is opt-in rather than folded into
    `health()`. Never raises; failures are returned.
    """
    creds = creds if creds is not None else load_credentials()
    if creds is None or not creds.is_complete():
        return {"live_ok": False, "detail": "no credentials"}
    try:
        client = get_client(creds)
        resp = client.get_quote("SPY")
        code = getattr(resp, "status_code", None)
        if code == 200:
            return {"live_ok": True, "detail": "quote request returned 200"}
        return {"live_ok": False,
                "detail": f"quote request returned {code}"}
    except Exception as exc:  # noqa: BLE001 - report anything, never raise
        return {"live_ok": False,
                "detail": f"{type(exc).__name__}: {exc}"[:300]}


def health(creds: SchwabCredentials | None = None,
           *, live: bool = False) -> dict:
    """Auth block for get_health. Contains no secrets.

    By default this reports FILE STATE ONLY — `checked_live` says so
    explicitly, so a reader cannot mistake it for proof of access. Pass
    `live=True` to spend one network round trip on the real answer.
    """
    creds = creds if creds is not None else load_credentials()
    state, age_days = auth_state(creds)
    tf = read_token_file(creds.token_path) if creds else TokenFile(
        False, False, False, None, None, "none")
    out = {
        "auth_state": state.value,
        "token_age_days": round(age_days, 2) if age_days is not None else None,
        "days_until_expiry": (round(7.0 - age_days, 2)
                              if age_days is not None else None),
        "token_path_configured": bool(creds and creds.token_path),
        # The three fields whose absence hid a two-day outage.
        "has_refresh_token": tf.has_refresh_token,
        "token_age_source": tf.age_source,
        "access_token_expired": tf.access_token_expired,
        "checked_live": False,
    }
    if live:
        out.update(verify_live(creds))
        out["checked_live"] = True
    if state is AuthState.TOKEN_INCOMPLETE:
        out["action_required"] = (
            "Token file exists but contains NO refresh_token, so it cannot be "
            "renewed and Schwab access is already dead or will die within the "
            "hour. This is what a half-completed OAuth flow leaves behind. "
            "Move the file aside and re-run `python -m scripts.schwab_login` — "
            "schwab-py will not repair it in place."
        )
    elif state is AuthState.TOKEN_UNREADABLE:
        out["action_required"] = (
            "Token file exists but could not be parsed. Move it aside and "
            "re-run `python -m scripts.schwab_login`."
        )
    elif state is AuthState.AUTH_EXPIRED:
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
