"""Durable option-chain persistence: SQLite WAL, one rolling database.

Spec: docs/FEED_SPEC_V4.md §6 (storage discipline), extended to the options
layer. Companion to data/tick_store.py, but deliberately NOT the same shape —
see "Why this is not TickStore" below.

The point of this module
------------------------
`data/schwab_chains.py` fetches a chain. Every fetch is a snapshot that is
discarded when the process exits, so the system can see open-interest LEVELS
but never open-interest CHANGE. Change is the signal: a 9,783-contract call
wall that was 4,000 yesterday is accumulation, and the same wall that was
15,000 yesterday is unwinding. The level alone cannot distinguish them.

Why this is not TickStore
-------------------------
TickStore is an async queue-fed batching writer with drop accounting, because
it sits under a live tape that delivers thousands of prints per second and
gets disconnected if it applies backpressure. None of that is true here. A
chain fetch is a daily batch of a few thousand rows arriving in one call, off
any hot path. Copying the queue/worker/drop machinery would add failure modes
to guard against a load profile that cannot occur. This writer is synchronous
and commits in one transaction.

One rolling database, not one per date
--------------------------------------
TickStore partitions per session date because a full tape is gigabytes per
week and a day's data is self-contained. Chain snapshots are a few thousand
rows per underlying per day. The primary query — OI change across dates — is
a self-join across days, which per-date files would turn into a cross-file
join for the one operation this module exists to serve.

THE T+1 TRAP — read before using oi_change()
--------------------------------------------
Open interest is computed by OCC after the close and disseminated the
following morning. The OI in a chain fetched on session date D is the figure
as of the close of D-1, NOT of D.

Therefore:

    snapshot(D).open_interest - snapshot(D-1).open_interest

is the OI change between the close of D-2 and the close of D-1. It is a real,
usable signal. It is NOT "what happened yesterday" and it is definitely not
"what is happening today". `OIChange` carries `as_of_close` and `prior_close`
so the caller cannot avoid knowing which two closes were actually compared.

This is the same class of error as the 0DTE volume/OI artifact: a number that
is arithmetically correct and semantically mislabelled.

Session gaps
------------
The previous snapshot is the previous one that EXISTS, not the previous
calendar day. Weekends, holidays and missed fetches all create gaps.
`oi_change()` resolves the prior session by lookup and reports the actual
dates compared, so a Tuesday-vs-Friday comparison is visible as one rather
than silently reading as "yesterday".
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- One row per contract per session date. The UNIQUE constraint makes a
-- re-fetch on the same date replace rather than duplicate: two rows for one
-- contract on one date would double-count in every OI diff.
CREATE TABLE IF NOT EXISTS chain_snapshots (
  session_date   TEXT NOT NULL,      -- ET calendar date of the FETCH, YYYYMMDD
  underlying     TEXT NOT NULL,
  symbol         TEXT NOT NULL,      -- OCC symbol
  put_call       TEXT NOT NULL,
  strike         REAL NOT NULL,
  expiration     TEXT NOT NULL,      -- YYYY-MM-DD
  days_to_expiration INTEGER,

  bid REAL, ask REAL, last REAL, mark REAL,
  bid_size INTEGER, ask_size INTEGER,

  volume         INTEGER NOT NULL,   -- contracts traded on the fetch date
  open_interest  INTEGER NOT NULL,   -- T+1: as of the PRIOR close

  volatility REAL, delta REAL, gamma REAL, theta REAL, vega REAL, rho REAL,

  in_the_money   INTEGER,
  intrinsic_value REAL,
  time_value     REAL,
  multiplier     REAL NOT NULL,
  is_penny_pilot INTEGER,
  is_mini        INTEGER,
  is_non_standard INTEGER,
  option_root    TEXT,

  underlying_price REAL,
  is_delayed     INTEGER,
  fetched_at_ms  INTEGER NOT NULL,

  UNIQUE(session_date, symbol)
);
CREATE INDEX IF NOT EXISTS ix_chain_und_date
  ON chain_snapshots(underlying, session_date);
CREATE INDEX IF NOT EXISTS ix_chain_sym_date
  ON chain_snapshots(symbol, session_date);
CREATE INDEX IF NOT EXISTS ix_chain_date_und_exp
  ON chain_snapshots(session_date, underlying, expiration);

-- One row per fetch. Lets a reader distinguish "no walls that day" from
-- "the fetch never ran", which the snapshot table alone cannot answer.
CREATE TABLE IF NOT EXISTS chain_fetches (
  session_date  TEXT NOT NULL,
  underlying    TEXT NOT NULL,
  fetched_at_ms INTEGER NOT NULL,
  contract_count INTEGER NOT NULL,
  is_delayed    INTEGER,
  status        TEXT,
  underlying_price REAL,
  error         TEXT,
  UNIQUE(session_date, underlying)
);
"""

