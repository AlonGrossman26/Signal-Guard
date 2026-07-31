"""Risk decisions. APPEND-ONLY — never UPDATE, never DELETE."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import MONEY, Base, uuid_pk
from signalguard.enums import Verdict


class Decision(Base):
    """The outcome of evaluating one alert, written before any order is sent."""

    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(
            f"verdict IN ('{Verdict.APPROVED}', '{Verdict.REJECTED}')",
            name="verdict_valid",
        ),
        # One alert produces exactly one real decision — a database guarantee,
        # not an application promise. This is the structural backstop behind the
        # idempotency requirement: even if two workers race on the same alert,
        # the second INSERT fails. /test decisions are excluded, since testing a
        # signal repeatedly is the whole point of that endpoint.
        Index(
            "uq_decisions_alert_id_live",
            "alert_id",
            unique=True,
            postgresql_where=text("is_test = false"),
        ),
        # The dashboard's "rejections by reason code" breakdown.
        Index("ix_decisions_reason_code_evaluated_at", "reason_code", "evaluated_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="RESTRICT"), nullable=False
    )
    # Null when we never got far enough to resolve which account was meant —
    # an unknown account name, or a payload too broken to read.
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )

    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The full risk config as it stood at evaluation time. This is what makes a
    # profile edit safe: past decisions keep the rules that actually produced
    # them, so the audit trail cannot be rewritten by changing a setting today.
    rule_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)

    computed_qty: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    # The price sizing actually used. For a limit order this is limit_price; for
    # a market order it is the reference price fetched at evaluation time, which
    # is why it has to be recorded rather than re-derived later (plan §2, A8).
    entry_reference_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # True for POST /webhook/{id}/test — full pipeline, never reaches the broker.
    is_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
