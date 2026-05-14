"""Tests for the universe parameterization of build_dynamic_watchlist
(2026-05-06 refactor for model-fork extension).

The refactor adds a `sources` parameter to build_dynamic_watchlist so
model-specific forks can plug in their own constituent universes (e.g.
Russell 2000 for gap-and-go on small caps) without modifying the build
logic. These tests pin down the parameterization behavior:

  - Default sources (no parameter) preserves Phase B behavior
  - Custom sources are honored
  - Sanity bounds on each source are enforced
  - Empty sources list aborts cleanly
"""
import asyncio
import sys
from unittest.mock import patch

sys.path.insert(0, '.')

from data.watchlist_builder import (
    WatchlistSource,
    build_dynamic_watchlist,
    default_large_cap_sources,
)


def test_watchlist_source_dataclass():
    """WatchlistSource holds name, fetcher, min/max counts."""
    def stub() -> set[str]:
        return {"AAPL"}
    src = WatchlistSource(name="test", fetcher=stub, min_count=1)
    assert src.name == "test"
    assert src.fetcher() == {"AAPL"}
    assert src.min_count == 1
    assert src.max_count is None
    src2 = WatchlistSource(name="t", fetcher=stub, min_count=1, max_count=10)
    assert src2.max_count == 10


def test_default_large_cap_sources_shape():
    """default_large_cap_sources returns the original three large-cap sources."""
    sources = default_large_cap_sources()
    names = {s.name for s in sources}
    assert names == {"sp500", "nasdaq100", "djia"}, f"got {names}"
    by_name = {s.name: s for s in sources}
    assert by_name["sp500"].min_count == 400
    assert by_name["nasdaq100"].min_count == 90
    assert by_name["nasdaq100"].max_count == 110
    assert by_name["djia"].min_count == 30
    assert by_name["djia"].max_count == 30


async def test_custom_sources_used():
    """Custom sources are passed through to the build."""
    def fake_universe_a() -> set[str]:
        return {"AAA", "BBB", "CCC"}
    def fake_universe_b() -> set[str]:
        return {"BBB", "DDD"}
    sources = [
        WatchlistSource("alpha", fake_universe_a, min_count=2),
        WatchlistSource("beta", fake_universe_b, min_count=1),
    ]
    # Mock fetch_30day_adv so we don't hit Polygon
    async def fake_advs(symbols, key):
        return {s: float(ord(s[0])) * 1e6 for s in symbols}
    with patch("data.watchlist_builder.fetch_30day_adv", fake_advs):
        result, metadata = await build_dynamic_watchlist(
            polygon_key="fake", top_n=10, sources=sources,
        )
    assert set(result) == {"AAA", "BBB", "CCC", "DDD"}, f"got {result}"
    assert metadata["source_counts"] == {"alpha": 3, "beta": 2}
    assert metadata["union_size"] == 4


async def test_sources_below_min_count_aborts():
    """A source returning fewer than min_count aborts the refresh."""
    def too_few() -> set[str]:
        return {"AAA"}
    sources = [WatchlistSource("alpha", too_few, min_count=5)]
    async def fake_advs(symbols, key):
        return {}
    with patch("data.watchlist_builder.fetch_30day_adv", fake_advs):
        result, metadata = await build_dynamic_watchlist(
            polygon_key="fake", top_n=10, sources=sources,
        )
    assert result == []
    assert metadata == {}


async def test_sources_above_max_count_aborts():
    """A source returning more than max_count aborts the refresh."""
    def too_many() -> set[str]:
        return {f"S{i}" for i in range(50)}
    sources = [WatchlistSource("alpha", too_many, min_count=1, max_count=10)]
    async def fake_advs(symbols, key):
        return {}
    with patch("data.watchlist_builder.fetch_30day_adv", fake_advs):
        result, metadata = await build_dynamic_watchlist(
            polygon_key="fake", top_n=10, sources=sources,
        )
    assert result == []
    assert metadata == {}


async def test_empty_sources_list_aborts():
    """Empty sources list returns empty result, no API call."""
    sources: list[WatchlistSource] = []
    api_called = {"n": 0}
    async def fake_advs(symbols, key):
        api_called["n"] += 1
        return {}
    with patch("data.watchlist_builder.fetch_30day_adv", fake_advs):
        result, metadata = await build_dynamic_watchlist(
            polygon_key="fake", top_n=10, sources=sources,
        )
    assert result == []
    assert metadata == {}
    assert api_called["n"] == 0, "should not call ADV API if no sources"


async def test_default_sources_used_when_none():
    """Passing sources=None falls back to default_large_cap_sources."""
    # Mock the three default fetchers + ADV so we don't hit network
    fake_sp500 = {f"SP{i}" for i in range(450)}
    fake_ndx = {f"NX{i}" for i in range(100)}
    fake_djia = {f"DJ{i}" for i in range(30)}
    async def fake_advs(symbols, key):
        return {s: 1e9 for s in symbols}
    with patch("data.watchlist_builder.get_sp500_symbols", lambda: fake_sp500), \
         patch("data.watchlist_builder.get_nasdaq100_symbols", lambda: fake_ndx), \
         patch("data.watchlist_builder.get_djia_symbols", lambda: fake_djia), \
         patch("data.watchlist_builder.fetch_30day_adv", fake_advs):
        result, metadata = await build_dynamic_watchlist(
            polygon_key="fake", top_n=500, sources=None,
        )
    assert "sp500" in metadata["source_counts"]
    assert "nasdaq100" in metadata["source_counts"]
    assert "djia" in metadata["source_counts"]
    assert metadata["source_counts"]["sp500"] == 450
    assert metadata["source_counts"]["nasdaq100"] == 100
    assert metadata["source_counts"]["djia"] == 30
    assert len(result) == 500


def main():
    sync_tests = [
        test_watchlist_source_dataclass,
        test_default_large_cap_sources_shape,
    ]
    async_tests = [
        test_custom_sources_used,
        test_sources_below_min_count_aborts,
        test_sources_above_max_count_aborts,
        test_empty_sources_list_aborts,
        test_default_sources_used_when_none,
    ]
    results = []
    for t in sync_tests:
        try: results.append(("PASS", t.__name__, t()))
        except AssertionError as e: results.append(("FAIL", t.__name__, str(e)))
        except Exception as e: results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))
    for t in async_tests:
        try: results.append(("PASS", t.__name__, asyncio.run(t())))
        except AssertionError as e: results.append(("FAIL", t.__name__, str(e)))
        except Exception as e: results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))
    for s, n, m in results:
        print(f"{s:6} {n:50} {m}")
    fails = [r for r in results if r[0] != "PASS"]
    print(f"\n{len(results)-len(fails)}/{len(results)} passed")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
