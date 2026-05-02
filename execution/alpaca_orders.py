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
        client_order_id: str | None = None,
        time_in_force: str = "day",
    ) -> OrderResult:
        """Submit a bracket order: entry (limit) + stop-loss as OCO children.

        Why limit (not market) for the entry?
          Paper market orders fill at unrealistic prices in fast moves. A
          limit at the latest mid gives realistic fills and is what every
          serious intraday system uses. If unfilled by EOD it cancels.

        Args:
            ticker: equity symbol.
            side: "buy" or "sell".
            qty: positive integer share count.
            limit_price: entry limit price.
            stop_price: stop-loss trigger (already validated by risk module).
            client_order_id: optional dedup key. Auto-generated if None.
            time_in_force: "day" (default) | "gtc" | "ioc" | "fok".

        Returns:
            OrderResult with success flag and order_id (if accepted by Alpaca).
        """
        if client_order_id is None:
            client_order_id = f"{ticker}-{side}-{int(time.time() * 1000)}"

        # OTO (one-triggers-other): parent limit order with a single
        # stop_loss child leg. order_class=bracket would require BOTH
        # take_profit AND stop_loss; we only want stop_loss because exits
        # come from the next opposite signal or the 15:55 ET flatten.
        # Bug C fix 2026-04-30: was "bracket" before, which Alpaca rejects
        # with HTTP 422 "bracket orders require take_profit.limit_price".
        payload: dict[str, Any] = {
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
            )
        except Exception as e:
            logger.exception("Order submit failed for %s", ticker)
            return OrderResult(
                success=False, order_id=None, client_order_id=client_order_id,
                error=str(e), submitted_qty=qty, side=side,
                ticker=ticker, stop_price=stop_price,
            )

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    async def close_all_positions(self, cancel_orders: bool = True) -> dict[str, Any]:
        """Close every open position and cancel all open orders.

        Used by the 15:55 ET flatten routine. Alpaca exposes DELETE /v2/positions
        which closes everything in a single call - simpler and lower-latency
        than iterating positions ourselves.

        Args:
            cancel_orders: if True, also cancels all open orders before closing
                positions. Important to avoid bracket-order children firing
                after the parent is gone.

        Returns:
            Dict summarizing the operation: keys "cancelled_orders",
            "closed_positions", "errors". Each is a list.
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

            # Race fix 2026-05-02: Alpaca's DELETE /v2/orders accepts the cancel
            # request and returns immediately, but the cancellation propagates
            # through the order book over ~100-500ms. If DELETE /v2/positions
            # fires before that's done, Alpaca reports held_for_orders=qty and
            # rejects the close with HTTP 422. Wait briefly so the cancellation
            # settles before we ask Alpaca to close the position. Only sleep
            # if we actually canceled something.
            if result["cancelled_orders"]:
                await asyncio.sleep(0.5)

        try:
            closed = await self._delete("/v2/positions")
            ok, bad = _split_multistatus(closed, key="symbol")
            result["closed_positions"] = ok
            for f in bad:
                msg = (f"position close failed: symbol={f.get('symbol')} "
                       f"status={f.get('status')} body={f.get('body')}")
                logger.error(msg)
                result["errors"].append(msg)
            logger.info("Closed %d positions (failures=%d)", len(ok), len(bad))
        except Exception as e:
            logger.error("Close positions failed: %s", e)
            result["errors"].append(f"close_positions: {e}")

        return result

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
