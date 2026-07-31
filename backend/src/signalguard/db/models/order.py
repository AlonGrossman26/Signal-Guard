"""Orders sent to (or about to be sent to) the broker."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, Base, updated_at_col, uuid_pk


class Order(Base):
    """One order, from before submission through to its final state.

    The row is written with status PENDING_SUBMIT and our own `client_order_id`
    **before** the HTTP call to the broker. That ordering is what makes a lost
    response survivable: if the network drops the reply, we still know exactly
    what we sent and under which ID, so reconciliation can ask the broker "what
    happened to this client ID?" instead of guessing. Writing the row *after* the
    call would mean a timeout leaves an order at the exchange that we have no
    record of — a naked position nobody knows about, which is the worst state
    this system can reach.
    """

    __tablename__ = "orders"
    __table_args__ = (
        # The exchange-side idempotency key. Passing a client-supplied ID means a
        # retried submission can never become two orders.
        UniqueConstraint(
            "broker_account_id", "client_order_id",
            name="uq_orders_broker_account_id_client_order_id",
        ),
        Index("ix_orders_broker_order_id", "broker_order_id"),
        Index("ix_orders_broker_account_id_status", "broker_account_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decisions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )

    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Null until the exchange answers. Null here with status SUBMITTED means the
    # reconciler has work to do.
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # ENTRY / STOP / TAKE_PROFIT / EXIT / KILL_SWITCH. Needed to tell an entry
    # from its protective stop — "never leave a naked position" depends on it.
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    qty: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    filled_qty: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    avg_fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    fee_asset: Mapped[str | None] = mapped_column(String(16), nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = updated_at_col()

    # A mapped internal error code, never a raw broker exception (CLAUDE.md §9).
    # Broker error text has a habit of containing the request — including keys.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
