"""Composition root: the one place that knows both ends of the pipeline.

CLAUDE.md §5 says `ingress/` "knows HTTP, knows nothing about brokers". That rule
is what keeps the webhook layer testable and the broker layer replaceable, and it
is also why the two were never joined — there was no legal place for the wire.

This module is that place. It sits above both layers rather than inside either,
so `ingress/` still imports no broker and `execution/` still imports no HTTP
handling. What ingress receives is a pair of plain callables; what it does with
them requires no knowledge of what is behind them, which is exactly the property
the boundary exists to protect.

**One adapter per alert.** `BrokerSession` builds at most one adapter and reuses
it for account state, reference price and order submission. Building three would
open three HTTP clients and three sets of decrypted credentials for a single
signal — and credentials are meant to exist in memory for the moment of use, not
for however long three clients take to garbage-collect (§12).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard import realtime
from signalguard.config import Settings, get_settings
from signalguard.db.models import BrokerAccount, User
from signalguard.enums import ReasonCode, Verdict
from signalguard.execution.base import BrokerAdapter, BrokerError
from signalguard.execution.binance_testnet import BinanceTestnetAdapter
from signalguard.execution.executor import execute_decision
from signalguard.execution.factory import build_broker
from signalguard.execution.orders import ExecutionResult
from signalguard.notify import Notifier, notifier_for_user
from signalguard.risk.types import OpenPosition

if TYPE_CHECKING:
    from signalguard.ingress.pipeline import PipelineResult

logger = logging.getLogger(__name__)

# Rejections that mean "we could not act", as opposed to "we decided not to".
# An exit blocked by one of these leaves the user exposed, so it is escalated
# rather than merely recorded (plan §3, Q2).
_UNDELIVERABLE = frozenset(
    {ReasonCode.BROKER_UNAVAILABLE, ReasonCode.STATE_UNAVAILABLE}
)


class BrokerSession:
    """A lazily-built broker adapter, scoped to the handling of one alert.

    Not thread-safe and not shared: one of these belongs to one alert, and is
    closed when that alert is done being handled.
    """

    def __init__(self, master_key: str) -> None:
        self._master_key = master_key
        self._adapter: BrokerAdapter | None = None
        self._account_id: str | None = None

    def adapter_for(self, account: BrokerAccount) -> BrokerAdapter:
        """Build (or reuse) the adapter for this account.

        Raises whatever `build_broker` raises — an unsupported broker or
        undecodable credentials are configuration failures, and the caller
        converts them into a rejection rather than swallowing them here.
        """
        if self._adapter is not None and self._account_id == str(account.id):
            return self._adapter
        self._adapter = build_broker(account, self._master_key)
        self._account_id = str(account.id)
        return self._adapter

    @property
    def adapter(self) -> BrokerAdapter | None:
        """The adapter built during this alert, if one was built."""
        return self._adapter

    async def account_state(
        self, account: BrokerAccount
    ) -> tuple[Decimal, Decimal, tuple[OpenPosition, ...]]:
        """Fetch broker ground truth, in the shape the risk pipeline wants.

        Deliberately allowed to raise. `evaluate_alert` catches any exception
        here and rejects with `BROKER_UNAVAILABLE` — no account state means no
        snapshot, and no snapshot means no evaluation (plan §3, Q2).
        """
        state = await self.adapter_for(account).get_account_state()
        positions = tuple(
            OpenPosition(
                symbol=p.symbol,
                qty=p.qty,
                avg_entry=p.avg_entry,
                mark_price=p.mark_price,
            )
            for p in state.positions
        )
        return state.total_equity, state.free_balance, positions

    async def reference_price(
        self, account: BrokerAccount, symbol: str
    ) -> Decimal | None:
        """Current price for sizing a market order (plan §2, A8).

        Returns None rather than raising when the price cannot be fetched. The
        engine then has no reference price, and rule 6 rejects — which is the
        fail-closed outcome, reached through the rule chain so the user gets a
        proper reason code instead of an exception.
        """
        adapter = self.adapter_for(account)
        if not isinstance(adapter, BinanceTestnetAdapter):
            return None
        try:
            return await adapter.get_reference_price(symbol)
        except BrokerError as exc:
            logger.warning(
                "Could not fetch a reference price; sizing will reject",
                extra={"symbol": symbol, "error_code": exc.code.value},
            )
            return None

    async def aclose(self) -> None:
        """Release the HTTP client, if one was ever built. Never raises."""
        adapter = self._adapter
        self._adapter = None
        if adapter is None:
            return
        closer = getattr(adapter, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001 - teardown must not mask the real outcome
            logger.warning("Failed to close a broker adapter cleanly")


async def execute_approved(
    session: AsyncSession,
    broker_session: BrokerSession,
    evaluation: PipelineResult,
    user_id: uuid.UUID,
    redis: Any | None = None,
) -> ExecutionResult | None:
    """Submit the orders an approved decision calls for (CLAUDE.md §10).

    Everything here is best-effort *relative to the decision*, which is already
    committed by the time this runs. A failure below therefore leaves a complete
    audit record of what was approved and why, plus order rows for whatever was
    attempted — precisely the state the reconciler exists to repair.

    The ordering is constraint #5 taken literally: decision durable first, orders
    second. The reverse would allow an order with no decision behind it, which is
    unauditable by construction.
    """
    decision = evaluation.decision
    if decision.verdict is not Verdict.APPROVED:
        return None

    account = evaluation.account
    alert_input = evaluation.alert_input
    if account is None or alert_input is None:
        # Unreachable on an approval — the engine only approves once both are
        # resolved. Refuse rather than reach for a default.
        logger.error(
            "Approved decision without a resolved account; not executing",
            extra={"decision_id": str(evaluation.decision_id)},
        )
        return None

    adapter = broker_session.adapter
    if adapter is None:
        logger.error(
            "Approved decision but no broker adapter was built; not executing",
            extra={"decision_id": str(evaluation.decision_id)},
        )
        return None

    notifier = await notifier_for_account(session, account, get_settings())
    try:
        result = await execute_decision(
            session,
            adapter,
            account=account,
            decision=decision,
            decision_id=evaluation.decision_id,
            alert=alert_input,
            notifier=notifier,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "Order submission failed after an approved decision",
            extra={"decision_id": str(evaluation.decision_id)},
        )
        return None
    finally:
        if notifier is not None:
            await notifier.aclose()

    if result is not None and redis is not None:
        await _publish_orders(redis, user_id, result)
    return result


async def _publish_orders(
    redis: Any, user_id: uuid.UUID, result: ExecutionResult
) -> None:
    """Push freshly-submitted orders onto the user's live feed."""
    for role, order in (("ENTRY", result.entry), ("STOP", result.stop)):
        if order is None:
            continue
        await realtime.publish(
            redis,
            user_id,
            realtime.EventType.ORDER,
            {
                "symbol": order.symbol,
                "side": order.side,
                "type": order.order_type,
                "role": role,
                "status": order.status,
                "qty": order.qty,
                "filled_qty": order.filled_qty,
                "avg_fill_price": order.avg_fill_price,
            },
        )


