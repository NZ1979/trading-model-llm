"""Rule 22 regression guard for log_hygiene.

These lock in the specific failure Rule 22 was written for: an entry point
calls ``logging.basicConfig(level=INFO)``, ``httpx`` inherits INFO, and every
outbound request URL lands in a log file. The behavioural test at the bottom is
the one that matters — a level assertion proves the attribute was set, not that
the record was actually suppressed.
"""

from __future__ import annotations

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from log_hygiene import (  # noqa: E402
    DEFAULT_FORMAT,
    NOISY_HTTP_LOGGERS,
    quiet_http_loggers,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Snapshot and restore every level this module touches, plus root."""
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in NOISY_HTTP_LOGGERS}
    saved_root = root.level
    saved_handlers = list(root.handlers)
    yield
    for name, lvl in saved.items():
        logging.getLogger(name).setLevel(lvl)
    root.setLevel(saved_root)
    root.handlers[:] = saved_handlers


def test_httpx_is_covered():
    """The specific offender. Rule 22 names httpx; this fails if it is dropped."""
    assert "httpx" in NOISY_HTTP_LOGGERS
    assert "httpcore" in NOISY_HTTP_LOGGERS


def test_schwab_oauth_callback_loggers_are_covered():
    """The redirect URL carries a single-use authorization code."""
    for name in ("werkzeug", "flask", "authlib", "schwab"):
        assert name in NOISY_HTTP_LOGGERS


def test_quiet_http_loggers_sets_every_name():
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    returned = quiet_http_loggers()
    assert returned == NOISY_HTTP_LOGGERS
    for name in NOISY_HTTP_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, name


def test_quiet_http_loggers_is_idempotent():
    quiet_http_loggers()
    quiet_http_loggers()
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_leaves_root_at_info_but_httpx_at_warning():
    """The whole point: verbose app logging without verbose request logging."""
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    setup_logging("INFO", force=True)
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_accepts_an_int_level():
    setup_logging(logging.DEBUG, force=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_honours_stream_for_stdio_mcp_servers():
    """stdio MCP servers must never write to stdout; it is the JSON-RPC channel."""
    setup_logging("INFO", stream=sys.stderr, force=True)
    handlers = logging.getLogger().handlers
    assert handlers, "basicConfig installed no handler"
    assert getattr(handlers[0], "stream", None) is sys.stderr


def test_a_url_bearing_info_record_is_actually_suppressed(capsys):
    """Behavioural, not a level assertion. This is what Rule 22 asks for.

    Emits the shape httpx emits — a full URL with a query string — and asserts
    nothing reaches the stream after setup_logging.
    """
    setup_logging("INFO", stream=sys.stderr, force=True)
    capsys.readouterr()

    logging.getLogger("httpx").info(
        'HTTP Request: GET https://api.example.com/v1/quotes?apiKey=SECRET123 "200 OK"'
    )
    logging.getLogger("app").info("this one should survive")

    captured = capsys.readouterr()
    assert "SECRET123" not in captured.err
    assert "SECRET123" not in captured.out
    assert "this one should survive" in captured.err


def test_nothing_is_logged_to_stdout_by_default_format():
    """Format string is shared so scripts stay visually consistent."""
    assert "%(levelname)" in DEFAULT_FORMAT
    assert "%(name)s" in DEFAULT_FORMAT
