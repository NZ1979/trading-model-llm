"""Regression tests for Bug D + Bug E + Bug F fixes.

Bug D: AlpacaAPIError captures HTTP response body so 422 reasons are visible.
Bug E: _split_multistatus distinguishes per-item failures from successes.
Bug F: close_all_positions polls for held_for_orders release after cancel,
       and per-position close with retry on HTTP 403 + Alpaca code 40310000."""
import asyncio
import json
import sys

sys.path.insert(0, '.')

from execution.alpaca_orders import AlpacaAPIError, _split_multistatus, AlpacaOrderClient


# ---- Bug D ----

def test_alpaca_api_error_parses_json_body():
    body = json.dumps({"code": 40010001, "message": "bracket orders require take_profit.limit_price"})
    e = AlpacaAPIError(422, "POST", "/v2/orders", body)
    assert e.code == 40010001
    assert "code=40010001" in str(e) and "take_profit.limit_price" in str(e)
    return "AlpacaAPIError parses JSON body"


def test_alpaca_api_error_handles_non_json_body():
    e = AlpacaAPIError(503, "GET", "/v2/account", "<html>oops</html>")
    assert e.code is None and "HTTP 503" in str(e)
    return "non-JSON body handled"


def test_alpaca_api_error_handles_empty_body():
    e = AlpacaAPIError(500, "POST", "/v2/orders", "")
    assert e.code is None
    return "empty body handled"


# ---- Bug E ----

def test_split_multistatus_mixed():
    items = [
        {"symbol": "AAPL", "status": 200},
        {"symbol": "CLX", "status": 422, "body": {"message": "held_for_orders"}},
        {"symbol": "TSLA", "status": 200},
    ]
    ok, bad = _split_multistatus(items, key="symbol")
    assert len(ok) == 2 and len(bad) == 1 and bad[0]["symbol"] == "CLX"
    return "2 success + 1 fail split"


def test_split_multistatus_edge_cases():
    assert _split_multistatus([], key="symbol") == ([], [])
    assert _split_multistatus(None, key="symbol") == ([], [])
    ok, bad = _split_multistatus(["str", {"symbol": "X", "status": 200}], key="symbol")
    assert len(ok) == 1 and len(bad) == 1
    return "edge cases (empty/non-list/non-dict-items) handled"


# ---- Bug F: close_all_positions ----

async def test_full_success_first_try():
    """Cancel succeeds, position immediately released, closes on first try."""
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)

    async def fake_get(path):
        return [{"symbol": "AAPL", "qty": "10", "qty_available": "10"}]

    async def fake_delete(path):
        if path == "/v2/orders":
            return [{"id": "o1", "status": 200}]
        if path == "/v2/positions/AAPL":
            return {"order_id": "close1"}
        raise AssertionError(f"unexpected: {path}")

    client._get = fake_get
    client._delete = fake_delete
    r = await client.close_all_positions(cancel_orders=True)
    assert len(r["cancelled_orders"]) == 1
    assert len(r["closed_positions"]) == 1 and r["closed_positions"][0]["retries"] == 0
    assert r["errors"] == []
    return "full success on first try"


async def test_empty_account():
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    async def fake_get(path):
        return []
    async def fake_delete(path):
        return []
    client._get, client._delete = fake_get, fake_delete
    r = await client.close_all_positions(cancel_orders=True)
    assert r["cancelled_orders"] == [] and r["closed_positions"] == [] and r["errors"] == []
    return "empty account: no-op clean"


async def test_retry_on_insufficient_qty_succeeds():
    """Bug F core: first close attempt 403s with code 40310000, retry wins."""
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    delete_attempts = {"n": 0}
    err = AlpacaAPIError(
        403, "DELETE", "/v2/positions/TDG",
        json.dumps({"code": 40310000, "message": "insufficient qty"})
    )

    async def fake_get(path):
        return [{"symbol": "TDG", "qty": "16", "qty_available": "16"}]

    async def fake_delete(path):
        if path == "/v2/orders":
            return [{"id": "stop1", "status": 200}]
        if path == "/v2/positions/TDG":
            delete_attempts["n"] += 1
            if delete_attempts["n"] == 1:
                raise err
            return {"order_id": "tdg-close"}
        raise AssertionError(f"unexpected: {path}")

    client._get, client._delete = fake_get, fake_delete

    # Speed up retry backoff
    import execution.alpaca_orders as ao
    orig = ao.AlpacaOrderClient._close_position_with_retry
    async def fast(self, symbol, max_retries=5, backoff_sec=0.005):
        return await orig(self, symbol, max_retries=max_retries, backoff_sec=backoff_sec)
    ao.AlpacaOrderClient._close_position_with_retry = fast
    try:
        r = await client.close_all_positions(cancel_orders=True)
    finally:
        ao.AlpacaOrderClient._close_position_with_retry = orig

    assert len(r["closed_positions"]) == 1
    assert r["closed_positions"][0]["retries"] == 1, f"expected 1 retry, got {r['closed_positions'][0]['retries']}"
    assert r["errors"] == []
    assert delete_attempts["n"] == 2, f"expected 2 attempts, got {delete_attempts['n']}"
    return "403 + 40310000 triggers retry, 2nd attempt succeeds"


async def test_retry_exhausts_to_failure():
    """If position never releases, retries exhaust and report failure."""
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    err = AlpacaAPIError(
        403, "DELETE", "/v2/positions/STUCK",
        json.dumps({"code": 40310000, "message": "insufficient qty"})
    )

    async def always_fail(path):
        raise err
    client._delete = always_fail
    cr = await client._close_position_with_retry("STUCK", max_retries=3, backoff_sec=0.005)
    assert cr["ok"] is False
    assert cr["retries"] == 3
    assert "40310000" in cr["error"]
    return "retries exhausted, failure reported with error"