async def notify_undelivered_exit(
    session: AsyncSession,
    evaluation: PipelineResult,
    action: str,
) -> bool:
    """Alert loudly when an *exit* signal could not be delivered (plan §3, Q2).

    Q2's answer is "reject when the broker is unreachable", and that answer has
    one uncomfortable case, named in the plan rather than hidden: `sell` and
    `close` are risk-**reducing**. Rejecting one leaves the user holding a
    position they explicitly asked to exit, and they will not find out from a
    rejection row on a dashboard they are not looking at.

    So this case gets pushed, not logged. It is the difference between "the
    system behaved correctly" and "the user knows they are still exposed".
    """
    decision = evaluation.decision
    if decision.verdict is not Verdict.REJECTED:
        return False
    if decision.reason_code not in _UNDELIVERABLE:
        return False
    if action.lower() not in ("sell", "close"):
        return False

    account = evaluation.account
    if account is None:
        return False

    notifier = await notifier_for_account(session, account, get_settings())
    if notifier is None:
        logger.critical(
            "EXIT SIGNAL NOT DELIVERED and no notifier is configured — the user "
            "is still holding a position they asked to close",
            extra={
                "broker_account_id": str(account.id),
                "reason_code": decision.reason_code.value,
            },
        )
        return False

    try:
        return await notifier.undelivered_exit(
            account.label, decision.reason_code.value
        )
    finally:
        await notifier.aclose()


async def notifier_for_account(
    session: AsyncSession, account: BrokerAccount, settings: Settings
) -> Notifier | None:
    """Build the notifier that routes to this account's owner (H-3).

    Shared app bot, per-user chat id — the model the human chose in PQ-2. Falls
    back to the app-level chat when the user has not set one, and returns None
    when notifications are not configured at all.
    """
    if not settings.telegram_bot_token:
        return None
    chat_id = (
        await session.execute(
            select(User.telegram_chat_id).where(User.id == account.user_id)
        )
    ).scalar_one_or_none()
    return notifier_for_user(settings, chat_id)


async def build_broker_for(
    account: BrokerAccount, master_key: str
) -> BrokerAdapter:
    """Adapter factory in the shape the reconciliation loop expects.

    The loop takes an async factory so it can be driven with a fake in tests;
    building an adapter opens no sockets, so the `async` here is purely about
    matching that signature.
    """
    return build_broker(account, master_key)
