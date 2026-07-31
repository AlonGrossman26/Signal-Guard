"""Broker accounts and cached exchange instrument filters."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, Base, created_at_col, updated_at_col, uuid_pk
from signalguard.enums import TradingState


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (
        # The webhook payload selects an account by label, so the label must be
        # unambiguous within a user. Two accounts called "binance-testnet-1"
        # would make routing a signal a coin flip.
        UniqueConstraint("user_id", "label", name="uq_broker_accounts_user_id_label"),
        # Constraint #2, enforced by the database rather than by discipline.
        # Until a later phase explicitly authorises live trading, Postgres itself
        # refuses to hold a non-testnet account.
        CheckConstraint("is_testnet = true", name="testnet_only"),
        CheckConstraint(
            f"trading_state IN ('{TradingState.ACTIVE}', '{TradingState.LOCKED}')",
            name="trading_state_valid",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    broker: Mapped[str] = mapped_column(String(64), nullable=False)  # binance_spot_testnet
    label: Mapped[str] = mapped_column(String(64), nullable=False)

    # AES-GCM envelope encryption (§12). Decrypted only in memory at the moment
    # of use, never logged, never returned by the API.
    encrypted_credentials: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credentials_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Lets the master key be rotated without re-encrypting everything at once.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    is_testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The durable kill-switch record. Deliberately a column here rather than
    # Redis-only state: if Redis is down we must still be able to answer "is this
    # account locked?", and the fail-closed answer to an unanswerable question is
    # to refuse to trade (plan §3, Q5).
    trading_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TradingState.ACTIVE
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Instrument(Base):
    """Cached exchange trading filters for one symbol.

    CLAUDE.md §7: fetch lot_step / min_qty / min_notional from the exchange and
    cache them, never hardcode. Cached in Postgres (not only Redis) so a restart
    while the exchange is unreachable does not leave us unable to size anything.

    `fetched_at` drives the staleness policy in plan §4 OQ-5: past the maximum
    age we reject with INSTRUMENT_UNAVAILABLE rather than size against filters
    that may have changed.
    """

    __tablename__ = "instruments"

    broker: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)

    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)

    tick_size: Mapped[Decimal] = mapped_column(MONEY, nullable=False)     # price increment
    lot_step: Mapped[Decimal] = mapped_column(MONEY, nullable=False)      # quantity increment
    min_qty: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)  # TRADING, HALT, ...
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
