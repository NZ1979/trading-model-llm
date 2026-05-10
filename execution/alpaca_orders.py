"""Alpaca paper trading REST client.

Wraps the four operations the platform needs:

  1. get_account_equity() - cached for 30s. Used by risk validator.
  2. get_open_positions() - real-time. Used by risk validator and flatten.
  3. submit_bracket_order() - entry + stop as a single OCO order.
  4. close_all_positions() - 15:55 ET flatten routine.

Why bracket orders?
  Alpaca's bracket order = parent (market/limit entry) + child stop + optional
  child take-profit, all submitted atomically. The stop sits on Alpaca's
  servers immediately, so a VPS crash between fill and stop placement can't
  leave us unprotected. The cost is that the stop is calculated from the
  signal-time price, not the actual fill price - in practice ES-tracking
  liquid names slip <1c on average so this isn't material.

Why a 30s equity cache?
  Account equity moves slowly intraday and the risk validator needs it on
  every signal evaluation (~30 names * every 5 mins = ~360 calls/hour).
  Fetching every time would burn rate limits unnecessarily. 30s staleness
  means a $100k account might be valued at $99.5k briefly; the position cap
  is approximate anyway so this is fine.

Why not async streaming order updates?
  Alpaca offers a trade_updates WebSocket for real-time fill notifications.
  We don't need it in Phase 6 because we're paper trading and the only
  fill-driven action is updating local position cache - which a 30s refresh
  handles. Adding the WebSocket would add complexity without value.
  Worth revisiting if/when we go live.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import json

import aiohttp

from strategy.risk import Position

logger = logging.getLogger(__name__)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

EQUITY_CACHE_TTL_SEC = 30.0


Side = Literal["buy", "sell"]


class AlpacaAPIError(Exception):
    """Raised when Alpaca returns a non-success HTTP status.

    Bug D fix 2026-05-02: includes the response body, which carries Alpaca's
    specific error code and message. Without this, a 422 from a bracket order
    surfaced only as 'HTTP 422: Unprocessable Entity' and the actual reason
    ('bracket orders require take_profit.limit_price') was lost. This class
    parses the JSON body if possible and includes the structured error.
    """

    def __init__(self, status: int, method: str, path: str, body_text: str):
        self.status = status
        self.method = method
        self.path = path
        self.body_text = body_text
        try:
            body_json = json.loads(body_text) if body_text else {}
            self.code = body_json.get("code")
            self.message = body_json.get("message", body_text[:200])
        except (json.JSONDecodeError, AttributeError):
            self.code = None
            self.message = body_text[:200]
        super().__init__(
            f"HTTP {status} {method} {path}: code={self.code} {self.message}"
        )


def _split_multistatus(items: Any, key: str) -> tuple[list, list]:
    """Split Alpaca's multi-status response into (successes, failures).

    Alpaca's bulk DELETE endpoints (/v2/orders, /v2/positions) return HTTP 207
    with a JSON array where each item has its own per-item status code:
        [{"symbol": "AAPL", "status": 200, "body": {...}},
         {"symbol": "TSLA", "status": 422, "body": {"message": "...", ...}}]

    Bug E fix 2026-05-02: previously we counted len(array) as successes,
    silently treating per-item failures as success. The 2026-05-01 flatten
    routine logged "closed=1" for a CLX position that actually had status=422
    (held_for_orders race with the just-canceled OTO child). Position stayed
    open, the user manually closed it 1+ hour later.

    Args:
        items: parsed JSON body from a 207 multi-status response. Expected
            to be a list of dicts. If not a list, returns ([], []).
        key: the per-item identifier field for log messages
            ("id" for orders, "symbol" for positions).

    Returns:
        (successes, failures) where each is a list of the per-item dicts.
        An item is success if its "status" field is 200 or 207.
    """
    if not isinstance(items, list):
        return [], []
    successes: list = []
    failures: list = []
    for item in items:
        if not isinstance(item, dict):
            failures.append({"status": "non_dict", "body": str(item)[:200]})
            continue
        status = item.get("status")
        # Alpaca returns 200 for successful per-item closes/cancels.
        # 207 sometimes appears at the wrapper level only; per-item is 200/422.
        if status == 200:
            successes.append(item)
        else:
            failures.append(item)
    return successes, failures


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Outcome of submit_bracket_order."""
    success: bool
    order_id: str | None
    client_order_id: str | None
    error: str | None
    submitted_qty: int
    side: Side
    ticker: str
    stop_price: float | None
    take_profit_price: float | None = None  # Layer 1 v2: TP target if bracket submitted; None for OTO-only orders


