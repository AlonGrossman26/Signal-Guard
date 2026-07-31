"""Closed round-trips and the circuit-breaker state they drive."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, Base, updated_at_col, uuid_pk
from signalguard.enums import CircuitState


class Trade(Base):
    """A completed round-trip with its realized PnL.

    This is what the circuit breaker counts — closed trades, not orders and not
    open positions. An open position that is currently down is not a loss yet.
    """

    __tablename__ = "trades"
    __table_args__ = (
        # Exactly the circuit breaker's query: consecutive losses, most recent
        # first, for one account.
        Index("ix_trades_account_closed_at", "broker_account_id", "closed_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    qty: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Net of fees. A trade that is gross-positive but fee-negative is a loss,
    # and the circuit breaker must count it as one.
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    entry_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    exit_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )


class CircuitBreakerState(Base):
    """Durable circuit-breaker state, one row per broker account.

    CLAUDE.md §7 requires this state to live in Redis *and* Postgres and to
    survive a restart. Redis is the fast path; this table is the record. If they
    disagree, this wins — and if Redis is unreachable, an unknown breaker state
    is treated as OPEN (fail closed), which is only possible because the durable
    copy exists here.
    """

    __tablename__ = "circuit_breaker_state"
    __table_args__ = (
        CheckConstraint(
            f"state IN ('{CircuitState.CLOSED}', '{CircuitState.OPEN}')",
            name="circuit_state_valid",
        ),
        CheckConstraint("consecutive_losses >= 0", name="losses_non_negative"),
    )

    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CircuitState.CLOSED
    )
    consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the cooldown expires. Null with state OPEN means manual reset only.
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Marks how far the counter has consumed the trade history, so re-running
    # the count is idempotent and a restart cannot double-count a loss.
    last_counted_trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = updated_at_col()
