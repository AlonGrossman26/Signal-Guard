"""Add users.telegram_chat_id for per-user notification routing.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01

Per-user Telegram routing (§10, §12): one shared app-level bot, each user's own
chat id. The chat id is a routing address, not a secret, so it is a plain nullable
column. Null means fall back to the app-level chat (or no notifications).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("telegram_chat_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "telegram_chat_id")