async def test_non_retriable_error_no_retry():
    """404 (or any non-403/40310000) is not retried."""
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    err = AlpacaAPIError(404, "DELETE", "/v2/positions/X", "")
    n = {"calls": 0}

    async def one_404(path):
        n["calls"] += 1
        raise err
    client._delete = one_404
    cr = await client._close_position_with_retry("X", max_retries=5, backoff_sec=0.005)
    assert cr["ok"] is False and cr["retries"] == 0 and n["calls"] == 1
    return "404 does not trigger retry"


async def test_wait_for_release_exits_when_ready():
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    poll = {"n": 0}
    async def fake_get(path):
        poll["n"] += 1
        if poll["n"] == 1:
            return [{"symbol": "X", "qty": "10", "qty_available": "0"}]
        return [{"symbol": "X", "qty": "10", "qty_available": "10"}]
    client._get = fake_get
    wr = await client._wait_for_release(max_wait_sec=5.0, poll_interval_sec=0.005)
    assert poll["n"] >= 2 and wr["still_held"] == [] and wr["wait_sec"] < 1.0
    return f"poll loop exits when released ({poll['n']} polls)"


async def test_wait_for_release_times_out():
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    async def held(path):
        return [{"symbol": "S", "qty": "10", "qty_available": "0"}]
    client._get = held
    wr = await client._wait_for_release(max_wait_sec=0.05, poll_interval_sec=0.01)
    assert len(wr["still_held"]) == 1 and wr["wait_sec"] >= 0.05 and wr["wait_sec"] < 0.5
    return "poll loop times out cleanly"


async def test_partial_failure_per_position():
    """One position releases, one stuck — first reported as closed,
    second reported in errors after retries."""
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    err = AlpacaAPIError(
        403, "DELETE", "/v2/positions/STUCK",
        json.dumps({"code": 40310000, "message": "insufficient qty"})
    )

    async def fake_get(path):
        return [
            {"symbol": "OK", "qty": "10", "qty_available": "10"},
            {"symbol": "STUCK", "qty": "10", "qty_available": "0"},
        ]

    async def fake_delete(path):
        if path == "/v2/orders":
            return [{"id": "o1", "status": 200}]
        if path == "/v2/positions/OK":
            return {"order_id": "ok-close"}
        if path == "/v2/positions/STUCK":
            raise err
        raise AssertionError(f"unexpected: {path}")

    client._get, client._delete = fake_get, fake_delete

    import execution.alpaca_orders as ao
    orig = ao.AlpacaOrderClient._close_position_with_retry
    async def fast(self, symbol, max_retries=2, backoff_sec=0.005):
        return await orig(self, symbol, max_retries=max_retries, backoff_sec=backoff_sec)
    ao.AlpacaOrderClient._close_position_with_retry = fast
    try:
        r = await client.close_all_positions(cancel_orders=True)
    finally:
        ao.AlpacaOrderClient._close_position_with_retry = orig

    closed = [c["symbol"] for c in r["closed_positions"]]
    assert "OK" in closed and "STUCK" not in closed
    assert len(r["errors"]) == 1 and "STUCK" in r["errors"][0]
    return "partial: OK closed, STUCK in errors"


async def test_no_wait_when_nothing_canceled():
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    get_calls = []
    async def fake_get(path):
        get_calls.append(path)
        return []
    async def fake_delete(path):
        if path == "/v2/orders":
            return []
        return {}
    client._get, client._delete = fake_get, fake_delete
    await client.close_all_positions(cancel_orders=True)
    assert len(get_calls) == 1, f"expected 1 _get, got {len(get_calls)}"
    return "no wait_for_release when no orders canceled"


async def test_submit_bracket_order_catches_api_error():
    from execution.alpaca_orders import OrderResult
    client = AlpacaOrderClient(api_key="k", api_secret="s", paper=True)
    async def fake_post(path, payload):
        raise AlpacaAPIError(
            422, "POST", "/v2/orders",
            json.dumps({"code": 40010001, "message": "bracket orders require take_profit.limit_price"})
        )
    client._post = fake_post
    r = await client.submit_bracket_order(
        ticker="MO", side="buy", qty=1, limit_price=40.0, stop_price=39.20,
    )
    assert isinstance(r, OrderResult) and r.success is False
    assert "40010001" in r.error
    return "bracket_order surfaces structured error"


def main():
    sync = [
        test_alpaca_api_error_parses_json_body,
        test_alpaca_api_error_handles_non_json_body,
        test_alpaca_api_error_handles_empty_body,
        test_split_multistatus_mixed,
        test_split_multistatus_edge_cases,
    ]
    asyncs = [
        test_full_success_first_try,
        test_empty_account,
        test_retry_on_insufficient_qty_succeeds,
        test_retry_exhausts_to_failure,
        test_non_retriable_error_no_retry,
        test_wait_for_release_exits_when_ready,
        test_wait_for_release_times_out,
        test_partial_failure_per_position,
        test_no_wait_when_nothing_canceled,
        test_submit_bracket_order_catches_api_error,
    ]
    results = []
    for t in sync:
        try: results.append(("PASS", t.__name__, t()))
        except AssertionError as e: results.append(("FAIL", t.__name__, str(e)))
        except Exception as e: results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))
    for t in asyncs:
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
