"""Open positions and equity snapshots — both caches of broker truth."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, Base, updated_at_col, uuid_pk


class Position(Base):
    """Current holding in one symbol.

    A cache, never the source of truth (CLAUDE.md §10) — the reconciliation loop
    overwrites this from the broker. Nothing may make a trading decision from
    this table without the reconciler having recently confirmed it.

    On Binance spot there are no position objects, only balances: holding
    0.1 BTC *is* the long position (plan §2, A2).
    """

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "symbol", name="uq_positions_broker_account_id_symbol"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    qty: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    avg_entry: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    updated_at: Mapped[datetime] = updated_at_col()


class EquitySnapshot(Base):
    """A point-in-time equity reading, and the daily baseline for rule 8.

    The three money columns are separate on purpose (plan §3, Q3): `equity` is
    mark-to-market (cash + holdings) and is what drawdown and position sizing
    are measured against; `free_balance` is spendable cash and is the hard
    affordability ceiling. Collapsing them into one number called
    "liquid_equity" is what made that question ambiguous in the first place.
    """

    __tablename__ = "equity_snapshots"
    __table_args__ = (
        # Exactly one baseline per account per trading day, enforced by the
        # database. This is what makes the restart and DST requirements hold
        # structurally rather than by careful coding: a process restart cannot
        # mint a second baseline, and a 23- or 25-hour DST day is still one
        # session_date.
        Index(
            "uq_equity_snapshots_baseline_per_day",
            "broker_account_id",
            "session_date",
            unique=True,
            postgresql_where=text("is_session_baseline = true"),
        ),
        Index("ix_equity_snapshots_account_taken_at", "broker_account_id", "taken_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )

    equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)       # mark-to-market
    free_balance: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    position_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_session_baseline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # The user-LOCAL trading day this baseline belongs to. Local, not UTC:
    # the reset happens at the user's configured wall-clock time in their own
    # timezone, so "which trading day is this?" is a local-calendar question.
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
