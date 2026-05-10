"""Verification for strategy.llm.metrics (A.2).

Hand-computed expected values against synthetic bars exercise the
forward-return, MAE/MFE, stop/target-touch, Calmar, and realized-R math.
Pure math; no API calls, no DB reads.

Run with:
    cd "C:\\trading\\LLM model"
    $env:PYTHONPATH = "."
    python scripts/verify_metrics.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from strategy.llm.metrics import (
    Bar,
    compute_calmar,
    compute_max_drawdown,
    compute_outcome,
    compute_realized_r,
)


def _print(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK " if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def _make_bars(prices: list[tuple[float, float, float, float]], start_ts: float = 0.0) -> list[Bar]:
    """Build 1-min bars from (open, high, low, close) tuples starting at start_ts."""
    return [
        Bar(ts=start_ts + i * 60, open=o, high=h, low=l, close=c, volume=10_000)
        for i, (o, h, l, c) in enumerate(prices)
    ]


def main() -> int:
    all_ok = True

    # -----------------------------------------------------------------
    # compute_outcome — long winner
    # -----------------------------------------------------------------
    # Buy at $100, stop at $97 (3% stop), target at $106 (6% target).
    # Bars: price drifts up to $108 over 60 min, never touches stop.
    bars = _make_bars([
        # min 0-4: 100 -> 102
        (100.0, 100.5, 99.8, 100.2),
        (100.2, 101.0, 100.0, 100.8),
        (100.8, 101.5, 100.5, 101.2),
        (101.2, 102.0, 101.0, 101.8),
        (101.8, 102.5, 101.5, 102.0),
        # min 5-14: 102 -> 105
        (102.0, 103.0, 102.0, 102.5),
        (102.5, 103.5, 102.3, 103.0),
        (103.0, 104.0, 102.8, 103.5),
        (103.5, 104.5, 103.3, 104.0),
        (104.0, 105.0, 103.8, 104.5),
        (104.5, 105.5, 104.3, 105.0),
        (105.0, 105.8, 104.8, 105.5),
        (105.5, 106.0, 105.3, 105.8),
        (105.8, 106.2, 105.5, 106.0),
        (106.0, 106.5, 105.8, 106.2),
        # min 15-29: 105.5 -> 107
        *[(106.0 + i * 0.05, 106.2 + i * 0.05, 105.7 + i * 0.05, 106.1 + i * 0.05) for i in range(15)],
        # min 30-59: 107 -> 108
        *[(106.85 + i * 0.04, 107.0 + i * 0.04, 106.6 + i * 0.04, 106.9 + i * 0.04) for i in range(30)],
    ])
    outcome = compute_outcome(
        decision_id=1,
        decision_ts=0.0,
        decision_price=100.0,
        side="buy",
        stop_price=97.0,
        target_price=106.0,
        bars=bars,
        eod_ts=60 * 60 * 6.5,  # 6.5h after open
    )
    all_ok &= _print(
        "long winner: target hits, stop doesn't",
        outcome.target_would_hit
        and not outcome.stop_would_hit
        and outcome.first_touch == "target"
        and outcome.target_hit_at_minutes is not None,
        f"first_touch={outcome.first_touch} target@={outcome.target_hit_at_minutes}m",
    )
    all_ok &= _print(
        "long winner: MFE positive, MAE near zero",
        outcome.mfe_pct > 5.0  # price went above $105 = +5% from $100
        and -1.0 < outcome.mae_pct <= 0.0,
        f"mfe={outcome.mfe_pct:.2f}% mae={outcome.mae_pct:.2f}%",
    )
    all_ok &= _print(
        "long winner: 5m return positive (price rose by 5m)",
        outcome.return_5m_pct is not None and outcome.return_5m_pct > 1.0,
        f"5m return={outcome.return_5m_pct:.2f}%",
    )

    # -----------------------------------------------------------------
    # compute_outcome — long loser (stop hits)
    # -----------------------------------------------------------------
    # Buy at $100, stop at $97. Price drops to $96 by min 10. Stop hits.
    bars = _make_bars([
        (100.0, 100.2, 99.5, 99.8),
        (99.8, 99.9, 99.0, 99.2),
        (99.2, 99.3, 98.5, 98.7),
        (98.7, 98.9, 98.0, 98.3),
        (98.3, 98.5, 97.5, 97.8),
        (97.8, 98.0, 97.0, 97.3),
        (97.3, 97.5, 96.8, 97.0),
        (97.0, 97.2, 96.5, 96.7),
        (96.7, 97.0, 96.0, 96.5),  # min 8: low=96.0 < stop 97.0 -> stop hit
        (96.5, 96.7, 96.3, 96.5),
    ])
    outcome = compute_outcome(
        decision_id=2,
        decision_ts=0.0,
        decision_price=100.0,
        side="buy",
        stop_price=97.0,
        target_price=106.0,
        bars=bars,
        eod_ts=60 * 60 * 6.5,
    )
    all_ok &= _print(
        "long loser: stop hits before target",
        outcome.stop_would_hit
        and not outcome.target_would_hit
        and outcome.first_touch == "stop",
        f"first_touch={outcome.first_touch} stop@={outcome.stop_hit_at_minutes}m",
    )
    all_ok &= _print(
        "long loser: MAE deeply negative, MFE near zero",
        outcome.mae_pct < -3.0 and 0.0 <= outcome.mfe_pct < 0.5,
        f"mae={outcome.mae_pct:.2f}% mfe={outcome.mfe_pct:.2f}%",
    )

    # -----------------------------------------------------------------
    # compute_outcome — short winner
    # -----------------------------------------------------------------
    # Sell at $100, stop at $103 (above), target at $94 (below). Price drops to $93.
    bars = _make_bars([
        (100.0, 100.2, 99.0, 99.5),
        (99.5, 99.8, 98.5, 99.0),
        (99.0, 99.5, 98.0, 98.5),
        (98.5, 98.8, 97.5, 98.0),
        (98.0, 98.3, 97.0, 97.5),
        (97.5, 97.8, 96.5, 97.0),
        (97.0, 97.5, 96.0, 96.5),
        (96.5, 96.8, 95.5, 96.0),
        (96.0, 96.3, 95.0, 95.5),
        (95.5, 95.8, 94.5, 95.0),
        (95.0, 95.3, 94.0, 94.5),
        (94.5, 94.8, 93.5, 94.0),
        (94.0, 94.3, 93.0, 93.5),  # min 12: low=93.0 < target 94.0 -> target hit
    ])
    outcome = compute_outcome(
        decision_id=3,
        decision_ts=0.0,
        decision_price=100.0,
        side="sell",
        stop_price=103.0,
        target_price=94.0,
        bars=bars,
        eod_ts=60 * 60 * 6.5,
    )
    all_ok &= _print(
        "short winner: target hits (price drops to target)",
        outcome.target_would_hit
        and not outcome.stop_would_hit
        and outcome.first_touch == "target",
        f"first_touch={outcome.first_touch} target@={outcome.target_hit_at_minutes}m mfe={outcome.mfe_pct:.2f}%",
    )
    all_ok &= _print(
        "short winner: MFE positive (favorable = price drops for short)",
        outcome.mfe_pct > 5.0,
        f"mfe={outcome.mfe_pct:.2f}%",
    )

    # -----------------------------------------------------------------
    # compute_outcome — Hold (no stop/target, treated as Buy for measurement)
    # -----------------------------------------------------------------
    bars = _make_bars([
        (100.0, 100.5, 99.5, 100.2),
        (100.2, 101.0, 100.0, 100.8),
        (100.8, 101.5, 100.5, 101.0),
        (101.0, 101.2, 100.8, 101.0),
        (101.0, 101.5, 100.5, 101.2),
        (101.2, 101.8, 100.9, 101.5),
    ])
    outcome = compute_outcome(
        decision_id=4,
        decision_ts=0.0,
        decision_price=100.0,
        side="hold",
        stop_price=None,
        target_price=None,
        bars=bars,
        eod_ts=60 * 60 * 6.5,
    )
    all_ok &= _print(
        "hold: first_touch is 'n/a', no stop/target hits even with prices that would have triggered them",
        outcome.first_touch == "n/a"
        and not outcome.stop_would_hit
        and not outcome.target_would_hit
        and outcome.return_5m_pct is not None,
        f"first_touch={outcome.first_touch} 5m={outcome.return_5m_pct:.2f}%",
    )

    # -----------------------------------------------------------------
    # compute_outcome — empty bars
    # -----------------------------------------------------------------
    outcome = compute_outcome(
        decision_id=5, decision_ts=0.0, decision_price=100.0,
        side="buy", stop_price=97.0, target_price=106.0,
        bars=[], eod_ts=60 * 60 * 6.5,
    )
    all_ok &= _print(
        "empty bars: returns empty outcome with horizon_complete reason",
        outcome.return_5m_pct is None
        and outcome.horizon_complete == "no_bars",
        f"horizon_complete={outcome.horizon_complete}",
    )

    # -----------------------------------------------------------------
    # compute_max_drawdown
    # -----------------------------------------------------------------
    # Equity curve: 100 -> 110 -> 95 -> 105 -> 90 -> 100
    # Two peaks: 110 at d2, 105 at d4. Two troughs: 95 at d3, 90 at d5.
    # Max DD: from 110 down to 90 = 20/110 = 18.18%
    base = date(2026, 1, 1)
    curve = [
        (base + timedelta(days=0), 100.0),
        (base + timedelta(days=1), 110.0),
        (base + timedelta(days=2), 95.0),
        (base + timedelta(days=3), 105.0),
        (base + timedelta(days=4), 90.0),
        (base + timedelta(days=5), 100.0),
    ]
    max_dd, peak_d, trough_d = compute_max_drawdown(curve)
    expected_dd = (110.0 - 90.0) / 110.0 * 100.0
    all_ok &= _print(
        "max_drawdown: 18.18% from 110 peak to 90 trough",
        abs(max_dd - expected_dd) < 0.01
        and peak_d == base + timedelta(days=1)
        and trough_d == base + timedelta(days=4),
        f"max_dd={max_dd:.2f}% peak={peak_d} trough={trough_d}",
    )

    # Empty / single-point series
    md_empty = compute_max_drawdown([])
    md_one = compute_max_drawdown([(base, 100.0)])
    all_ok &= _print(
        "max_drawdown: empty / single-point returns (0, None, None)",
        md_empty == (0.0, None, None) and md_one == (0.0, None, None),
    )

    # -----------------------------------------------------------------
    # compute_calmar
    # -----------------------------------------------------------------
    # Build a clean equity curve: starts at 100k, grows to 110k over 90 trading days,
    # with a 5% drawdown along the way. Calmar = annualized_return / max_dd_pct.
    curve = []
    eq = 100_000.0
    peak_eq = eq
    for i in range(90):
        d = base + timedelta(days=i)
        # First 30 days: grow to 105k
        # Days 30-50: drop to 99.75k (5% drawdown from 105k peak)
        # Days 50-90: recover and grow to 110k
        if i < 30:
            eq = 100_000.0 + i * (105_000.0 - 100_000.0) / 29.0
        elif i < 50:
            eq = 105_000.0 - (i - 30) * (105_000.0 - 99_750.0) / 19.0
        else:
            eq = 99_750.0 + (i - 50) * (110_000.0 - 99_750.0) / 39.0
        curve.append((d, eq))

    calmar = compute_calmar(curve, window_days=120)
    # Annualized return: (110k/100k)^(252/90) - 1 ≈ 0.30 (30%)
    # Max DD: 5%
    # Calmar ≈ 30 / 5 = 6.0
    all_ok &= _print(
        "calmar: 90-day curve with ~30% annualized + 5% DD -> Calmar ~6",
        calmar is not None and 4.0 < calmar < 9.0,
        f"calmar={calmar:.2f}",
    )

    # No drawdown -> None (avoid divide-by-zero downstream)
    flat_curve = [(base + timedelta(days=i), 100_000.0 + i * 100.0) for i in range(30)]
    calmar_flat = compute_calmar(flat_curve)
    all_ok &= _print(
        "calmar: monotonically rising series returns None (no DD to divide by)",
        calmar_flat is None,
        f"calmar_flat={calmar_flat}",
    )

    # -----------------------------------------------------------------
    # compute_realized_r
    # -----------------------------------------------------------------
    # Long: stop at $97, target at $106 from entry $100.
    # If stop hit: r = -1.0
    stop_outcome = type(outcome)(  # quick mock
        decision_id=1, return_5m_pct=-3.0, return_15m_pct=None, return_30m_pct=None,
        return_60m_pct=None, return_eod_pct=-3.0,
        mae_pct=-3.0, mfe_pct=0.5, mae_at_minutes=8, mfe_at_minutes=2,
        stop_would_hit=True, stop_hit_at_minutes=8,
        target_would_hit=False, target_hit_at_minutes=None,
        first_touch="stop", avg_spread_bps=10.0, estimated_slippage_bps=5.0,
        horizon_complete="60m",
    )
    r = compute_realized_r(
        decision_price=100.0, side="buy",
        stop_price=97.0, target_price=106.0,
        take_profit_atr_multiple=2.0, stop_loss_atr_multiple=1.0,
        outcome=stop_outcome,
    )
    all_ok &= _print(
        "realized_r: stop hit -> -1.0",
        r is not None and r == -1.0,
        f"r={r}",
    )

    # Target hit: R = take_profit_atr / stop_loss_atr = 2.0 / 1.0 = 2.0
    target_outcome = type(outcome)(
        decision_id=2, return_5m_pct=2.0, return_15m_pct=4.0, return_30m_pct=6.0,
        return_60m_pct=6.0, return_eod_pct=5.5,
        mae_pct=-0.3, mfe_pct=6.5, mae_at_minutes=1, mfe_at_minutes=20,
        stop_would_hit=False, stop_hit_at_minutes=None,
        target_would_hit=True, target_hit_at_minutes=18,
        first_touch="target", avg_spread_bps=10.0, estimated_slippage_bps=5.0,
        horizon_complete="60m",
    )
    r = compute_realized_r(
        decision_price=100.0, side="buy",
        stop_price=97.0, target_price=106.0,
        take_profit_atr_multiple=2.0, stop_loss_atr_multiple=1.0,
        outcome=target_outcome,
    )
    all_ok &= _print(
        "realized_r: target hit -> tp_atr / stop_atr = 2.0",
        r is not None and abs(r - 2.0) < 0.001,
        f"r={r}",
    )

    # Neither hit: R = eod_return / stop_distance_pct
    # decision=100, stop=97 -> stop_dist_pct = 3%. eod_return = +1.5%. R = 0.5
    neither_outcome = type(outcome)(
        decision_id=3, return_5m_pct=0.2, return_15m_pct=0.5, return_30m_pct=1.0,
        return_60m_pct=1.2, return_eod_pct=1.5,
        mae_pct=-0.5, mfe_pct=1.8, mae_at_minutes=1, mfe_at_minutes=45,
        stop_would_hit=False, stop_hit_at_minutes=None,
        target_would_hit=False, target_hit_at_minutes=None,
        first_touch="neither", avg_spread_bps=10.0, estimated_slippage_bps=5.0,
        horizon_complete="final",
    )
    r = compute_realized_r(
        decision_price=100.0, side="buy",
        stop_price=97.0, target_price=106.0,
        take_profit_atr_multiple=2.0, stop_loss_atr_multiple=1.0,
        outcome=neither_outcome,
    )
    expected_r = 1.5 / 3.0  # 0.5
    all_ok &= _print(
        "realized_r: neither hit -> eod_return / stop_dist_pct = 0.5",
        r is not None and abs(r - expected_r) < 0.001,
        f"r={r:.3f}",
    )

    # Hold: returns None (no R-multiple applies)
    r = compute_realized_r(
        decision_price=100.0, side="hold",
        stop_price=None, target_price=None,
        take_profit_atr_multiple=None, stop_loss_atr_multiple=None,
        outcome=neither_outcome,
    )
    all_ok &= _print(
        "realized_r: Hold -> None",
        r is None,
        f"r={r}",
    )

    print()
    print("ALL OK" if all_ok else "SOME FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
