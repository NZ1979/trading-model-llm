"""Regression test for Bug B fix.

Setup: replicate the production _evaluate_and_execute path with the patched
SymbolState defaults (None sentinels). Verify:

(1) On the FIRST steady-state Hold/none decision per ticker, a row IS
    written to the decisions DB (the dedup-default match no longer prevents
    the first write).
(2) On a SECOND identical Hold/none decision for the same ticker, no NEW
    row is written (dedup still works after the first write, because state
    has been updated to "Hold"/"none" and the action+setup match).
(3) When the decision changes (e.g., setup transitions to "pullback"), a
    new row is written.
"""
import asyncio, sqlite3, sys, time
from collections import deque
from dataclasses import dataclass
from datetime import timezone
from zoneinfo import ZoneInfo

import numpy as np, pandas as pd
sys.path.insert(0, '.')

from analysis.indicators import TechnicalSignal
from strategy.signal_engine import evaluate_trade

ET = ZoneInfo("America/New_York")


# Replicate the patched SymbolState shape (None defaults)
@dataclass
class SymbolStatePatched:
    ticker: str
    last_decision_action: str | None = None  # PATCHED
    last_decision_setup: str | None = None    # PATCHED


def attempt_log(state, decision, db):
    is_actionable = decision.action != "Hold"
    is_changed = (
        decision.action != state.last_decision_action
        or decision.setup != state.last_decision_setup
    )
    if is_actionable or is_changed:
        db.execute("INSERT INTO decisions (ts, ticker, action, setup, sentiment, "
                   "confidence, walls_status, reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (time.time(), decision.ticker, decision.action, decision.setup,
                    decision.sentiment_score, decision.technical_confidence,
                    decision.walls_status, " | ".join(decision.reasons)))
        state.last_decision_action = decision.action
        state.last_decision_setup = decision.setup
        return True
    return False


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE decisions (
        id INTEGER PRIMARY KEY, ts REAL, ticker TEXT, action TEXT, setup TEXT,
        sentiment INTEGER, confidence INTEGER, walls_status TEXT, reasons TEXT)""")
    return db


def hold_none_decision(ticker):
    """A typical steady-state result."""
    sig = TechnicalSignal("Hold", 0, "none", ("no_setup",))
    return evaluate_trade(ticker, None, sig, futures_walls=None,
                           require_walls_for_pullback=False)


def hold_pullback_decision(ticker):
    """The result when pullback technicals fire but sentiment is too low."""
    sig = TechnicalSignal("Buy", 80, "pullback", ("daily_regime=bull", "rsi_low"))
    return evaluate_trade(ticker, 2, sig, futures_walls=None,  # sent=2 < +5
                           require_walls_for_pullback=False)


def buy_pullback_decision(ticker):
    sig = TechnicalSignal("Buy", 80, "pullback", ("daily_regime=bull", "rsi_low"))
    return evaluate_trade(ticker, 6, sig, futures_walls=None,  # sent=+6 >= +5
                           require_walls_for_pullback=False)


def test_first_hold_logs_under_patch():
    """With None defaults, the first Hold/none for a ticker should be logged."""
    state = SymbolStatePatched(ticker="AAPL")
    db = fresh_db()
    decision = hold_none_decision("AAPL")
    logged = attempt_log(state, decision, db)
    rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert logged is True, "First Hold/none should be logged (dedup default fixed)"
    assert rows == 1, f"Expected 1 row, got {rows}"


def test_second_identical_hold_does_not_relog():
    """After first Hold/none is logged, state == defaults-replaced. A second
    identical Hold/none should NOT log."""
    state = SymbolStatePatched(ticker="AAPL")
    db = fresh_db()
    attempt_log(state, hold_none_decision("AAPL"), db)
    logged = attempt_log(state, hold_none_decision("AAPL"), db)
    rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert logged is False
    assert rows == 1, f"Expected still 1 row after duplicate, got {rows}"


def test_setup_transition_logs():
    """First Hold/none -> Hold/pullback should log because setup changed."""
    state = SymbolStatePatched(ticker="AAPL")
    db = fresh_db()
    attempt_log(state, hold_none_decision("AAPL"), db)
    logged = attempt_log(state, hold_pullback_decision("AAPL"), db)
    rows = db.execute("SELECT setup FROM decisions").fetchall()
    assert logged is True
    assert len(rows) == 2
    assert rows[0][0] == "none" and rows[1][0] == "pullback"


def test_actionable_decision_always_logs():
    state = SymbolStatePatched(ticker="AAPL")
    db = fresh_db()
    logged = attempt_log(state, buy_pullback_decision("AAPL"), db)
    rows = db.execute("SELECT action, setup FROM decisions").fetchall()
    assert logged is True
    assert rows[0] == ("Buy", "pullback")


def test_independent_tickers_each_log_first():
    """503 tickers × first Hold/none each = 503 rows, not zero."""
    db = fresh_db()
    for t in [f"T{i}" for i in range(503)]:
        state = SymbolStatePatched(ticker=t)
        attempt_log(state, hold_none_decision(t), db)
    rows = db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert rows == 503, f"Expected 503 rows (one per ticker), got {rows}"


def main():
    tests = [
        test_first_hold_logs_under_patch,
        test_second_identical_hold_does_not_relog,
        test_setup_transition_logs,
        test_actionable_decision_always_logs,
        test_independent_tickers_each_log_first,
    ]
    results = []
    for t in tests:
        try:
            r = t()
            results.append(("PASS", t.__name__, r))
        except AssertionError as e:
            results.append(("FAIL", t.__name__, str(e)))
        except Exception as e:
            results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))
    for s, n, m in results:
        print(f"{s:6} {n:50} {m}")
    fails = [r for r in results if r[0] != "PASS"]
    print(f"\n{len(results)-len(fails)}/{len(results)} passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
