"""Broker adapter interface and its domain types (CLAUDE.md §9).

One abstract base class, implemented exactly once (Binance spot testnet). The
interface exists so the *seam* is clear, not so we can swap brokers — multiple
brokers are explicitly out of scope for v1, and building a plugin layer for them
would be the "future-proofing" CLAUDE.md §13 forbids.

Knows brokers. Knows nothing about HTTP requests or the risk engine (§5).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from signalguard.enums import OrderSide, OrderStatus, OrderType


class BrokerErrorCode(StrEnum):
    """The small internal error enum from §9.

    Every broker failure is mapped into one of these before it leaves this
    layer. Raw exceptions are never allowed upward: an exchange error message
    routinely echoes the request that caused it, API key included, and it would
    end up in a log or an HTTP response.
    """

    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INVALID_ORDER = "INVALID_ORDER"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    AUTH_FAILED = "AUTH_FAILED"
    MARKET_CLOSED = "MARKET_CLOSED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    UNKNOWN = "UNKNOWN"


class BrokerError(Exception):
    """A mapped broker failure.

    `retryable` is the important field. Only idempotent operations may be
    retried, and only on errors that could plausibly succeed later — retrying an
    INVALID_ORDER just sends the same bad order again, and retrying a
    non-idempotent submit is how one signal becomes two positions.
    """

    def __init__(self, code: BrokerErrorCode, message: str = "", *, retryable: bool = False):
        # The message is ours, never the broker's raw text.
        super().__init__(f"{code}: {message}" if message else str(code))
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    lot_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    status: str


@dataclass(frozen=True)
class BrokerPosition:
    """On spot this is a non-zero base-asset balance, not a position object."""

    symbol: str
    qty: Decimal
    avg_entry: Decimal
    mark_price: Decimal


@dataclass(frozen=True)
class AccountState:
    """Broker ground truth.

    Three money fields, deliberately named for exactly what they are (plan §3,
    Q3): `total_equity` is mark-to-market and sets the risk budget,
    `free_balance` is spendable cash and caps what can be bought.
    """

    total_equity: Decimal
    free_balance: Decimal
    position_value: Decimal
    positions: tuple[BrokerPosition, ...] = ()


@dataclass(frozen=True)
class OrderRequest:
    """An order to place.

    `client_order_id` is generated and persisted by us *before* submission and
    passed through to the exchange, so a retry can never become two orders.
    """

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    status: OrderStatus
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    fees: Decimal = Decimal("0")
    fee_asset: str | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Fill:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    filled_at: datetime
    is_final: bool = field(default=False)


class BrokerAdapter(ABC):
    """The broker interface from CLAUDE.md §9.

    Every method must have a timeout. Retries are bounded, use exponential
    backoff, and apply **only to idempotent operations** — reads and cancels.
    `submit_order` is never blindly retried; its safety comes from the
    client-supplied order ID plus an explicit lookup of what happened.
    """

    @abstractmethod
    async def get_account_state(self) -> AccountState: ...

    @abstractmethod
    async def get_instrument(self, symbol: str) -> Instrument: ...

    @abstractmethod
    async def list_instruments(self) -> tuple[Instrument, ...]: ...

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> BrokerOrder: ...

    @abstractmethod
    async def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str, symbol: str) -> None: ...

    @abstractmethod
    async def cancel_all_orders(self) -> int: ...

    @abstractmethod
    async def close_all_positions(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def stream_fills(self) -> AsyncIterator[Fill]: ...
