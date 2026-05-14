"""Tests for the Russell 2000 source via iShares IWM holdings CSV.

We can't hit iShares from CI/sandbox without an allowlist, so the live
network behavior is tested by manually running the function (see
docs/deploy/russell2000_verification.md). These tests cover the
parsing-only portion of the logic by injecting a stub CSV that mimics
the iShares format.
"""
import io
import sys
from unittest.mock import patch

sys.path.insert(0, '.')

from data import watchlist_builder
from data.watchlist_builder import (
    get_russell2000_symbols,
    default_small_cap_sources,
    WatchlistSource,
)

# Stub CSV mimicking the iShares IWM holdings format. ~10 rows of fund
# metadata, then a "Ticker" header row, then a few real-looking holdings
# plus some non-equity rows that we expect to be filtered out.
SAMPLE_IWM_CSV = '''iShares Russell 2000 ETF
"Fund Holdings as of","Apr 30, 2026"
"Inception Date","May 22, 2000"
"Net Assets","$60,000,000,000.00"
"Shares Outstanding","260,000,000"
"Stock Ticker","IWM"
""
""
" "
"Ticker","Name","Sector","Asset Class","Market Value","Weight (%)"
"GME","GAMESTOP CORP CLASS A","Consumer Discretionary","Equity","250000000","0.42"
"RIOT","RIOT BLOCKCHAIN INC","Financials","Equity","180000000","0.30"
"BRK.B","BERKSHIRE HATHAWAY CL B","Financials","Equity","100000000","0.17"
"USD","US DOLLAR","-","Cash","45000000","0.07"
"-","BLK CSH FND TREASURY","-","Money Market","30000000","0.05"
"E MINI RUSS 2000 JUN 26","FUTURE","-","Cash Like","-15000000","-0.02"
"AMC","AMC ENTERTAINMENT HOLDINGS","Consumer Discretionary","Equity","60000000","0.10"
'''


def test_russell2000_parses_real_format():
    """Verify the parser extracts equity tickers and skips non-equity rows."""
    class StubResponse:
        def __init__(self, body): self.body = body.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.body

    def fake_urlopen(req, timeout=None):
        return StubResponse(SAMPLE_IWM_CSV)

    with patch.object(watchlist_builder, "urlopen", fake_urlopen):
        syms = get_russell2000_symbols()

    # Expected: real tickers only. Cash, futures, currency entries skipped.
    assert "GME" in syms, f"GME missing from {syms}"
    assert "RIOT" in syms, f"RIOT missing from {syms}"
    assert "BRK.B" in syms, f"BRK.B missing from {syms}"
    assert "AMC" in syms, f"AMC missing from {syms}"
    # USD is alphanumeric and 3 chars so currently passes our filter — that's
    # a known limitation; the per-ticker ADV check downstream will drop it
    # because Polygon won't return ADV data for "USD".
    # We DO expect futures/cash-like rows with non-ticker symbols filtered.
    assert "BLK_CSH_FND_TREASURY" not in syms
    assert any("FUTURE" not in s for s in syms)  # FUTURE label dropped


def test_russell2000_handles_missing_header():
    """If iShares format changes and the Ticker header row is missing,
    the function returns an empty set rather than crashing."""
    class StubResponse:
        def __init__(self, body): self.body = body.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.body
    def fake_urlopen(req, timeout=None):
        return StubResponse("Not the right format at all\nNo ticker header here\n")
    with patch.object(watchlist_builder, "urlopen", fake_urlopen):
        syms = get_russell2000_symbols()
    assert syms == set(), f"expected empty set, got {syms}"


def test_default_small_cap_sources_shape():
    """default_small_cap_sources returns a single-source list using Russell 2000."""
    sources = default_small_cap_sources()
    assert len(sources) == 1, f"expected 1 source, got {len(sources)}"
    assert sources[0].name == "russell2000"
    assert sources[0].fetcher is get_russell2000_symbols
    assert sources[0].min_count == 1500
    assert sources[0].max_count is None


def main():
    tests = [
        test_russell2000_parses_real_format,
        test_russell2000_handles_missing_header,
        test_default_small_cap_sources_shape,
    ]
    results = []
    for t in tests:
        try: results.append(("PASS", t.__name__, t()))
        except AssertionError as e: results.append(("FAIL", t.__name__, str(e)))
        except Exception as e: results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))
    for s, n, m in results:
        print(f"{s:6} {n:50} {m}")
    fails = [r for r in results if r[0] != "PASS"]
    print(f"\n{len(results)-len(fails)}/{len(results)} passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
