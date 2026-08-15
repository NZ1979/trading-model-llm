"""Tests for Schwab auth state handling (spec v4 §4).

The 7-day refresh-token expiry is the single most likely thing to break this
integration on a Monday. These tests pin the state machine that makes the
expiry visible in advance, and assert that no code path can leak a credential
into a log, a repr, or an exception message (Rule 21).

No network. No schwab-py import — the module defers that import so auth state
can be read on a machine where the SDK is absent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from data.schwab_auth import (
    AuthState,
    SchwabCredentials,
    auth_state,
    get_client,
    health,
    load_credentials,
    parse_env_file,
    token_age_seconds,
)

SECRET = "sec456-DO-NOT-LEAK"
KEY = "abc123-DO-NOT-LEAK"


@pytest.fixture()
def env_file(tmp_path) -> Path:
    """Written with a BOM on purpose — Notepad on Windows does this by
    default, and a BOM silently corrupts the first key name."""
    p = tmp_path / "schwab.env"
    p.write_text(
        "# a comment\n"
        "\n"
        f"SCHWAB_API_KEY={KEY}  \n"          # trailing whitespace on purpose
        f"SCHWAB_APP_SECRET={SECRET}\n"
        "SCHWAB_CALLBACK_URL=https://127.0.0.1:8182\n"
        f"SCHWAB_TOKEN_PATH={tmp_path / 'tok.json'}\n",
        encoding="utf-8-sig",
    )
    return p


# ------------------------------------------------------------------ parsing

def test_bom_does_not_corrupt_first_key(env_file):
    assert "SCHWAB_API_KEY" in parse_env_file(env_file)


def test_trailing_whitespace_is_stripped(env_file):
    """A trailing space becomes part of the secret and produces an auth
    failure that looks like a bad credential."""
    assert parse_env_file(env_file)["SCHWAB_API_KEY"] == KEY


def test_comments_and_blank_lines_ignored(env_file):
    assert len(parse_env_file(env_file)) == 4


def test_incomplete_credentials_return_none(tmp_path):
    p = tmp_path / "bad.env"
    p.write_text("SCHWAB_API_KEY=x\n", encoding="utf-8")
    assert load_credentials(p) is None


# ------------------------------------------------------------ no leakage

def test_repr_redacts_key_and_secret(env_file):
    creds = load_credentials(env_file)
    text = repr(creds)
    assert SECRET not in text
    assert KEY not in text
    assert "redacted" in text


def test_missing_key_log_names_keys_not_values(tmp_path, caplog):
    p = tmp_path / "partial.env"
    p.write_text(f"SCHWAB_API_KEY={KEY}\n", encoding="utf-8")
    with caplog.at_level("ERROR"):
        load_credentials(p)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "SCHWAB_APP_SECRET" in text
    assert KEY not in text


# --------------------------------------------------------- the state machine

def test_no_token_file(env_file):
    creds = load_credentials(env_file)
    state, age = auth_state(creds)
    assert state is AuthState.NO_TOKEN
    assert age is None


def test_fresh_token_is_ok(env_file):
    creds = load_credentials(env_file)
    Path(creds.token_path).write_text("{}")
    state, age = auth_state(creds)
    assert state is AuthState.OK
    assert age < 0.1


@pytest.mark.parametrize("age_days,expected", [
    (0.5, AuthState.OK),
    (4.9, AuthState.OK),
    (5.1, AuthState.WARN_EXPIRING),
    (6.9, AuthState.WARN_EXPIRING),
    (7.1, AuthState.AUTH_EXPIRED),
    (30.0, AuthState.AUTH_EXPIRED),
])
def test_state_transitions_by_token_age(env_file, age_days, expected):
    creds = load_credentials(env_file)
    tok = Path(creds.token_path)
    tok.write_text("{}")
    stamp = time.time() - age_days * 86400
    os.utime(tok, (stamp, stamp))
    assert auth_state(creds)[0] is expected


def test_missing_credentials_state(env_file):
    assert auth_state(None)[0] is AuthState.NO_CREDENTIALS


def test_token_age_of_missing_file_is_none(tmp_path):
    assert token_age_seconds(tmp_path / "nope.json") is None


# ------------------------------------------------------------------- health

def test_health_never_raises_without_credentials():
    assert isinstance(health(None), dict)


def test_health_reports_days_until_expiry(env_file):
    creds = load_credentials(env_file)
    Path(creds.token_path).write_text("{}")
    h = health(creds)
    assert 6.9 < h["days_until_expiry"] <= 7.0
    assert h["auth_state"] == AuthState.OK.value


def test_expired_health_explains_the_failure_mode(env_file):
    creds = load_credentials(env_file)
    tok = Path(creds.token_path)
    tok.write_text("{}")
    stamp = time.time() - 8 * 86400
    os.utime(tok, (stamp, stamp))
    h = health(creds)
    assert h["auth_state"] == AuthState.AUTH_EXPIRED.value
    assert "invalid_client" in h["action_required"]
    assert "schwab_login" in h["action_required"]


def test_health_contains_no_credential_values(env_file):
    creds = load_credentials(env_file)
    Path(creds.token_path).write_text("{}")
    text = str(health(creds))
    assert SECRET not in text and KEY not in text


# ---------------------------------------------------------------- get_client

def test_get_client_refuses_expired_token_without_interactive(env_file):
    """A daemon must fail with an actionable message, never silently block on
    an interactive browser flow (Rule 18)."""
    creds = load_credentials(env_file)
    tok = Path(creds.token_path)
    tok.write_text("{}")
    stamp = time.time() - 8 * 86400
    os.utime(tok, (stamp, stamp))

    with pytest.raises(RuntimeError) as exc:
        get_client(creds)
    msg = str(exc.value)
    assert "schwab_login" in msg
    assert SECRET not in msg and KEY not in msg


def test_get_client_refuses_when_no_token(env_file):
    creds = load_credentials(env_file)
    with pytest.raises(RuntimeError) as exc:
        get_client(creds)
    assert "schwab_login" in str(exc.value)


def test_get_client_rejects_blank_credentials_with_the_right_message():
    """An all-blank SchwabCredentials is not None but is equally unusable.
    The error must name the missing keys, not send the user hunting for a
    token file."""
    with pytest.raises(RuntimeError) as exc:
        get_client(SchwabCredentials("", "", "", ""))
    msg = str(exc.value)
    assert "SCHWAB_API_KEY" in msg
    assert "incomplete" in msg
    assert "No Schwab token" not in msg


def test_missing_fields_lists_only_blank_keys():
    creds = SchwabCredentials("k", "", "https://127.0.0.1:8182", "")
    assert creds.missing_fields() == ["SCHWAB_APP_SECRET", "SCHWAB_TOKEN_PATH"]
    assert creds.is_complete() is False
    assert SchwabCredentials("k", "s", "c", "t").is_complete() is True
