"""Binance spot **testnet** adapter (CLAUDE.md §9).

The only broker implementation in v1. Spot only: market and limit orders, no
margin, no leverage, no shorting.

Two things are enforced structurally rather than by care:

* **The base URL is a constant.** There is no configuration switch that points
  this class at production. Trading live requires editing code and passing
  review, not flipping an environment variable at 2am.
* **No broker exception escapes.** Every failure path maps to `BrokerError` with
  an internal code. Binance error text echoes the request that caused it — API
  key included — so letting one propagate would put a credential into whatever
  log or HTTP response caught it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from signalguard.enums import OrderSide, OrderStatus, OrderType
from signalguard.execution.base import (
    AccountState,
    BrokerAdapter,
    BrokerError,
    BrokerErrorCode,
    BrokerOrder,
    BrokerPosition,
    Fill,
    Instrument,
    OrderRequest,
)

logger = logging.getLogger(__name__)

# Hardcoded, not configurable. See the module docstring.
TESTNET_BASE_URL = "https://testnet.binance.vision"

REQUEST_TIMEOUT_SEC = 10.0
MAX_RETRIES = 3
QUOTE_ASSET = "USDT"

# Binance order status -> ours.
_STATUS_MAP = {
    "NEW": OrderStatus.SUBMITTED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "PENDING_CANCEL": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.CANCELLED,
}

# Binance error codes -> ours. Anything unmapped becomes UNKNOWN, which is not
# retryable — an error we do not understand is not one to repeat.
_ERROR_MAP = {
    -1021: BrokerErrorCode.INVALID_ORDER,        # timestamp outside recvWindow
    -1022: BrokerErrorCode.AUTH_FAILED,          # bad signature
    -2010: BrokerErrorCode.INSUFFICIENT_BALANCE,
    -2011: BrokerErrorCode.UNKNOWN_ORDER,
    -2013: BrokerErrorCode.UNKNOWN_ORDER,
    -2014: BrokerErrorCode.AUTH_FAILED,
    -2015: BrokerErrorCode.AUTH_FAILED,
    -1003: BrokerErrorCode.RATE_LIMITED,
    -1013: BrokerErrorCode.INVALID_ORDER,        # filter failure
    -1121: BrokerErrorCode.INVALID_ORDER,        # bad symbol
}


def _decimal(value: Any) -> Decimal:
    """Convert a broker string to Decimal without ever touching a float."""
    return Decimal(str(value))


class BinanceTestnetAdapter(BrokerAdapter):
    """Binance spot testnet."""

    def __init__(self, api_key: str, api_secret: str, *, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = client or httpx.AsyncClient(
            base_url=TESTNET_BASE_URL, timeout=REQUEST_TIMEOUT_SEC
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Plumbing -------------------------------------------------------------

    def _sign(self, params: dict[str, Any]) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return hmac.new(
            self._api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
        idempotent: bool = True,
    ) -> Any:
        """Issue a request, mapping every failure into BrokerError.

        Retries apply **only when `idempotent`** — reads and cancels. An order
        submission is never blindly retried; its safety comes from the
        client-supplied order ID plus an explicit lookup of what happened.
        """
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            params["signature"] = self._sign(params)

        headers = {"X-MBX-APIKEY": self._api_key} if signed else {}
        attempts = MAX_RETRIES if idempotent else 1
        last_error: BrokerError | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, path, params=params, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = BrokerError(
                    BrokerErrorCode.TIMEOUT, "request timed out", retryable=True
                )
                if not idempotent:
                    raise last_error from exc
            except httpx.HTTPError as exc:
                last_error = BrokerError(
                    BrokerErrorCode.CONNECTION_FAILED, "connection failed", retryable=True
                )
                if not idempotent:
                    raise last_error from exc
            else:
                if response.status_code < 400:
                    return response.json()
                raise self._map_error(response)

            # Exponential backoff: 0.5s, 1s, 2s.
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (2**attempt))

        raise last_error or BrokerError(BrokerErrorCode.UNKNOWN)

    def _map_error(self, response: httpx.Response) -> BrokerError:
        """Map a broker error response onto our enum, discarding its text."""
        try:
            payload = response.json()
            code = int(payload.get("code", 0))
        except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
            code = 0

        if response.status_code == 429:
            return BrokerError(BrokerErrorCode.RATE_LIMITED, retryable=True)
        if response.status_code in (401, 403):
            return BrokerError(BrokerErrorCode.AUTH_FAILED)
        if response.status_code >= 500:
            return BrokerError(BrokerErrorCode.CONNECTION_FAILED, retryable=True)

        mapped = _ERROR_MAP.get(code, BrokerErrorCode.UNKNOWN)
        # Deliberately no broker message: it echoes the signed request.
        logger.warning(
            "Broker error",
            extra={"http_status": response.status_code, "broker_code": code,
                   "mapped": mapped.value},
        )
        return BrokerError(mapped)

    # --- Interface ------------------------------------------------------------

    async def get_account_state(self) -> AccountState:
        data = await self._request("GET", "/api/v3/account")

        free_balance = Decimal("0")
        positions: list[BrokerPosition] = []
        position_value = Decimal("0")

        for balance in data.get("balances", []):
            asset = balance["asset"]
            total = _decimal(balance["free"]) + _decimal(balance["locked"])
            if asset == QUOTE_ASSET:
                free_balance = _decimal(balance["free"])
                continue
            if total <= 0:
                continue

            # On spot, a non-zero base balance IS the long position (plan §2, A2).
            symbol = f"{asset}{QUOTE_ASSET}"
            try:
                mark = await self._last_price(symbol)
            except BrokerError:
                continue  # not a tradeable pair; ignore it rather than fail

            positions.append(
                BrokerPosition(
                    symbol=symbol, qty=total, avg_entry=mark, mark_price=mark
                )
            )
            position_value += total * mark

        return AccountState(
            total_equity=free_balance + position_value,
            free_balance=free_balance,
            position_value=position_value,
            positions=tuple(positions),
        )

    async def _last_price(self, symbol: str) -> Decimal:
        data = await self._request(
            "GET", "/api/v3/ticker/price", {"symbol": symbol}, signed=False
        )
        return _decimal(data["price"])

    async def get_reference_price(self, symbol: str) -> Decimal:
        """Current price, used to size market orders (plan §2, A8)."""
        return await self._last_price(symbol)

    async def get_instrument(self, symbol: str) -> Instrument:
        data = await self._request(
            "GET", "/api/v3/exchangeInfo", {"symbol": symbol}, signed=False
        )
        symbols = data.get("symbols", [])
        if not symbols:
            raise BrokerError(BrokerErrorCode.INVALID_ORDER, "unknown symbol")
        return self._parse_instrument(symbols[0])

    async def list_instruments(self) -> tuple[Instrument, ...]:
        """Fetched at startup and cached — never hardcoded (CLAUDE.md §7)."""
        data = await self._request("GET", "/api/v3/exchangeInfo", signed=False)
        return tuple(
            self._parse_instrument(item)
            for item in data.get("symbols", [])
            if item.get("quoteAsset") == QUOTE_ASSET
        )

    def _parse_instrument(self, item: dict[str, Any]) -> Instrument:
        filters = {f["filterType"]: f for f in item.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        return Instrument(
            symbol=item["symbol"],
            base_asset=item["baseAsset"],
            quote_asset=item["quoteAsset"],
            tick_size=_decimal(price_filter.get("tickSize", "0.01")),
            lot_step=_decimal(lot.get("stepSize", "0.00000001")),
            min_qty=_decimal(lot.get("minQty", "0")),
            min_notional=_decimal(notional.get("minNotional", "0")),
            status=item.get("status", "UNKNOWN"),
        )

    async def submit_order(self, order: OrderRequest) -> BrokerOrder:
        """Place an order. Never retried blindly — see `_request`."""
        params: dict[str, Any] = {
            "symbol": order.symbol,
            "side": order.side.value,
            "newClientOrderId": order.client_order_id,
            "quantity": format(order.qty, "f"),
        }

        if order.stop_price is not None:
            # Binance spot names this STOP_LOSS; it triggers a market exit.
            params["type"] = "STOP_LOSS"
            params["stopPrice"] = format(order.stop_price, "f")
        elif order.order_type is OrderType.LIMIT:
            if order.price is None:
                raise BrokerError(BrokerErrorCode.INVALID_ORDER, "limit order needs a price")
            params["type"] = "LIMIT"
            params["timeInForce"] = "GTC"
            params["price"] = format(order.price, "f")
        else:
            params["type"] = "MARKET"

        data = await self._request(
            "POST", "/api/v3/order", params, idempotent=False
        )
        return self._parse_order(data, order)

    async def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder:
        """Look an order up by OUR id — the key to recovering a lost response."""
        data = await self._request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return self._parse_order(data)

    async def cancel_order(self, broker_order_id: str, symbol: str) -> None:
        # Idempotent: cancelling an already-cancelled order is not an error.
        await self._request(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": broker_order_id}
        )

    async def cancel_all_orders(self) -> int:
        open_orders = await self._request("GET", "/api/v3/openOrders")
        cancelled = 0
        for item in open_orders:
            try:
                await self.cancel_order(str(item["orderId"]), item["symbol"])
                cancelled += 1
            except BrokerError as exc:
                if exc.code is BrokerErrorCode.UNKNOWN_ORDER:
                    cancelled += 1  # already gone: the desired end state
                else:
                    raise
        return cancelled

    async def close_all_positions(self) -> list[BrokerOrder]:
        """Market-sell every base asset back to the quote currency."""
        from signalguard.execution.orders import new_client_order_id

        state = await self.get_account_state()
        closed: list[BrokerOrder] = []
        for position in state.positions:
            if position.qty <= 0:
                continue
            instrument = await self.get_instrument(position.symbol)
            from signalguard.risk.sizing import round_down_to_step

            qty = round_down_to_step(position.qty, instrument.lot_step)
            if qty < instrument.min_qty:
                continue  # dust: below what the exchange will accept
            closed.append(
                await self.submit_order(
                    OrderRequest(
                        client_order_id=new_client_order_id(),
                        symbol=position.symbol,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        qty=qty,
                    )
                )
            )
        return closed

    def _parse_order(
        self, data: dict[str, Any], request: OrderRequest | None = None
    ) -> BrokerOrder:
        filled = _decimal(data.get("executedQty", "0"))
        quote_filled = _decimal(data.get("cummulativeQuoteQty", "0"))
        avg_price = (quote_filled / filled) if filled > 0 else None

        return BrokerOrder(
            client_order_id=data.get("clientOrderId")
            or (request.client_order_id if request else ""),
            broker_order_id=str(data.get("orderId", "")),
            symbol=data["symbol"],
            side=OrderSide(data.get("side", "BUY")),
            order_type=(
                OrderType.LIMIT if data.get("type") == "LIMIT" else OrderType.MARKET
            ),
            qty=_decimal(data.get("origQty", request.qty if request else "0")),
            status=_STATUS_MAP.get(data.get("status", ""), OrderStatus.UNKNOWN),
            filled_qty=filled,
            avg_fill_price=avg_price,
            price=_decimal(data["price"]) if data.get("price") else None,
            stop_price=_decimal(data["stopPrice"]) if data.get("stopPrice") else None,
            submitted_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def stream_fills(self) -> AsyncIterator[Fill]:
        """Fill stream.

        Not implemented for v1: the reconciliation loop already polls broker
        truth, and a websocket user-data stream is a second source of the same
        information with its own reconnect and sequencing failure modes. Adding
        it before the polling path is proven would be optimising the wrong thing.
        """
        raise NotImplementedError(
            "Fill streaming is not implemented; reconciliation polls instead."
        )