_INSERT = """
INSERT INTO chain_snapshots (
  session_date, underlying, symbol, put_call, strike, expiration,
  days_to_expiration, bid, ask, last, mark, bid_size, ask_size,
  volume, open_interest, volatility, delta, gamma, theta, vega, rho,
  in_the_money, intrinsic_value, time_value, multiplier,
  is_penny_pilot, is_mini, is_non_standard, option_root,
  underlying_price, is_delayed, fetched_at_ms
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(session_date, symbol) DO UPDATE SET
  volume=excluded.volume,
  open_interest=excluded.open_interest,
  bid=excluded.bid, ask=excluded.ask, last=excluded.last, mark=excluded.mark,
  bid_size=excluded.bid_size, ask_size=excluded.ask_size,
  volatility=excluded.volatility, delta=excluded.delta, gamma=excluded.gamma,
  theta=excluded.theta, vega=excluded.vega, rho=excluded.rho,
  in_the_money=excluded.in_the_money,
  intrinsic_value=excluded.intrinsic_value,
  time_value=excluded.time_value,
  underlying_price=excluded.underlying_price,
  is_delayed=excluded.is_delayed,
  fetched_at_ms=excluded.fetched_at_ms
"""

_FETCH_INSERT = """
INSERT INTO chain_fetches (
  session_date, underlying, fetched_at_ms, contract_count,
  is_delayed, status, underlying_price, error
) VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(session_date, underlying) DO UPDATE SET
  fetched_at_ms=excluded.fetched_at_ms,
  contract_count=excluded.contract_count,
  is_delayed=excluded.is_delayed,
  status=excluded.status,
  underlying_price=excluded.underlying_price,
  error=excluded.error
"""


def session_date_et(now: datetime | None = None) -> str:
    """YYYYMMDD for the current ET calendar date.

    ET-anchored for the same reason as TickStore: the trading session is an
    ET concept, and a UTC date would roll at 20:00 ET.
    """
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ET).strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class OIChange:
    """Open-interest change for one contract between two stored snapshots.

    `as_of_close` and `prior_close` are the two CLOSES actually compared, not
    the fetch dates. Because OI is T+1, comparing fetches on D and D-1 yields
    the change between the closes of D-1 and D-2. Carrying both here makes
    that impossible to misread downstream.
    """

    symbol: str
    underlying: str
    put_call: str
    strike: float
    expiration: str

    open_interest: int          # OI at as_of_close
    prior_open_interest: int     # OI at prior_close
    oi_change: int
    volume: int                  # volume on the later FETCH date

    session_date: str            # fetch date of the later snapshot
    prior_session_date: str      # fetch date of the earlier snapshot
    as_of_close: str             # close the later OI figure describes
    prior_close: str             # close the earlier OI figure describes
    multiplier: float

    @property
    def oi_change_pct(self) -> float | None:
        """Percent change. None when prior OI is zero — not zero percent.

        A contract going 0 -> 5,000 is not a percentage move, it is a new
        listing or a strike that had no open position. Returning 0.0 or
        infinity here would put it at one end of any ranking; None forces the
        caller to decide.
        """
        if not self.prior_open_interest:
            return None
        return (self.oi_change / self.prior_open_interest) * 100.0

    @property
    def oi_change_shares(self) -> float:
        """OI change in underlying shares, via the contract multiplier."""
        return self.oi_change * self.multiplier


