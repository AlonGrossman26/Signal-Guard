"""Webhook endpoints — the per-user URL a signal source posts to."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from signalguard.db.base import Base, created_at_col, uuid_pk


class WebhookEndpoint(Base):
    """The `endpoint_id` in POST /webhook/{endpoint_id}.

    Not in CLAUDE.md §6, but required: `endpoint_id` is a long random token that
    grants the ability to submit signals, so it is a *credential*, not an
    identifier (plan §2, A9). Storing it in plaintext would mean a database leak
    hands over working webhook URLs.

    The hash must be **deterministic** — SHA-256 over (pepper || token) — because
    we look the endpoint up by value on every single request, so it has to be
    indexable. Argon2 is deliberately slow, which is exactly right for passwords
    (guessed one at a time by an attacker) and exactly wrong here. The pepper
    lives in the environment, so a stolen database alone is not enough to
    brute-force a token offline.
    """

    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    endpoint_id_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # HMAC signing secret (preferred auth) and the weaker in-body shared secret
    # TradingView's free plan forces. Both encrypted at rest (§12).
    hmac_secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hmac_secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_secret_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False, default=1)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
