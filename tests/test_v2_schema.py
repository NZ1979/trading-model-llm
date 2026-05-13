"""Tests for v2 schema migrations (Q2 + Q4 in LLM_MODEL_V2_REFINEMENTS.md).

Verifies that `init_v2_schema(conn)`:
  - adds the six Q2/Q4 columns to `decisions` with correct types/defaults
  - extends `shadow_outcomes` with three day_N_eod_pct columns
  - creates the `position_trace` event ledger with the spec'd columns
  - indexes `position_trace.decision_id`
  - is idempotent: re-running on a fully-migrated DB is a no-op
"""
import sqlite3
import sys

sys.path.insert(0, '.')

from main import init_v2_schema, _column_exists, _add_column_if_missing


# ---------------------------------------------------------------------------
# Helpers: build the v1 tables the v2 migration requires
# ---------------------------------------------------------------------------

def _create_v1_decisions(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            setup TEXT,
            sentiment INTEGER,
            confidence INTEGER,
            walls_status TEXT,
            reasons TEXT
        )
    """)


def _create_v1_shadow_outcomes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE shadow_outcomes ("
        "decision_id INTEGER PRIMARY KEY, "
        "return_5m_pct REAL, return_15m_pct REAL, return_30m_pct REAL, "
        "return_60m_pct REAL, return_eod_pct REAL, "
        "mae_pct REAL, mfe_pct REAL, "
        "mae_at_minutes INTEGER, mfe_at_minutes INTEGER, "
        "stop_would_hit INTEGER, stop_hit_at_minutes INTEGER, "
        "target_would_hit INTEGER, target_hit_at_minutes INTEGER, "
        "first_touch TEXT, "
        "avg_spread_bps REAL, estimated_slippage_bps REAL, "
        "populated_at REAL NOT NULL, horizon_complete TEXT NOT NULL, "
        "FOREIGN KEY(decision_id) REFERENCES decisions(id))"
    )


def _v1_db() -> sqlite3.Connection:
    """Return an in-memory connection with the v1 tables already created."""
    conn = sqlite3.connect(":memory:")
    _create_v1_decisions(conn)
    _create_v1_shadow_outcomes(conn)
    return conn


def _pragma_cols(conn: sqlite3.Connection, table: str) -> dict:
    """Return {column_name: (type, notnull, dflt_value, pk)} for the table."""
    out = {}
    for cid, name, col_type, notnull, dflt, pk in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        out[name] = (col_type, notnull, dflt, pk)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_v2_schema_adds_decisions_columns():
    conn = _v1_db()
    init_v2_schema(conn)
    cols = _pragma_cols(conn, "decisions")
    expected = {
        "holding_day":     ("INTEGER", 1, "0",         0),
        "policy_version":  ("TEXT",    1, "'0.0.0'",   0),
        "prompt_version":  ("TEXT",    1, "'0.0.0'",   0),
        "schema_version":  ("TEXT",    1, "'0.0.0'",   0),
        "code_sha":        ("TEXT",    1, "'unknown'", 0),
        "bucket_key_used": ("TEXT",    0, None,        0),
    }
    for name, (col_type, notnull, dflt, pk) in expected.items():
        assert name in cols, f"missing column on decisions: {name}"
        actual_type, actual_notnull, actual_dflt, actual_pk = cols[name]
        assert actual_type == col_type, f"{name}: type {actual_type!r} != {col_type!r}"
        assert actual_notnull == notnull, f"{name}: notnull {actual_notnull} != {notnull}"
        assert actual_dflt == dflt, f"{name}: default {actual_dflt!r} != {dflt!r}"
        assert actual_pk == pk, f"{name}: pk {actual_pk} != {pk}"


def test_v2_schema_extends_shadow_outcomes():
    conn = _v1_db()
    init_v2_schema(conn)
    cols = _pragma_cols(conn, "shadow_outcomes")
    for name in ("day_1_eod_pct", "day_2_eod_pct", "day_3_eod_pct"):
        assert name in cols, f"missing column on shadow_outcomes: {name}"
        col_type, notnull, dflt, _pk = cols[name]
        assert col_type == "REAL"
        assert notnull == 0, f"{name} should be nullable"
        assert dflt is None


def test_v2_schema_creates_position_trace():
    conn = _v1_db()
    init_v2_schema(conn)
    cols = _pragma_cols(conn, "position_trace")
    expected_cols = {
        "trace_id", "decision_id", "event_time", "event_type",
        "qty_delta", "fill_price", "new_stop_price", "intent",
    }
    assert set(cols.keys()) == expected_cols, (
        f"position_trace columns {set(cols.keys())} != {expected_cols}"
    )
    # trace_id is primary key
    _t, _n, _d, pk = cols["trace_id"]
    assert pk == 1, "trace_id should be primary key"
    # decision_id is NOT NULL
    _t, notnull, _d, _pk = cols["decision_id"]
    assert notnull == 1, "decision_id should be NOT NULL"
    # event_time / event_type are NOT NULL
    assert cols["event_time"][1] == 1
    assert cols["event_type"][1] == 1
    # Optional fields nullable
    assert cols["qty_delta"][1] == 0
    assert cols["fill_price"][1] == 0
    assert cols["new_stop_price"][1] == 0
    assert cols["intent"][1] == 0


def test_position_trace_has_decision_id_index():
    conn = _v1_db()
    init_v2_schema(conn)
    indexes = conn.execute("PRAGMA index_list(position_trace)").fetchall()
    # PRAGMA index_list rows: seq, name, unique, origin, partial
    names = [r[1] for r in indexes]
    assert any("decision_id" in n for n in names), (
        f"expected an index on position_trace.decision_id, got {names}"
    )


def test_v2_schema_idempotent():
    """Re-running init_v2_schema on a fully migrated DB must not raise
    or corrupt state. This is the key property: the live `_init_db`
    runs on every Orchestrator boot."""
    conn = _v1_db()
    init_v2_schema(conn)
    # Insert a row into position_trace to ensure data survives re-runs
    conn.execute("INSERT INTO decisions (ts, ticker, action) VALUES (?, ?, ?)",
                 (1234567890.0, "AAPL", "Buy"))
    decision_id = conn.execute("SELECT id FROM decisions").fetchone()[0]
    conn.execute(
        "INSERT INTO position_trace "
        "(decision_id, event_time, event_type, qty_delta, fill_price) "
        "VALUES (?, ?, ?, ?, ?)",
        (decision_id, "2026-05-12T14:30:00", "ENTRY", 10, 100.5),
    )

    # Re-run twice; both must be no-ops
    init_v2_schema(conn)
    init_v2_schema(conn)

    # Data survives
    rows = conn.execute(
        "SELECT decision_id, event_type, qty_delta, fill_price FROM position_trace"
    ).fetchall()
    assert rows == [(decision_id, "ENTRY", 10, 100.5)]

    # Column set unchanged
    cols = set(_pragma_cols(conn, "decisions").keys())
    assert "holding_day" in cols
    assert "bucket_key_used" in cols
    pt_cols = set(_pragma_cols(conn, "position_trace").keys())
    assert "trace_id" in pt_cols and "intent" in pt_cols


def test_column_exists_helper():
    """Direct unit test of the _column_exists helper."""
    conn = _v1_db()
    assert _column_exists(conn, "decisions", "ts") is True
    assert _column_exists(conn, "decisions", "nonexistent") is False
    # v2 column not yet added
    assert _column_exists(conn, "decisions", "holding_day") is False
    init_v2_schema(conn)
    assert _column_exists(conn, "decisions", "holding_day") is True


def test_add_column_if_missing_is_idempotent():
    """Direct unit test of _add_column_if_missing."""
    conn = _v1_db()
    _add_column_if_missing(conn, "decisions", "test_col", "INTEGER DEFAULT 7")
    assert _column_exists(conn, "decisions", "test_col")
    # Re-call must not raise even though column exists
    _add_column_if_missing(conn, "decisions", "test_col", "INTEGER DEFAULT 999")
    # Original default still in effect (no recreation)
    cols = _pragma_cols(conn, "decisions")
    assert cols["test_col"][2] == "7"
