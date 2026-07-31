"""A controllable fake broker (CLAUDE.md §13).

    "A fake broker adapter that can be told to time out, partially fill, reject,
     and double-fill — used for integration tests. No test may touch a real
     network."

Those four behaviours are not arbitrary. Each is a real failure mode that has
cost people real money:

* **timeout** — the order may or may not have landed; we do not know
* **partial fill** — the protective stop must cover what actually filled, not
  what was requested
* **reject** — the entry never opened, so there is nothing to unwind
* **double fill** — the same client order ID submitted twice must not become two
  positions

The fake implements the same interface as the real adapter, so anything tested
against it is testing the code that runs in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

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

DEFAULT_INSTRUMENT = Instrument(
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("10"),
    status="TRADING",
)


@dataclass
class FakeBrokerBehaviour:
    """Dials for each failure mode. All default to "behaves normally"."""

    fail_next_submit: BrokerErrorCode | None = None
    fail_stop_submit: bool = False          # entry succeeds, stop fails
    partial_fill_ratio: Decimal | None = None
    timeout_on_submit: bool = False
    fail_cancel_all: bool = False
    fail_close_all: bool = False
    fail_account_state: bool = False
    unknown_order_on_get: bool = False


@dataclass
class FakeBroker(BrokerAdapter):
    """An in-memory broker. Never touches a network."""

    equity: Decimal = Decimal("10000")
    free_balance: Decimal = Decimal("10000")
    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    behaviour: FakeBrokerBehaviour = field(default_factory=FakeBrokerBehaviour)

    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    submit_calls: list[OrderRequest] = field(default_factory=list)
    cancel_all_calls: int = 0
    close_all_calls: int = 0
    _next_id: int = 1000

    # --- Reads ---------------------------------------------------------------

    async def get_account_state(self) -> AccountState:
        if self.behaviour.fail_account_state:
            raise BrokerError(BrokerErrorCode.CONNECTION_FAILED, retryable=True)
        position_value = sum(
            (p.qty * p.mark_price for p in self.positions.values()), start=Decimal("0")
        )
        return AccountState(
            total_equity=self.free_balance + position_value,
            free_balance=self.free_balance,
            position_value=position_value,
            positions=tuple(self.positions.values()),
        )

    async def get_instrument(self, symbol: str) -> Instrument:
        return DEFAULT_INSTRUMENT

    async def list_instruments(self) -> tuple[Instrument, ...]:
        return (DEFAULT_INSTRUMENT,)

    async def get_order(self, client_order_id: str, symbol: str) -> BrokerOrder:
        if self.behaviour.unknown_order_on_get:
            raise BrokerError(BrokerErrorCode.UNKNOWN_ORDER)
        order = self.orders.get(client_order_id)
        if order is None:
            raise BrokerError(BrokerErrorCode.UNKNOWN_ORDER)
        return order

    # --- Writes --------------------------------------------------------------

    async def submit_order(self, order: OrderRequest) -> BrokerOrder:
        self.submit_calls.append(order)

        if self.behaviour.timeout_on_submit:
            raise BrokerError(BrokerErrorCode.TIMEOUT, "timed out", retryable=False)

        is_stop = order.stop_price is not None
        if is_stop and self.behaviour.fail_stop_submit:
            raise BrokerError(BrokerErrorCode.INVALID_ORDER, "stop rejected")

        if self.behaviour.fail_next_submit is not None and not is_stop:
            code = self.behaviour.fail_next_submit
            self.behaviour.fail_next_submit = None  # one-shot
            raise BrokerError(code)

        # Idempotency: the same client order ID is the SAME order, never a second
        # one. This is what makes a retried submission safe.
        if order.client_order_id in self.orders:
            return self.orders[order.client_order_id]

        self._next_id += 1

        if is_stop:
            filled = Decimal("0")
            status = OrderStatus.SUBMITTED
        elif self.behaviour.partial_fill_ratio is not None:
            filled = order.qty * self.behaviour.partial_fill_ratio
            status = OrderStatus.PARTIALLY_FILLED
        else:
            filled = order.qty
            status = OrderStatus.FILLED

        price = order.price or Decimal("62000")
        result = BrokerOrder(
            client_order_id=order.client_order_id,
            broker_order_id=str(self._next_id),
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            qty=order.qty,
            status=status,
            filled_qty=filled,
            avg_fill_price=price if filled > 0 else None,
            price=order.price,
            stop_price=order.stop_price,
            fees=filled * price * Decimal("0.001"),
            fee_asset="USDT",
            submitted_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.orders[order.client_order_id] = result

        # Keep the fake's own position book consistent with what it filled.
        if filled > 0 and not is_stop:
            if order.side is OrderSide.BUY:
                existing = self.positions.get(order.symbol)
                new_qty = (existing.qty if existing else Decimal("0")) + filled
                self.positions[order.symbol] = BrokerPosition(
                    symbol=order.symbol, qty=new_qty, avg_entry=price, mark_price=price
                )
                self.free_balance -= filled * price
            else:
                existing = self.positions.get(order.symbol)
                if existing:
                    remaining = existing.qty - filled
                    if remaining <= 0:
                        self.positions.pop(order.symbol, None)
                    else:
                        self.positions[order.symbol] = BrokerPosition(
                            symbol=order.symbol,
                            qty=remaining,
                            avg_entry=existing.avg_entry,
                            mark_price=price,
                        )
                self.free_balance += filled * price

        return result

    async def cancel_order(self, broker_order_id: str, symbol: str) -> None:
        for key, order in list(self.orders.items()):
            if order.broker_order_id == broker_order_id:
                self.orders[key] = BrokerOrder(
                    **{**order.__dict__, "status": OrderStatus.CANCELLED}
                )

    async def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        if self.behaviour.fail_cancel_all:
            raise BrokerError(BrokerErrorCode.CONNECTION_FAILED, retryable=True)
        open_count = sum(
            1
            for o in self.orders.values()
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
        )
        for key, order in list(self.orders.items()):
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
                self.orders[key] = BrokerOrder(
                    **{**order.__dict__, "status": OrderStatus.CANCELLED}
                )
        return open_count

    async def close_all_positions(self) -> list[BrokerOrder]:
        self.close_all_calls += 1
        if self.behaviour.fail_close_all:
            raise BrokerError(BrokerErrorCode.CONNECTION_FAILED, retryable=True)

        closed: list[BrokerOrder] = []
        for symbol, position in list(self.positions.items()):
            if position.qty <= 0:
                continue
            closed.append(
                await self.submit_order(
                    OrderRequest(
                        client_order_id=f"close-{symbol}-{self._next_id}",
                        symbol=symbol,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        qty=position.qty,
                    )
                )
            )
        return closed

    async def stream_fills(self) -> AsyncIterator[Fill]:
        for order in self.orders.values():
            if order.filled_qty > 0:
                yield Fill(
                    broker_order_id=order.broker_order_id,
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.filled_qty,
                    price=order.avg_fill_price or Decimal("0"),
                    fee=order.fees,
                    fee_asset=order.fee_asset or "USDT",
                    filled_at=order.updated_at or datetime.now(UTC),
                    is_final=order.status is OrderStatus.FILLED,
                )
