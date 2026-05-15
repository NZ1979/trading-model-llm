"""Tests for data/fred_vix.py.

Covers:
  - URL api_key scrubber: redaction, idempotency, case-insensitivity,
    multiple occurrences, no-match passthrough
  - _require_fred_key: present, missing, empty
  - get_vix_history: success path with sentinel skip, empty
    observations, all-sentinels, bad date range, 4xx with scrubbed
    URL in error message
  - get_vix_eod: walks back to most recent close, returns None when
    no observations available

httpx is mocked via httpx.MockTransport — same library, real Response
objects, no fake-class drift. The async client construction is
monkeypatched at the fred_vix module's import site so the production
code path is exercised end-to-end.
"""
from __future__ import annotations

import sys
from datetime import date
from typing import Callable

import httpx
import pytest

sys.path.insert(0, '.')

from data import fred_vix
from data.fred_vix import (
    _require_fred_key,
    _scrub_apikey,
    get_vix_eod,
    get_vix_history,
)


# ---------------------------------------------------------------------------
# _scrub_apikey
# ---------------------------------------------------------------------------


def test_scrub_apikey_redacts_in_url():
    s = ("https://api.stlouisfed.org/fred/series/observations"
         "?api_key=ABCDEF1234567890&series_id=VIXCLS")
    out = _scrub_apikey(s)
    assert "ABCDEF1234567890" not in out
    assert "api_key=<redacted>" in out
    assert "series_id=VIXCLS" in out


def test_scrub_apikey_idempotent_on_already_scrubbed():
    s = "api_key=<redacted>&series_id=VIXCLS"
    assert _scrub_apikey(s) == s


def test_scrub_apikey_no_match_returns_unchanged():
    s = "https://example.com/no/key/here"
    assert _scrub_apikey(s) == s


def test_scrub_apikey_case_insensitive():
    s = "Api_Key=SECRET&Series_Id=VIXCLS"
    out = _scrub_apikey(s)
    assert "SECRET" not in out


def test_scrub_apikey_replaces_multiple_occurrences():
    s = "api_key=AAA&other=x&api_key=BBB"
    out = _scrub_apikey(s)
    assert "AAA" not in out
    assert "BBB" not in out
    assert out.count("<redacted>") == 2


# ---------------------------------------------------------------------------
# _require_fred_key
# ---------------------------------------------------------------------------


def test_require_fred_key_present(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key-xyz")
    assert _require_fred_key() == "test-key-xyz"


def test_require_fred_key_missing_raises(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        _require_fred_key()


def test_require_fred_key_empty_raises(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "")
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        _require_fred_key()


# ---------------------------------------------------------------------------
# get_vix_history — mocked httpx via MockTransport
# ---------------------------------------------------------------------------


def _install_mock(monkeypatch, handler: Callable[[httpx.Request], httpx.Response]):
    """Patch fred_vix's AsyncClient so it uses an in-process MockTransport.

    Returns a list that gets appended each time the handler is invoked,
    so tests can assert on the request URLs / params after the call.

    Note on the closure: ``httpx.AsyncClient`` is captured in
    ``real_async_client`` BEFORE the monkeypatch fires. Without this,
    the factory would call ``httpx.AsyncClient(...)`` which after
    patching IS the factory itself → infinite recursion. The closure
    holds the real class so factory calls construct a real client
    backed by MockTransport.
    """
    seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient  # capture BEFORE patching

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        return real_async_client(transport=httpx.MockTransport(wrapped_handler))

    monkeypatch.setattr("data.fred_vix.httpx.AsyncClient", factory)
    return seen


@pytest.mark.asyncio
async def test_get_vix_history_success_skips_sentinels(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [
            {"date": "2026-04-01", "value": "18.42"},
            {"date": "2026-04-02", "value": "19.10"},
            {"date": "2026-04-04", "value": "."},   # Saturday: sentinel
            {"date": "2026-04-05", "value": "."},   # Sunday: sentinel
            {"date": "2026-04-06", "value": "18.95"},
        ]
    }))
    df = await get_vix_history(date(2026, 4, 1), date(2026, 4, 6))
    assert len(df) == 3
    assert df["vix_close"].iloc[0] == pytest.approx(18.42)
    assert df["vix_close"].iloc[-1] == pytest.approx(18.95)
    # Index should be sorted ascending.
    assert list(df.index) == sorted(df.index)