class AlpacaOrderClient:
    """Async REST client for Alpaca paper trading.

    Usage:
        client = AlpacaOrderClient(key, secret, paper=True)
        async with client:
            equity = await client.get_account_equity()
            positions = await client.get_open_positions()
            result = await client.submit_bracket_order(
                ticker="AAPL", side="buy", qty=50,
                limit_price=200.50, stop_price=196.49,
            )
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        paper: bool = True,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = PAPER_BASE_URL if paper else LIVE_BASE_URL
        self._session: aiohttp.ClientSession | None = None
        self._equity_cache: tuple[float, float] | None = None  # (equity, fetched_at)
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> AlpacaOrderClient:
        self._session = aiohttp.ClientSession(
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Account / positions
    # ------------------------------------------------------------------

    async def get_account_equity(self, force_refresh: bool = False) -> float:
        """Get account equity in dollars. Cached for 30s.

        Returns:
            Total equity (cash + position value). Returns 0.0 if API fails -
            the risk validator will then reject all orders, which is the
            safe failure mode.
        """
        async with self._lock:
            if not force_refresh and self._equity_cache is not None:
                equity, fetched_at = self._equity_cache
                if time.monotonic() - fetched_at < EQUITY_CACHE_TTL_SEC:
                    return equity

            try:
                data = await self._get("/v2/account")
                equity = float(data["equity"])
                self._equity_cache = (equity, time.monotonic())
                return equity
            except Exception as e:
                logger.error("get_account_equity failed: %s", e)
                # Stale cache is better than nothing
                if self._equity_cache is not None:
                    return self._equity_cache[0]
                return 0.0

    async def get_open_positions(self) -> list[Position]:
        """Fetch all open positions. Always fresh (no cache).

        Returns:
            List of Position dataclasses. Empty list on API error (the risk
            validator will then think account is flat - safe failure mode for
            new orders, NOT safe for the flatten routine which has its own
            error handling).
        """
        try:
            data = await self._get("/v2/positions")
            return [
                Position(
                    ticker=p["symbol"],
                    quantity=int(float(p["qty"])),  # negative for shorts
                    avg_price=float(p["avg_entry_price"]),
                    current_price=float(p["current_price"]),
                )
                for p in data
            ]
        except Exception as e:
            logger.error("get_open_positions failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def submit_bracket_order(
        self,
        ticker: str,
        side: Side,
        qty: int,
        limit_price: float,
        stop_price: float,
        take_profit_limit_price: float | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "day",
    ) -> OrderResult:
        """Submit a bracket order: entry (limit) + stop-loss + optional take-profit.

        Why limit (not market) for the entry?
          Paper market orders fill at unrealistic prices in fast moves. A
          limit at the latest mid gives realistic fills and is what every
          serious intraday system uses. If unfilled by EOD it cancels.

        Layer 1 of v2 profit-protection (see docs/LLM_MODEL_V2_REFINEMENTS.md
        § B.1): when take_profit_limit_price is supplied, the order is
        submitted as a full bracket (parent limit + stop_loss child + take_profit
        child). Alpaca holds both children server-side, so a fast move that
        prints at the TP price fills instantly with zero local latency. When
        take_profit_limit_price is None, behavior is identical to v1: an OTO
        order with stop_loss only (Bug C 2026-04-30 fix preserved).

        Args:
            ticker: equity symbol.
            side: "buy" or "sell".
            qty: positive integer share count.
            limit_price: entry limit price.
            stop_price: stop-loss trigger (already validated by risk module).
            take_profit_limit_price: optional take-profit target. For buy orders,
                must be strictly above limit_price; for sell orders, strictly
                below. Out-of-range values raise ValueError before submit so
                the operator gets a clear error rather than an opaque Alpaca 422.
            client_order_id: optional dedup key. Auto-generated if None.
            time_in_force: "day" (default) | "gtc" | "ioc" | "fok".

        Returns:
            OrderResult with success flag, order_id, and take_profit_price
            (None when no TP leg was attached).
        """
        if client_order_id is None:
            client_order_id = f"{ticker}-{side}-{int(time.time() * 1000)}"

        # Defensive validation of TP/limit relationship — fail fast on
        # caller bugs rather than getting an opaque 422 from Alpaca.
        if take_profit_limit_price is not None:
            if side == "buy" and take_profit_limit_price <= limit_price:
                raise ValueError(
                    f"take_profit_limit_price ${take_profit_limit_price:.2f} "
                    f"must be strictly above limit_price ${limit_price:.2f} "
                    f"for a buy bracket"
                )
            if side == "sell" and take_profit_limit_price >= limit_price:
                raise ValueError(
                    f"take_profit_limit_price ${take_profit_limit_price:.2f} "
                    f"must be strictly below limit_price ${limit_price:.2f} "
                    f"for a sell bracket"
                )

        # Build the order payload. Two shapes:
        #   - "oto" (no TP supplied): preserves v1 behavior exactly.
        #     order_class=bracket would require BOTH take_profit AND
        #     stop_loss; oto with a single stop_loss child is what
        #     Bug C 2026-04-30 fixed.
        #   - "bracket" (TP supplied): full bracket with both children
        #     held server-side. Activated by v2 Layer 1.
        if take_profit_limit_price is not None:
            payload: dict[str, Any] = {
                "symbol": ticker,
                "qty": str(qty),
                "side": side,
                "type": "limit",
                "limit_price": f"{limit_price:.2f}",
                "time_in_force": time_in_force,
                "order_class": "bracket",
                "take_profit": {
                    "limit_price": f"{take_profit_limit_price:.2f}",
                },
                "stop_loss": {
                    "stop_price": f"{stop_price:.2f}",
                },
                "client_order_id": client_order_id,
            }
        else:
            payload = {
                "symbol": ticker,
                "qty": str(qty),
                "side": side,
                "type": "limit",
                "limit_price": f"{limit_price:.2f}",
                "time_in_force": time_in_force,
                "order_class": "oto",
                "stop_loss": {
                    "stop_price": f"{stop_price:.2f}",
                },
                "client_order_id": client_order_id,
            }

        try:
            data = await self._post("/v2/orders", payload)
            return OrderResult(
                success=True,
                order_id=data.get("id"),
                client_order_id=data.get("client_order_id"),
                error=None,
                submitted_qty=qty,
                side=side,
                ticker=ticker,
                stop_price=stop_price,
                take_profit_price=take_profit_limit_price,
            )
        except AlpacaAPIError as e:
            # Bug D fix: AlpacaAPIError already includes the response body's
            # code+message, so we get the actual reason (e.g. "bracket orders
            # require take_profit.limit_price") instead of a bare status code.
            logger.error("Order rejected for %s: %s", ticker, e)
            return OrderResult(
                success=False, order_id=None, client_order_id=client_order_id,
                error=str(e), submitted_qty=qty, side=side,
                ticker=ticker, stop_price=stop_price,
                take_profit_price=take_profit_limit_price,
            )
        except Exception as e:
            logger.exception("Order submit failed for %s", ticker)
            return OrderResult(
                success=False, order_id=None, client_order_id=client_order_id,
                error=str(e), submitted_qty=qty, side=side,
                ticker=ticker, stop_price=stop_price,
                take_profit_price=take_profit_limit_price,
            )

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    async def close_all_positions(self, cancel_orders: bool = True) -> dict[str, Any]:
        """Close every open position and cancel all open orders.

        Used by the 15:55 ET flatten routine. Two-phase robustness against
        Alpaca's held_for_orders race:

          1. Cancel all pending orders, then poll /v2/positions until every
             position reports qty_available == qty (released from prior
             stop-leg holds). Timeout at 10s.
          2. Per-position close via DELETE /v2/positions/{symbol} with retry
             on HTTP 403 + Alpaca code 40310000 ("insufficient qty
             available"). Up to 5 retries with 1s backoff.

        Args:
            cancel_orders: if True, cancels all open orders before closing
                positions. Required to avoid bracket-order children firing
                after the parent is gone.

        Returns:
            Dict with keys "cancelled_orders", "closed_positions", "errors".
            Each is a list.
        """
        result: dict[str, Any] = {
            "cancelled_orders": [],
            "closed_positions": [],
            "errors": [],
        }

        if cancel_orders:
            try:
                cancelled = await self._delete("/v2/orders")
                ok, bad = _split_multistatus(cancelled, key="id")
                result["cancelled_orders"] = ok
                for f in bad:
                    msg = (f"order cancel failed: id={f.get('id')} "
                           f"status={f.get('status')} body={f.get('body')}")
                    logger.error(msg)
                    result["errors"].append(msg)
                logger.info("Cancelled %d open orders (failures=%d)",
                            len(ok), len(bad))
            except Exception as e:
                logger.error("Cancel orders failed: %s", e)
                result["errors"].append(f"cancel_orders: {e}")

            # Bug F fix 2026-05-05: replaces the prior 0.5s fixed sleep
            # (Bug E race fix) which was insufficient for OTO bracket-stop
            # children. After /v2/orders cancel, the held_for_orders side of
            # the position state can take several seconds to release on
            # Alpaca's side. Polling until released or timeout.
            if result["cancelled_orders"]:
                wait_result = await self._wait_for_release(max_wait_sec=10.0)
                logger.info(
                    "Wait for release: %d ready, %d still held, %.2fs elapsed",
                    len(wait_result["released"]),
                    len(wait_result["still_held"]),
                    wait_result["wait_sec"],
                )
                for p in wait_result["still_held"]:
                    logger.warning(
                        "Position still held after %ss wait: symbol=%s "
                        "qty=%s qty_available=%s (per-position retry will "
                        "attempt anyway)",
                        wait_result["wait_sec"], p.get("symbol"),
                        p.get("qty"), p.get("qty_available"),
                    )

        try:
            positions = await self._get("/v2/positions")
            if not positions:
                logger.info("No positions to close.")
                return result

            close_tasks = [
                self._close_position_with_retry(p["symbol"])
                for p in positions
            ]
            close_results = await asyncio.gather(
                *close_tasks, return_exceptions=False
            )

            for cr in close_results:
                if cr["ok"]:
                    result["closed_positions"].append(cr)
                else:
                    msg = (f"position close failed after {cr['retries']} "
                           f"retries: symbol={cr['symbol']} "
                           f"error={cr['error']}")
                    logger.error(msg)
                    result["errors"].append(msg)

            ok_count = sum(1 for cr in close_results if cr["ok"])
            logger.info("Closed %d positions (failures=%d)",
                        ok_count, len(close_results) - ok_count)
        except Exception as e:
            logger.error("Close positions failed: %s", e)
            result["errors"].append(f"close_positions: {e}")

        return result

    async def _wait_for_release(
        self, max_wait_sec: float = 10.0, poll_interval_sec: float = 0.5,
    ) -> dict[str, Any]:
        """Poll /v2/positions until qty_available equals qty for all positions.

        After canceling orders that hold position quantities, Alpaca takes
        100ms - several seconds to update the held_for_orders accounting.
        Without waiting for this, immediate close attempts return HTTP 403
        with code 40310000 ("insufficient qty available"). This is Bug F
        (2026-05-05), root cause of an end-of-day flatten failure.

        Args:
            max_wait_sec: total seconds to wait before giving up.
            poll_interval_sec: seconds between polls.

        Returns:
            Dict with keys "released" (positions where qty_available == qty),
            "still_held" (positions where qty_available < qty), and
            "wait_sec" (actual elapsed time).
        """
        start = time.monotonic()
        while True:
            try:
                positions = await self._get("/v2/positions")
            except Exception as e:
                logger.error("_wait_for_release poll failed: %s", e)
                return {"released": [], "still_held": [],
                        "wait_sec": time.monotonic() - start}

            still_held = [
                p for p in positions
                if int(float(p.get("qty_available", "0")))
                < int(float(p.get("qty", "0")))
            ]
            elapsed = time.monotonic() - start
            if not still_held:
                return {"released": positions, "still_held": [],
                        "wait_sec": elapsed}
            if elapsed >= max_wait_sec:
                return {"released": [p for p in positions if p not in still_held],
                        "still_held": still_held, "wait_sec": elapsed}
            await asyncio.sleep(poll_interval_sec)

    async def _close_position_with_retry(
        self, symbol: str, max_retries: int = 5, backoff_sec: float = 1.0,
    ) -> dict[str, Any]:
        """Close one position with retry on insufficient_qty errors.

        DELETE /v2/positions/{symbol} can return HTTP 403 + Alpaca code
        40310000 if held_for_orders is non-zero (Bug F race). Retry on
        that specific error class with a backoff. Other errors are
        non-retriable (e.g. 404 unknown symbol, 400 bad request).

        Args:
            symbol: equity symbol to close.
            max_retries: total attempts including the first. Default 5.
            backoff_sec: seconds between attempts. Default 1.0.

        Returns:
            Dict with keys "symbol", "ok" (bool), "retries" (int),
            "error" (str or None).
        """
        last_error: str | None = None
        for attempt in range(max_retries):
            try:
                await self._delete(f"/v2/positions/{symbol}")
                return {"symbol": symbol, "ok": True,
                        "retries": attempt, "error": None}
            except AlpacaAPIError as e:
                last_error = str(e)
                # Code 40310000 = "insufficient qty available", caused by
                # held_for_orders not yet released. This is the retriable case.
                is_retriable = (e.status == 403 and e.code == 40310000)
                if not is_retriable:
                    return {"symbol": symbol, "ok": False,
                            "retries": attempt, "error": last_error}
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff_sec)
            except Exception as e:
                return {"symbol": symbol, "ok": False,
                        "retries": attempt, "error": str(e)}
        return {"symbol": symbol, "ok": False,
                "retries": max_retries, "error": last_error}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel one order by ID. Returns True on success."""
        try:
            await self._delete(f"/v2/orders/{order_id}")
            return True
        except Exception as e:
            logger.error("Cancel order %s failed: %s", order_id, e)
            return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> Any:
        assert self._session is not None, "Use 'async with' context manager"
        url = f"{self.base_url}{path}"
        async with self._session.get(url) as resp:
            if not resp.ok:
                body = await resp.text()
                raise AlpacaAPIError(resp.status, "GET", path, body)
            return await resp.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        assert self._session is not None, "Use 'async with' context manager"
        url = f"{self.base_url}{path}"
        async with self._session.post(url, json=payload) as resp:
            if not resp.ok:
                body = await resp.text()
                raise AlpacaAPIError(resp.status, "POST", path, body)
            return await resp.json()

    async def _delete(self, path: str) -> Any:
        assert self._session is not None, "Use 'async with' context manager"
        url = f"{self.base_url}{path}"
        async with self._session.delete(url) as resp:
            # /v2/positions and /v2/orders DELETE returns a 207 multistatus
            # with a JSON body when there's anything to act on, or 204 empty.
            # 207 is "multi-status" — counted as ok here; per-item statuses are
            # checked by the caller (close_all_positions) per Bug E fix.
            if resp.status == 204:
                return []
            if not resp.ok and resp.status != 207:
                body = await resp.text()
                raise AlpacaAPIError(resp.status, "DELETE", path, body)
            return await resp.json()
