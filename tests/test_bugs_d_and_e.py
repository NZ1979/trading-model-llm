"""Regression tests for Bug D + Bug E fixes.

Bug D: AlpacaAPIError captures HTTP response body so 422 reasons are visible.
Bug E: _split_multistatus + close_all_positions distinguish per-item failures
from successes in Alpaca's 207 multi-status responses."""
import asyncio
import json
import sys
from unittest import mock

sys.path.insert(0, '.')

from execution.alpaca_orders import AlpacaAPIError, _split_multistatus, AlpacaOrderClient


# -------------------- Bug D: AlpacaAPIError --------------------

def test_alpaca_api_error_parses_json_body():
    body = json.dumps({"code": 40010001, "message": "bracket orders require take_profit.limit_price"})
    e = AlpacaAPIError(422, "POST", "/v2/orders", body)
    assert e.status == 422
    assert e.code == 40010001
    assert "take_profit.limit_price" in e.message
    assert "code=40010001" in str(e)
    assert "take_profit.limit_price" in str(e)
    return f"AlpacaAPIError parsed JSON: code={e.code}"


def test_alpaca_api_error_handles_non_json_body():
    e = AlpacaAPIError(503, "GET", "/v2/account", "<html>service unavailable</html>")
    assert e.code is None
    assert "service unavailable" in e.message
    assert "HTTP 503" in str(e)
    return "non-JSON body falls back to truncated text"


def test_alpaca_api_error_handles_empty_body():
    e = AlpacaAPIError(500, "POST", "/v2/orders", "")
    assert e.code is None
    assert e.message == "" or len(e.message) <= 200
    return "empty body handled without crash"


# -------------------- Bug E: _split_multistatus --------------------

def test_split_multistatus_all_success():
    items = [
        {"symbol": "AAPL", "status": 200, "body": {"id": "x"}},
        {"symbol": "TSLA", "status": 200, "body": {"id": "y"}},
    ]
    ok, bad = _split_multistatus(items, key="symbol")
    assert len(ok) == 2 and len(bad) == 0
    return "2/2 successes counted correctly"


def test_split_multistatus_all_failure():
    items = [
        {"symbol": "CLX", "status": 422, "body": {"code": 40310000, "message": "insufficient qty available"}},
    ]
    ok, bad = _split_multistatus(items, key="symbol")
    assert len(ok) == 0 and len(bad) == 1
    assert bad[0].get("status") == 422
    return f"1/1 failure flagged: {bad[0]['body']['message']}"


def test_split_multistatus_mixed():
    items = [
        {"symbol": "AAPL", "status": 200, "body": {"id": "x"}},
        {"symbol": "CLX", "status": 422, "body": {"message": "held_for_orders"}},
        {"symbol": "TSLA", "status": 200, "body": {"id": "z"}},
    ]
    ok, bad = _split_multistatus(items, key="symbol")
    assert len(ok) == 2 and len(bad) == 1
    assert bad[0]["symbol"] == "CLX"
    return "2 success + 1 fail correctly split"


def test_split_multistatus_empty_list():
    ok, bad = _split_multistatus([], key="symbol")
    assert ok == [] and bad == []
    return "empty list handled"


def test_split_multistatus_non_list():
    ok, bad = _split_multistatus(None, key="symbol")
    assert ok == [] and bad == []
    return "non-list input returns empty pair"


def test_split_multistatus_non_dict_item():
    items = ["unexpected_string", {"symbol": "AAPL", "status": 200}]
    ok, bad = _split_multistatus(items, key="symbol")
    assert len(ok) == 1 and len(bad) == 1
    return "non-dict items go to failures"


# -------------------- Bug E: close_all_positions integration --------------------

