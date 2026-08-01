"""Kill switch and manual unlock (CLAUDE.md §10, §11).

The safety-critical guarantee this endpoint always delivers is the **durable
lock**: once `set_locked` returns, the account's `trading_state` is `LOCKED` in
Postgres, and the risk engine refuses every new signal for it (`is_locked` fails
closed). That holds even if Redis is down and even if no broker connection is
available.

The physical flatten (cancel every open order, close every position) is the
execution layer's job. Where a broker adapter is wired in — the reconciler at
runtime, or an injected fake in tests — this endpoint drives the full, already
tested `fire_kill_switch` sweep immediately. Where one is not, the account is
still locked at once and the reconciliation loop enforces "flat while LOCKED"
continuously (`execution/killswitch.py`). Either way, no new order can be
accepted the instant this returns.

Unlock is deliberately separate and manual: nothing in this system clears a lock
on a timer. Whatever fired the kill switch deserves a human deciding it is
resolved.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from signalguard.api.routes_accounts import _owned_account
from signalguard.api.schemas import BrokerAccountResponse, KillSwitchResponse
from signalguard.api.security import AppSettings, CurrentUser, DbSession
from signalguard.db.models import BrokerAccount
from signalguard.execution.base import BrokerAdapter
from signalguard.execution.factory import build_broker
from signalguard.execution.killswitch import fire_kill_switch, set_locked, unlock_account
from signalguard.notify import Notifier, notifier_for_user
from signalguard.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/broker-accounts", tags=["kill-switch"])


async def provide_kill_switch_broker(
    account_id: uuid.UUID, session: DbSession, settings: AppSettings
) -> BrokerAdapter | None:
    """Broker adapter used to flatten an account when the kill switch fires.

    Builds the real Binance testnet adapter from the account's encrypted
    credentials, so firing the kill switch cancels orders and closes positions
    immediately. Returns ``None`` — falling back to a durable lock only, with the
    reconciler enforcing the flatten — when the account is gone or its
    credentials cannot be decoded, because the endpoint must still lock the
    account even when no broker can be built. Tests override this dependency with
    a fake so no test touches the network.
    """
    account = await session.get(BrokerAccount, account_id)
    if account is None:
        return None
    try:
        return build_broker(account, settings.credentials_master_key)
    except Exception:  # noqa: BLE001 - any build failure falls back to lock-only
        logger.warning(
            "Could not build broker for kill-switch sweep; locking only",
            extra={"broker_account_id": str(account_id)},
        )
        return None


async def provide_notifier(settings: AppSettings, user: CurrentUser) -> Notifier | None:
    """The Telegram notifier for the current user, or None when unconfigured.

    Routes to the user's own chat when they have set one, falling back to the
    app-level chat (shared bot + per-user chat id, §12/§10).
    """
    return notifier_for_user(settings, user.telegram_chat_id)


@router.post("/{account_id}/kill")
async def kill_switch(
    account_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    broker: Annotated[BrokerAdapter | None, Depends(provide_kill_switch_broker)] = None,
    notifier: Annotated[Notifier | None, Depends(provide_notifier)] = None,
) -> KillSwitchResponse:
    """Lock the account and flatten it. Idempotent — firing twice is harmless."""
    account = await _owned_account(session, user.id, account_id)
    redis = get_redis()
    reason = "manual kill switch (dashboard)"

    orders_cancelled = 0
    positions_closed = 0
    errors: list[str] = []
    swept = False

    if broker is not None:
        result = await fire_kill_switch(session, redis, broker, account.id, reason)
        orders_cancelled = result.orders_cancelled
        positions_closed = result.positions_closed
        errors = result.errors
        swept = True
    else:
        # No adapter available: lock durably now, let the reconciler flatten.
        await set_locked(session, redis, account.id, reason)

    await session.commit()
    await session.refresh(account)
    logger.critical(
        "Kill switch fired via API",
        extra={"broker_account_id": str(account.id), "swept": swept, "errors": errors},
    )

    # Notify best-effort, after the lock is durable. kill_switch_fired never
    # raises; closing the client here avoids leaking a per-request connection.
    if notifier is not None:
        try:
            await notifier.kill_switch_fired(
                account.label,
                orders_cancelled=orders_cancelled,
                positions_closed=positions_closed,
                errors=errors,
            )
        finally:
            await notifier.aclose()

    return KillSwitchResponse(
        account_id=account.id,
        trading_state=account.trading_state,
        locked_at=account.locked_at,
        locked_reason=account.locked_reason,
        orders_cancelled=orders_cancelled,
        positions_closed=positions_closed,
        swept=swept,
        errors=errors,
    )


@router.post("/{account_id}/unlock")
async def unlock(
    account_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> BrokerAccountResponse:
    """Clear a kill-switch lock. Always an explicit human act, never automatic."""
    account = await _owned_account(session, user.id, account_id)
    redis = get_redis()
    await unlock_account(session, redis, account.id)
    await session.commit()
    await session.refresh(account)
    logger.warning("Account unlocked via API", extra={"broker_account_id": str(account.id)})
    return BrokerAccountResponse(
        id=account.id,
        broker=account.broker,
        label=account.label,
        is_testnet=account.is_testnet,
        is_active=account.is_active,
        trading_state=account.trading_state,
        locked_at=account.locked_at,
        locked_reason=account.locked_reason,
        created_at=account.created_at,
    )