class ChainStore:
    """Rolling SQLite store for option-chain snapshots.

    Usage
    -----
        store = ChainStore(r"C:\\trading\\LLM model\\data\\chains")
        store.open()
        store.write_chain(chain)          # an OptionChain from schwab_chains
        changes = store.oi_change("SNDK")
        store.close()

    Also usable as a context manager.
    """

    def __init__(self, db_dir: str, *, filename: str = "chains.db") -> None:
        self._db_dir = db_dir
        self._filename = filename
        self._conn: sqlite3.Connection | None = None

    # -------------------------------------------------------------- lifecycle

    @property
    def db_path(self) -> str:
        return os.path.join(self._db_dir, self._filename)

    def open(self) -> "ChainStore":
        os.makedirs(self._db_dir, exist_ok=True)
        path = self.db_path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(
                f"Expected WAL journal mode on {path}, got {mode!r}. WAL is "
                f"required so a reader can query while the pre-market fetch "
                f"writes."
            )
        logger.info("ChainStore open: %s (journal_mode=%s)", path, mode)
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "ChainStore":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ChainStore used before open()")
        return self._conn

    # ----------------------------------------------------------------- write

    def write_chain(self, chain, *, session_date: str | None = None) -> int:
        """Persist one OptionChain. Returns the number of contracts written.

        Idempotent per (session_date, symbol): re-running the fetch on the
        same date updates in place. Without that, a retry after a partial
        failure would double every OI diff that crossed it.
        """
        conn = self._require()
        sd = session_date or session_date_et(chain.fetched_at)
        fetched_ms = int(chain.fetched_at.timestamp() * 1000)
        delayed = None if chain.is_delayed is None else int(chain.is_delayed)

        rows = [
            (
                sd, chain.underlying, c.symbol, c.put_call, c.strike,
                c.expiration, c.days_to_expiration,
                c.bid, c.ask, c.last, c.mark, c.bid_size, c.ask_size,
                c.volume, c.open_interest,
                c.volatility, c.delta, c.gamma, c.theta, c.vega, c.rho,
                int(c.in_the_money), c.intrinsic_value, c.time_value,
                c.multiplier, int(c.is_penny_pilot), int(c.is_mini),
                int(c.is_non_standard), c.option_root,
                chain.underlying_price, delayed, fetched_ms,
            )
            for c in chain.contracts
        ]

        try:
            conn.executemany(_INSERT, rows)
            conn.execute(_FETCH_INSERT, (
                sd, chain.underlying, fetched_ms, len(rows), delayed,
                chain.status, chain.underlying_price, None,
            ))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            # Rule 18: a failed chain write is a hole in the OI history that
            # cannot be backfilled — the chain as of that date is gone once
            # the day passes. It must be loud.
            logger.exception(
                "ChainStore write FAILED for %s on %s — %d contracts lost. "
                "OI history for this date cannot be reconstructed later.",
                chain.underlying, sd, len(rows))
            raise
        logger.info("ChainStore wrote %d contracts for %s on %s",
                    len(rows), chain.underlying, sd)
        return len(rows)

    def record_failure(self, underlying: str, error: str, *,
                       session_date: str | None = None) -> None:
        """Record that a fetch was attempted and failed.

        Without this, a missing date is ambiguous between "the fetch failed"
        and "the daemon was not running", and the OI series has an unexplained
        gap that the next reader has to guess about.
        """
        conn = self._require()
        sd = session_date or session_date_et()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        conn.execute(_FETCH_INSERT,
                     (sd, underlying, now_ms, 0, None, "ERROR", None, error))
        conn.commit()

    # ------------------------------------------------------------------ read

    def sessions(self, underlying: str) -> list[str]:
        """Session dates with a successful (non-empty) fetch, oldest first."""
        conn = self._require()
        rows = conn.execute(
            "SELECT session_date FROM chain_fetches "
            "WHERE underlying = ? AND contract_count > 0 "
            "ORDER BY session_date", (underlying,)).fetchall()
        return [r["session_date"] for r in rows]

    def prior_session(self, underlying: str, session_date: str) -> str | None:
        """The most recent stored session strictly before `session_date`.

        The PREVIOUS STORED session, not the previous calendar day. Weekends,
        holidays and missed fetches are all gaps, and pretending otherwise
        would silently label a Friday-to-Tuesday move as one day.
        """
        conn = self._require()
        row = conn.execute(
            "SELECT session_date FROM chain_fetches "
            "WHERE underlying = ? AND contract_count > 0 AND session_date < ? "
            "ORDER BY session_date DESC LIMIT 1",
            (underlying, session_date)).fetchone()
        return row["session_date"] if row else None

    def oi_change(
        self,
        underlying: str,
        *,
        session_date: str | None = None,
        prior_session_date: str | None = None,
        min_open_interest: int = 0,
        min_abs_change: int = 0,
        min_days_to_expiration: int = 1,
    ) -> list[OIChange]:
        """Open-interest change per contract between two stored snapshots.

        Defaults mirror the guards already learned the hard way in
        scripts/fetch_option_chain.py:

        - `min_days_to_expiration=1` excludes 0DTE, where OI is near zero by
          construction and every ratio explodes.
        - `min_open_interest` filters illiquid strikes whose OI moves are
          noise.

        Contracts present on only one of the two dates are EXCLUDED. A new
        listing has no prior OI to difference against, and treating absent as
        zero would report the entire OI as new accumulation. Use
        `new_contracts()` to see those separately.

        Returns [] when there is no prior session — the first fetch has
        nothing to compare against, which is not an error.
        """
        conn = self._require()
        sd = session_date or self.latest_session(underlying)
        if sd is None:
            return []
        prior = prior_session_date or self.prior_session(underlying, sd)
        if prior is None:
            logger.info(
                "ChainStore.oi_change: no session before %s for %s — nothing "
                "to compare. This is expected on the first fetch.", sd,
                underlying)
            return []

        rows = conn.execute(
            """
            SELECT
              cur.symbol, cur.underlying, cur.put_call, cur.strike,
              cur.expiration, cur.days_to_expiration, cur.multiplier,
              cur.open_interest AS oi,
              prv.open_interest AS prior_oi,
              cur.volume AS volume
            FROM chain_snapshots cur
            JOIN chain_snapshots prv
              ON prv.symbol = cur.symbol AND prv.session_date = ?
            WHERE cur.session_date = ?
              AND cur.underlying = ?
              AND cur.open_interest >= ?
              AND cur.days_to_expiration >= ?
              AND ABS(cur.open_interest - prv.open_interest) >= ?
            ORDER BY ABS(cur.open_interest - prv.open_interest) DESC
            """,
            (prior, sd, underlying, min_open_interest,
             min_days_to_expiration, min_abs_change)).fetchall()

        as_of = _prior_trading_close(sd)
        prior_close = _prior_trading_close(prior)

        return [
            OIChange(
                symbol=r["symbol"], underlying=r["underlying"],
                put_call=r["put_call"], strike=r["strike"],
                expiration=r["expiration"],
                open_interest=r["oi"], prior_open_interest=r["prior_oi"],
                oi_change=r["oi"] - r["prior_oi"], volume=r["volume"],
                session_date=sd, prior_session_date=prior,
                as_of_close=as_of, prior_close=prior_close,
                multiplier=r["multiplier"],
            )
            for r in rows
        ]

    def new_contracts(self, underlying: str, *,
                      session_date: str | None = None) -> list[sqlite3.Row]:
        """Contracts present on `session_date` but absent on the prior session.

        Separated from oi_change() deliberately: these have no prior OI, so
        any "change" figure for them is really a level. Reporting them inside
        the same ranking would put brand-new strikes at the top of an
        accumulation table purely because they did not exist before.
        """
        conn = self._require()
        sd = session_date or self.latest_session(underlying)
        if sd is None:
            return []
        prior = self.prior_session(underlying, sd)
        if prior is None:
            return []
        return conn.execute(
            """
            SELECT cur.* FROM chain_snapshots cur
            WHERE cur.session_date = ? AND cur.underlying = ?
              AND NOT EXISTS (
                SELECT 1 FROM chain_snapshots prv
                WHERE prv.symbol = cur.symbol AND prv.session_date = ?
              )
            ORDER BY cur.open_interest DESC
            """, (sd, underlying, prior)).fetchall()

    def latest_session(self, underlying: str) -> str | None:
        conn = self._require()
        row = conn.execute(
            "SELECT session_date FROM chain_fetches "
            "WHERE underlying = ? AND contract_count > 0 "
            "ORDER BY session_date DESC LIMIT 1", (underlying,)).fetchone()
        return row["session_date"] if row else None

    def snapshot(self, underlying: str,
                 session_date: str | None = None) -> list[sqlite3.Row]:
        """All stored contracts for one underlying on one session date."""
        conn = self._require()
        sd = session_date or self.latest_session(underlying)
        if sd is None:
            return []
        return conn.execute(
            "SELECT * FROM chain_snapshots "
            "WHERE underlying = ? AND session_date = ? "
            "ORDER BY expiration, put_call, strike", (underlying, sd)
        ).fetchall()

    def underlyings(self) -> list[str]:
        conn = self._require()
        return [r["underlying"] for r in conn.execute(
            "SELECT DISTINCT underlying FROM chain_fetches "
            "ORDER BY underlying").fetchall()]

    def stats(self) -> dict:
        conn = self._require()
        row = conn.execute(
            "SELECT COUNT(*) AS rows, COUNT(DISTINCT session_date) AS dates, "
            "COUNT(DISTINCT underlying) AS unds FROM chain_snapshots"
        ).fetchone()
        fetches = conn.execute(
            "SELECT COUNT(*) AS n, SUM(error IS NOT NULL) AS errors "
            "FROM chain_fetches").fetchone()
        return {
            "db_path": self.db_path,
            "snapshot_rows": row["rows"],
            "session_dates": row["dates"],
            "underlyings": row["unds"],
            "fetches": fetches["n"],
            "failed_fetches": fetches["errors"] or 0,
        }


def _prior_trading_close(session_date: str) -> str:
    """The close that a chain fetched on `session_date` reports OI for.

    OI is disseminated the morning after it is computed, so a fetch on D
    carries the close of D-1. This returns a LABEL, deliberately derived from
    stored data rather than a calendar: the honest answer is "the close before
    this fetch date", and inventing a specific date would require a market
    holiday calendar this module has no business owning.
    """
    return f"close before {session_date[:4]}-{session_date[4:6]}-{session_date[6:]}"
