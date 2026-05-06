"""Tests for data/pm_rvol_thresholds.py (Phase C, 2026-05-06).

The runtime threshold loader has to be defensively coded — it's called
during signal evaluation for every ticker on every bar, so any unhandled
exception kills the live trading loop. These tests pin down the failure
modes explicitly:

  - missing file
  - malformed JSON
  - missing ticker
  - missing _default
  - non-numeric values
  - negative/zero values
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, '.')

from data.pm_rvol_thresholds import (
    HARD_FALLBACK_THRESHOLD,
    get_threshold,
    load_thresholds,
)


def test_get_threshold_present():
    t = {"AAPL": 3.8, "MSFT": 4.2, "_default": 5.0}
    assert get_threshold(t, "AAPL") == 3.8
    assert get_threshold(t, "MSFT") == 4.2
    return "ticker present returns its value"


def test_get_threshold_absent_with_default():
    t = {"AAPL": 3.8, "_default": 5.0}
    assert get_threshold(t, "ZZZZ") == 5.0
    return "ticker absent + _default present returns _default"


def test_get_threshold_absent_without_default():
    t = {"AAPL": 3.8}
    assert get_threshold(t, "ZZZZ") == HARD_FALLBACK_THRESHOLD
    return "ticker absent + no _default returns HARD_FALLBACK"


def test_get_threshold_empty_dict():
    assert get_threshold({}, "AAPL") == HARD_FALLBACK_THRESHOLD
    return "empty dict returns HARD_FALLBACK"


def test_get_threshold_invalid_value_falls_back():
    t = {"AAPL": "not a number", "_default": 5.0}
    assert get_threshold(t, "AAPL") == 5.0
    return "non-numeric ticker value falls back to _default"


def test_get_threshold_zero_value_falls_back():
    t = {"AAPL": 0.0, "_default": 5.0}
    assert get_threshold(t, "AAPL") == 5.0
    return "zero ticker value falls back to _default"


def test_get_threshold_negative_value_falls_back():
    t = {"AAPL": -1.0, "_default": 5.0}
    assert get_threshold(t, "AAPL") == 5.0
    return "negative ticker value falls back to _default"


def test_get_threshold_invalid_default_falls_back_to_hard():
    t = {"AAPL": "bad", "_default": "also bad"}
    assert get_threshold(t, "AAPL") == HARD_FALLBACK_THRESHOLD
    return "invalid _default falls back to HARD_FALLBACK"


def test_get_threshold_metadata_key_ignored():
    t = {"AAPL": 3.8, "_metadata": {"computed_at": "2026-05-06"}}
    # _metadata isn't a ticker; lookup of an absent ticker should fall back
    assert get_threshold(t, "ZZZZ") == HARD_FALLBACK_THRESHOLD
    return "_metadata key is not treated as a ticker entry"


def test_load_thresholds_missing_file():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nonexistent.json"
        result = load_thresholds(path)
        assert result == {}
    return "missing file returns empty dict (no exception)"


def test_load_thresholds_malformed_json():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bad.json"
        path.write_text("{ this is not json")
        result = load_thresholds(path)
        assert result == {}
    return "malformed JSON returns empty dict (no exception)"


def test_load_thresholds_non_dict_root():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "list.json"
        path.write_text('["AAPL", "MSFT"]')
        result = load_thresholds(path)
        assert result == {}
    return "non-dict root returns empty dict"


def test_load_thresholds_valid():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "valid.json"
        path.write_text(json.dumps({
            "AAPL": 3.8,
            "MSFT": 4.2,
            "_default": 5.0,
            "_metadata": {"computed_at": "2026-05-06T12:00:00Z"},
        }))
        result = load_thresholds(path)
        assert result["AAPL"] == 3.8
        assert result["MSFT"] == 4.2
        assert result["_default"] == 5.0
        assert "_metadata" in result
    return "valid JSON loads correctly"


def test_load_thresholds_round_trip_via_get_threshold():
    """Integration: write file, load it, look up tickers via get_threshold."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "round.json"
        path.write_text(json.dumps({
            "AAPL": 3.8,
            "_default": 5.5,
        }))
        loaded = load_thresholds(path)
        assert get_threshold(loaded, "AAPL") == 3.8
        assert get_threshold(loaded, "ZZZZ") == 5.5
    return "round-trip: write -> load -> lookup works"


def main():
    tests = [
        test_get_threshold_present,
        test_get_threshold_absent_with_default,
        test_get_threshold_absent_without_default,
        test_get_threshold_empty_dict,
        test_get_threshold_invalid_value_falls_back,
        test_get_threshold_zero_value_falls_back,
        test_get_threshold_negative_value_falls_back,
        test_get_threshold_invalid_default_falls_back_to_hard,
        test_get_threshold_metadata_key_ignored,
        test_load_thresholds_missing_file,
        test_load_thresholds_malformed_json,
        test_load_thresholds_non_dict_root,
        test_load_thresholds_valid,
        test_load_thresholds_round_trip_via_get_threshold,
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
        print(f"{s:6} {n:55} {m}")
    fails = [r for r in results if r[0] != "PASS"]
    print(f"\n{len(results)-len(fails)}/{len(results)} passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
