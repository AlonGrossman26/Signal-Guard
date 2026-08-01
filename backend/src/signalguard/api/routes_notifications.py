"""Per-user notification settings (CLAUDE.md §10, §12).

Shared bot + per-user chat id: the app runs one Telegram bot (its token is
app-level config); each user sets their own chat id here, and their kill-switch
and other alerts route to it. Clearing it falls back to the app-level chat, or to
no notifications if neither is set.

The chat id is a routing address, not a secret, so it is returned in plain — but
whether Telegram is even configured (the bot token) is never exposed here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from signalguard.api.schemas import (
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
)
from signalguard.api.security import CurrentUser, DbSession

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
async def get_notification_settings(user: CurrentUser) -> NotificationSettingsResponse:
    return NotificationSettingsResponse(telegram_chat_id=user.telegram_chat_id)


@router.put("")
async def update_notification_settings(
    body: NotificationSettingsUpdate, session: DbSession, user: CurrentUser
) -> NotificationSettingsResponse:
    user.telegram_chat_id = body.telegram_chat_id
    await session.commit()
    logger.info(
        "Notification settings updated",
        extra={"user_id": str(user.id), "has_chat_id": user.telegram_chat_id is not None},
    )
    return NotificationSettingsResponse(telegram_chat_id=user.telegram_chat_id)