async def test_close_all_positions_with_partial_failure():
    """The actual production scenario from 2026-05-01:
    DELETE /v2/orders succeeds for the OTO child cancel, but
    DELETE /v2/positions returns 207 with status=422 for CLX
    (held_for_orders race)."""
    client = AlpacaOrderClient(api_key="fake", api_secret="fake", paper=True)

    # Mock _delete to return realistic responses
    cancelled_response = [
        {"id": "child-stop-id", "status": 200, "body": {"id": "child-stop-id"}}
    ]
    closed_response = [
        {"symbol": "CLX", "status": 422,
         "body": {"available": "0", "code": 40310000,
                  "existing_qty": "225", "held_for_orders": "225",
                  "message": "insufficient qty available for order (existing_qty=225 held_for_orders=225)",
                  "symbol": "CLX"}}
    ]
    call_count = {"orders": 0, "positions": 0}

    async def fake_delete(path):
        if path == "/v2/orders":
            call_count["orders"] += 1
            return cancelled_response
        elif path == "/v2/positions":
            call_count["positions"] += 1
            return closed_response
        raise AssertionError(f"unexpected path: {path}")

    client._delete = fake_delete

    result = await client.close_all_positions(cancel_orders=True)

    assert call_count["orders"] == 1, "should call DELETE /v2/orders once"
    assert call_count["positions"] == 1, "should call DELETE /v2/positions once"
    assert len(result["cancelled_orders"]) == 1, "1 cancel succeeded"
    assert len(result["closed_positions"]) == 0, "0 positions actually closed"
    assert len(result["errors"]) == 1, "1 error logged for the failed close"
    err = result["errors"][0]
    assert "CLX" in err and "422" in err and "held_for_orders" in err
    return f"partial failure correctly reported: {err[:80]}..."


async def test_close_all_positions_full_success():
    client = AlpacaOrderClient(api_key="fake", api_secret="fake", paper=True)
    async def fake_delete(path):
        if path == "/v2/orders":
            return [{"id": "o1", "status": 200, "body": {}}]
        return [{"symbol": "AAPL", "status": 200, "body": {}}]
    client._delete = fake_delete
    result = await client.close_all_positions(cancel_orders=True)
    assert len(result["cancelled_orders"]) == 1
    assert len(result["closed_positions"]) == 1
    assert len(result["errors"]) == 0
    return "full success path: 1 cancel + 1 close, no errors"


async def test_close_all_positions_empty():
    client = AlpacaOrderClient(api_key="fake", api_secret="fake", paper=True)
    async def fake_delete(path):
        return []  # 204 case: nothing to close/cancel
    client._delete = fake_delete
    result = await client.close_all_positions(cancel_orders=True)
    assert result == {"cancelled_orders": [], "closed_positions": [], "errors": []}
    return "empty multi-status (nothing to do) handled"


def main():
    tests_sync = [
        test_alpaca_api_error_parses_json_body,
        test_alpaca_api_error_handles_non_json_body,
        test_alpaca_api_error_handles_empty_body,
        test_split_multistatus_all_success,
        test_split_multistatus_all_failure,
        test_split_multistatus_mixed,
        test_split_multistatus_empty_list,
        test_split_multistatus_non_list,
        test_split_multistatus_non_dict_item,
    ]
    tests_async = [
        test_close_all_positions_with_partial_failure,
        test_close_all_positions_full_success,
        test_close_all_positions_empty,
    ]
    results = []
    for t in tests_sync:
        try:
            r = t()
            results.append(("PASS", t.__name__, r))
        except AssertionError as e:
            results.append(("FAIL", t.__name__, str(e)))
        except Exception as e:
            results.append(("ERROR", t.__name__, f"{type(e).__name__}: {e}"))

    for t in tests_async:
        try:
            r = asyncio.run(t())
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

# Append: verify submit_bracket_order catches AlpacaAPIError (Bug D rewire)
async def test_submit_bracket_order_catches_alpaca_api_error():
    """When _post raises AlpacaAPIError, submit_bracket_order should
    return success=False with the structured error string (not a traceback)."""
    from execution.alpaca_orders import AlpacaOrderClient, AlpacaAPIError, OrderResult
    client = AlpacaOrderClient(api_key="fake", api_secret="fake", paper=True)

    async def fake_post(path, payload):
        raise AlpacaAPIError(
            422, "POST", "/v2/orders",
            json.dumps({"code": 40010001,
                        "message": "bracket orders require take_profit.limit_price"})
        )
    client._post = fake_post
    result = await client.submit_bracket_order(
        ticker="MO", side="buy", qty=1, limit_price=40.0, stop_price=39.20,
    )
    assert isinstance(result, OrderResult)
    assert result.success is False
    assert "40010001" in result.error
    assert "take_profit.limit_price" in result.error
    return f"submit_bracket_order surfaces structured error: {result.error[:80]}"


# re-run including the new test
print("\n=== Additional test: submit_bracket_order catches AlpacaAPIError ===")
try:
    r = asyncio.run(test_submit_bracket_order_catches_alpaca_api_error())
    print(f"PASS  {r}")
except AssertionError as e:
    print(f"FAIL  {e}")
except Exception as e:
    print(f"ERROR  {type(e).__name__}: {e}")
