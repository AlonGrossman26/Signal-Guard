"""Inbound alerts. APPEND-ONLY — never UPDATE, never DELETE."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import Base, uuid_pk


class Alert(Base):
    """Every inbound webhook request, recorded before any judgement is made.

    Written *first*, before authentication succeeds, before parsing, before the
    risk engine runs — because constraint #5 says every inbound alert is
    persisted independently of whether an order was placed. If the process dies
    one line later, the record still exists. That is what makes the audit trail
    trustworthy: it does not depend on the happy path completing.

    Append-only is enforced by a database trigger (see the initial migration),
    not merely by convention. A convention is a comment; a trigger is a rule.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        # The Postgres backstop for duplicate detection. Redis owns the fast
        # 60-second window; this index makes the durable check cheap too.
        Index("ix_alerts_dedupe_key_received_at", "dedupe_key", "received_at"),
        # The dashboard's live feed: newest first, per user.
        Index("ix_alerts_user_id_received_at", "user_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    webhook_endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # The payload as received, with one exception: the body `secret` field is
    # replaced with "[REDACTED]" before storage. CLAUDE.md §6 says "verbatim",
    # but constraint #6 says secrets are never stored in the clear, and in that
    # conflict the secret wins (plan §4, OQ-4).
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # SHA-256 of the true, unmodified request bytes. Redaction above loses exact
    # fidelity, so this preserves what redaction costs: the ability to prove what
    # was actually received, and to settle an HMAC dispute after the fact.
    raw_body_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    content_length: Mapped[int] = mapped_column(Integer, nullable=False)

    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)

    # Recorded so the audit trail shows which authentication mode was used —
    # the in-body secret is weaker than HMAC, and that difference should be
    # visible after the fact, not buried in configuration.
    auth_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