@pytest.mark.asyncio
async def test_get_vix_history_empty_observations_raises(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": []
    }))
    with pytest.raises(RuntimeError, match="0 observations"):
        await get_vix_history(date(2026, 4, 1), date(2026, 4, 6))


@pytest.mark.asyncio
async def test_get_vix_history_only_sentinels_raises(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [
            {"date": "2026-04-04", "value": "."},
            {"date": "2026-04-05", "value": "."},
        ]
    }))
    with pytest.raises(RuntimeError, match="sentinel"):
        await get_vix_history(date(2026, 4, 4), date(2026, 4, 5))


@pytest.mark.asyncio
async def test_get_vix_history_bad_date_range_raises():
    with pytest.raises(ValueError, match="end_date"):
        await get_vix_history(date(2026, 4, 30), date(2026, 4, 1))


@pytest.mark.asyncio
async def test_get_vix_history_4xx_raises_with_scrubbed_url(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "MYSECRETKEY")
    _install_mock(monkeypatch, lambda req: httpx.Response(
        400, text="Bad Request: invalid api_key"
    ))
    with pytest.raises(RuntimeError) as exc:
        await get_vix_history(date(2026, 4, 1), date(2026, 4, 6))
    msg = str(exc.value)
    # The secret must not appear in the error message.
    assert "MYSECRETKEY" not in msg
    # Even though the body text mentions "api_key" literally, the
    # scrubber should have replaced the value (note: body's literal
    # "api_key" word without "=value" wouldn't be redacted, but the
    # URL portion definitely should be).
    assert "HTTP 400" in msg


@pytest.mark.asyncio
async def test_get_vix_history_passes_correct_params(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    seen = _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [{"date": "2026-04-01", "value": "18.0"}]
    }))
    await get_vix_history(date(2026, 4, 1), date(2026, 4, 6))
    assert len(seen) == 1
    url = seen[0].url
    # FRED endpoint
    assert url.path == "/fred/series/observations"
    # Required query params
    params = dict(url.params)
    assert params["series_id"] == "VIXCLS"
    assert params["file_type"] == "json"
    assert params["observation_start"] == "2026-04-01"
    assert params["observation_end"] == "2026-04-06"
    assert params["sort_order"] == "asc"
    assert params["api_key"] == "test-key"


# ---------------------------------------------------------------------------
# get_vix_eod
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_vix_eod_returns_most_recent_close(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [
            {"date": "2026-04-01", "value": "18.42"},
            {"date": "2026-04-02", "value": "19.10"},
            {"date": "2026-04-03", "value": "18.75"},
        ]
    }))
    val = await get_vix_eod(date(2026, 4, 3))
    assert val == pytest.approx(18.75)


@pytest.mark.asyncio
async def test_get_vix_eod_walks_back_over_weekend(monkeypatch):
    """Sunday lookup should return Friday's close.

    FRED returns the full window with sentinels for Sat/Sun; the loader
    skips sentinels and returns the most-recent real close.
    """
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [
            {"date": "2026-04-03", "value": "18.75"},  # Fri
            {"date": "2026-04-04", "value": "."},      # Sat
            {"date": "2026-04-05", "value": "."},      # Sun
        ]
    }))
    val = await get_vix_eod(date(2026, 4, 5))
    assert val == pytest.approx(18.75)


@pytest.mark.asyncio
async def test_get_vix_eod_returns_none_when_no_data(monkeypatch):
    """get_vix_eod returns None when get_vix_history fails internally?

    Actually: get_vix_history raises RuntimeError on empty/all-sentinel
    responses, so get_vix_eod would also raise. The function returns
    None only when df is empty AFTER successful fetch — which by
    construction can't happen. Document that contract here.
    """
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    _install_mock(monkeypatch, lambda req: httpx.Response(200, json={
        "observations": [{"date": "2026-04-04", "value": "."}],
    }))
    # All-sentinel raises from get_vix_history (not None from get_vix_eod).
    with pytest.raises(RuntimeError):
        await get_vix_eod(date(2026, 4, 4))
