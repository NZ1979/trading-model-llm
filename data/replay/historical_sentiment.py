"""Point-in-time historical sentiment loader for the replay harness.

CRITICAL — Rule 26 partition:

The original M2 design doc specified that this loader queries
``trader-prod``'s ``sentiment`` table on the VPS read-only. Rule 26
(added 2026-05-13 to ``CLAUDE_PREFLIGHT.md``) forbids any LLM-model
session from touching ``/opt/trader/app/trading.db``, including for
historical reads. The design predates the rule.

Resolution path chosen 2026-05-14 (Option A from the design-decision
discussion): a one-time curated SQLite fixture exported from
trader-prod by a SEPARATE gap-and-go-anchored session, transferred to
Godzilla via a non-SSH path, and read by this loader at runtime. The
export procedure is documented at ``data/replay/fixtures/README.md``.
The fixture path is configurable via ``ReplayConfig.sentiment_fixture_path``.

This module is INSIDE Rule 26 ("If realistic data is needed for
LLM-fork dev, use synthesized fixtures or a deliberately-curated local
sample DB"), not an exception to it.

Status: M2.1 scaffolding stub. Implementation lands in M2.2.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HistoricalSentimentRow:
    """One row from the sentiment fixture.

    Shape mirrors the production ``sentiment`` table on trader-prod.
    Values are pre-scored by the live Haiku pipeline at the time the
    headline was first ingested; we do NOT re-score historical
    headlines in replay (per design doc § Sentiment-data caveats —
    re-scoring would couple replay results to Claude model drift).
    """

    ts_et: datetime  # tz-aware America/New_York; the time the score was written
    ticker: str
    sentiment_score: float  # -1.0 (most negative) to 1.0 (most positive)
    polygon_article_id: str | None
    haiku_model_id: str | None  # the model that produced this score, for audit


def open_fixture(path: Path) -> sqlite3.Connection:
    """Open the curated sentiment fixture read-only.

    Connects in ``mode=ro`` URI mode so a buggy loader can't corrupt
    the fixture by accident. Fails loud (Rule 18) if the file is
    missing or unreadable — no silent fallback to empty data.

    Raises:
        FileNotFoundError: fixture path does not exist. Re-export from
            trader-prod per ``data/replay/fixtures/README.md``.
        sqlite3.DatabaseError: file exists but isn't a valid SQLite DB.
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "open_fixture is M2.2 work; M2.1 declares the contract. "
        "Implementation MUST use sqlite3 URI mode 'file:<path>?mode=ro'."
    )


def latest_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    as_of_et: datetime,
    max_age_seconds: int = 86400,
) -> HistoricalSentimentRow | None:
    """Return the most recent sentiment row for ``ticker`` at ``as_of_et``.

    Mirrors ``analysis/sentiment.py::latest_sentiment`` from the live
    code path — same semantics, different storage. The replay's
    ``context_builder`` calls this where production calls the live
    function.

    Returns None when no row exists within ``max_age_seconds`` of
    ``as_of_et``. Callers treat ``None`` as "no sentiment signal"
    consistent with the live system's behavior on cold tickers.

    Args:
        conn: connection from ``open_fixture``.
        ticker: equity symbol.
        as_of_et: replay tick timestamp; rows with ``ts_et > as_of_et``
            are excluded (point-in-time correctness).
        max_age_seconds: rows older than this are excluded. Default 24h
            matches the live ``latest_sentiment`` default.

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "latest_sentiment is M2.2 work; M2.1 declares the contract"
    )


def coverage_window(conn: sqlite3.Connection) -> tuple[datetime, datetime]:
    """Return (min_ts_et, max_ts_et) of the fixture's coverage.

    Used at replay start to verify the requested ``[start_date,
    end_date]`` window is fully covered by the fixture. If the replay
    range extends past ``max_ts_et``, the replay aborts loudly with a
    "re-export needed" message — never silently degrades to
    empty-sentiment for the uncovered tail (Rule 18).

    Raises:
        NotImplementedError: M2.1 scaffolding. Implementation in M2.2.
    """
    raise NotImplementedError(
        "coverage_window is M2.2 work; M2.1 declares the contract"
    )
