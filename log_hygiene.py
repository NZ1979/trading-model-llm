"""Central HTTP logger hygiene — Rule 22, in one place instead of N copies.

Why this module exists
----------------------
Rule 22 requires that any code path making outbound vendor HTTP calls has the
noisy HTTP loggers pinned to WARNING before it configures logging, because
``httpx`` logs the FULL request URL — query string included — at INFO.

Before this module the suppression list lived inline in
``scripts/fetch_option_chain.py`` and nowhere else. That is fine until a second
entry point calls ``logging.basicConfig`` and forgets it, which is exactly what
``mcp_server/server.py`` did: INFO level, no suppression, and a first-party
``import httpx`` reachable from it via ``data/alpaca_rest.py``.

The audit, 2026-08-17
---------------------
* ``data/alpaca_rest.py`` is the only first-party module that imports ``httpx``
  directly. ``schwab-py`` pulls it in transitively.
* Alpaca authenticates with the ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY``
  headers and Schwab with a ``Bearer`` header, so neither vendor puts a
  credential in a URL. The residual exposure is request metadata, not secrets —
  MEANINGFULLY LOWER than the 2026-05-04 Polygon ``?apiKey=`` leak that Rule 22
  was written for, and not zero.
* The one genuine URL-borne secret in this project is the Schwab OAuth redirect,
  whose query string carries a single-use authorization code. It is served by
  the ``flask``/``werkzeug`` callback server that ``schwab-py`` starts, which is
  why those two are on the list alongside the HTTP clients.
* ``scripts/market.py``, ``scripts/watch.py`` and ``scripts/daily.py`` never
  call ``basicConfig`` at all, so the root logger stays at WARNING and httpx
  never emits. Their gap is LATENT, not live: it opens the day someone adds a
  ``--verbose`` flag. Calling ``setup_logging`` from here is what keeps that
  day from being a regression.

A logger set to WARNING still emits warnings and errors, so nothing operational
is hidden by this. Only routine per-request INFO chatter is.
"""

from __future__ import annotations

import logging
from typing import IO, Any

#: Pinned to WARNING by :func:`quiet_http_loggers`.
#:
#: ``httpx`` / ``httpcore`` / ``httpx2``  — log full request URLs at INFO.
#: ``urllib3`` / ``aiohttp`` / ``anthropic`` — quieter by default, listed so a
#:     dependency swap cannot silently reintroduce the leak.
#: ``werkzeug`` / ``flask`` / ``authlib`` / ``schwab`` — the Schwab OAuth
#:     callback server; its redirect URL carries a single-use auth code.
NOISY_HTTP_LOGGERS: tuple[str, ...] = (
    "werkzeug",
    "flask",
    "authlib",
    "schwab",
    "httpx",
    "httpcore",
    "httpx2",
    "urllib3",
    "aiohttp",
    "anthropic",
)

DEFAULT_FORMAT = "%(levelname)-7s %(name)s: %(message)s"


def quiet_http_loggers(level: int = logging.WARNING) -> tuple[str, ...]:
    """Pin every logger in :data:`NOISY_HTTP_LOGGERS` to ``level``.

    Safe to call more than once and safe to call before or after
    ``basicConfig``; it sets levels on named loggers and touches no handlers.

    Returns the tuple of names it acted on so a caller can log or assert on it.
    """
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(level)
    return NOISY_HTTP_LOGGERS


def setup_logging(
    level: str | int = "INFO",
    *,
    stream: IO[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
    **kwargs: Any,
) -> None:
    """``logging.basicConfig`` with the Rule 22 suppression welded on.

    Use this instead of calling ``basicConfig`` directly. ``stream`` matters for
    stdio MCP servers, which must log to stderr only — stdout is the JSON-RPC
    channel and a single stray line corrupts the protocol.
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper())
    if stream is not None:
        kwargs["stream"] = stream
    logging.basicConfig(level=level, format=fmt, **kwargs)
    quiet_http_loggers()
