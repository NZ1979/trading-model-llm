"""Phase 0 — Verify Finnhub endpoint availability on the Fundamental-1 plan.

Run from Windows PowerShell after setting $env:FINNHUB_API_KEY.
Tests the 12 highest-priority endpoints for gap-and-go strategy fit.
Prints HTTP status, response shape preview, and PASS/FAIL per endpoint.

Usage (PowerShell, Windows):
    $env:FINNHUB_API_KEY = "your_key_here"
    python scripts/test_finnhub_endpoints.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KEY = os.environ.get("FINNHUB_API_KEY")
if not KEY:
    print("FAIL: FINNHUB_API_KEY env var not set.")
    print("In PowerShell: $env:FINNHUB_API_KEY = 'your_key_here'")
    sys.exit(1)

BASE = "https://finnhub.io/api/v1"
TICKER = "AAPL"
TODAY = date.today()
WEEK_AGO = (TODAY - timedelta(days=7)).isoformat()
MONTH_AGO = (TODAY - timedelta(days=30)).isoformat()
TODAY_STR = TODAY.isoformat()
TWO_WEEKS_FWD = (TODAY + timedelta(days=14)).isoformat()


def call(path: str, params: dict | None = None) -> tuple[int, object]:
    """Make one Finnhub API call. Returns (status_code, parsed_body_or_error)."""
    p = dict(params or {})
    p["token"] = KEY
    url = f"{BASE}{path}?{urlencode(p)}"
    try:
        req = Request(url, headers={"User-Agent": "trader-finnhub-test/1.0"})
        with urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = body[:200]
            return resp.status, data
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = body[:200]
        except Exception:
            data = {"error": "could not read body"}
        return e.code, data
    except URLError as e:
        return -1, {"error": f"network: {e}"}
    except Exception as e:
        return -1, {"error": f"unexpected: {type(e).__name__}: {e}"}


# (rank, name, path, params)
TESTS = [
    (1, "Earnings Calendar", "/calendar/earnings",
     {"from": TODAY_STR, "to": TWO_WEEKS_FWD}),
    (2, "Major Press Releases", "/press-releases",
     {"symbol": TICKER, "from": MONTH_AGO, "to": TODAY_STR}),
    (3, "Company News", "/company-news",
     {"symbol": TICKER, "from": WEEK_AGO, "to": TODAY_STR}),
    (4, "News Sentiment", "/news-sentiment",
     {"symbol": TICKER}),
    (5, "Recommendation Trends", "/stock/recommendation",
     {"symbol": TICKER}),
    (6, "Stock Upgrade/Downgrade", "/stock/upgrade-downgrade",
     {"symbol": TICKER}),
    (7, "Newsroom", "/stock/newsroom",
     {"symbol": TICKER}),
    (8, "Insider Transactions", "/stock/insider-transactions",
     {"symbol": TICKER, "from": MONTH_AGO, "to": TODAY_STR}),
    (9, "Social Sentiment", "/stock/social-sentiment",
     {"symbol": TICKER, "from": WEEK_AGO}),
    (10, "Basic Financials", "/stock/metric",
     {"symbol": TICKER, "metric": "all"}),
    (11, "Investment Themes", "/stock/investment-theme",
     {"theme": "futureFood"}),
    (12, "FDA Committee Calendar", "/fda-advisory-committee-calendar",
     {}),
]


def classify(status: int, data: object) -> tuple[str, str]:
    """Return (verdict, short_preview)."""
    preview = json.dumps(data, default=str)[:140].replace("\n", " ")
    if status == 200:
        if isinstance(data, dict) and not data:
            verdict = "PASS (empty dict)"
        elif isinstance(data, list) and not data:
            verdict = "PASS (empty list)"
        elif isinstance(data, dict) and "error" in data:
            verdict = f"FAIL - 200 with error: {data.get('error')}"
        else:
            verdict = "PASS"
        return verdict, preview
    if status == 401:
        return "FAIL - 401 unauthorized (bad API key)", preview
    if status == 403:
        return "FAIL - 403 forbidden (NOT on plan)", preview
    if status == 429:
        return "RATE LIMITED (try again later)", preview
    if status == -1:
        return "FAIL - network error", preview
    return f"FAIL - HTTP {status}", preview


def main() -> int:
    print(f"Testing 12 Finnhub endpoints with API key ending '...{KEY[-4:]}'")
    print(f"Date: {TODAY_STR}, ticker: {TICKER}\n")
    results = []
    for rank, name, path, params in TESTS:
        status, data = call(path, params)
        verdict, preview = classify(status, data)
        results.append((rank, name, status, verdict, preview))
        print(f"  #{rank:2}  [{verdict:42}]  {name:24}  HTTP {status}")
        print(f"        preview: {preview}")
        time.sleep(0.25)  # well under 30/sec global cap

    print()
    n_pass = sum(1 for _, _, s, v, _ in results if v.startswith("PASS"))
    n_fail = sum(1 for _, _, _, v, _ in results if v.startswith("FAIL"))
    n_rate = sum(1 for _, _, _, v, _ in results if v.startswith("RATE"))
    print(f"Summary: {n_pass}/{len(results)} PASS, {n_fail} FAIL, {n_rate} RATE LIMITED")
    print()
    print("=" * 60)
    print("AVAILABLE on this plan:")
    for rank, name, _, v, _ in results:
        if v.startswith("PASS"):
            print(f"  #{rank:2}  {name}")
    print()
    print("NOT on this plan (403):")
    forbidden = [(r, n) for r, n, s, _, _ in results if s == 403]
    if forbidden:
        for r, n in forbidden:
            print(f"  #{r:2}  {n}")
    else:
        print("  (none)")
    print()
    print("Other failures:")
    other = [(r, n, v) for r, n, s, v, _ in results
             if v.startswith("FAIL") and s != 403]
    if other:
        for r, n, v in other:
            print(f"  #{r:2}  {n}: {v}")
    else:
        print("  (none)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
