"""Turn an approved decision into orders (CLAUDE.md §10).

This is the module the audit found missing. Everything under it already existed —
`submit_entry_with_stop`, the order rows, the emergency close — but nothing ever
called them, so an `APPROVED` decision was persisted and then dropped. That made
SignalGuard a rejection engine rather than the middleware described in §1.

The contract here is narrow on purpose:

* **A rejection executes nothing.** Not "executes a smaller order", not "executes
  with a warning". The decision is the authority, and the only thing this module
  is allowed to do with a `REJECTED` verdict is return.
* **An entry is never submitted without its stop.** That guarantee lives in
  `orders.submit_entry_with_stop`; this module simply refuses to offer any path
  around it.
* **An exit closes what is held.** The quantity comes from the engine's reading of
  broker state, never from a fresh calculation — sizing an exit could sell more
  than we own, or leave a remainder behind and call the position closed.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import BrokerAccount
from signalguard.enums import AlertAction, OrderRole, OrderSide, OrderStatus, OrderType, Verdict
from signalguard.execution.base import BrokerAdapter, BrokerError, OrderRequest
from signalguard.execution.orders import (
    ExecutionResult,
    apply_broker_result,
    new_client_order_id,
    record_order,
    submit_entry_with_stop,
)
from signalguard.notify import Notifier
from signalguard.risk.types import AlertInput, Decision

logger = logging.getLogger(__name__)


async def execute_decision(
    session: AsyncSession,
    broker: BrokerAdapter,
    *,
    account: BrokerAccount,
    decision: Decision,
    decision_id: uuid.UUID,
    alert: AlertInput,
    notifier: Notifier | None = None,
) -> ExecutionResult | None:
    """Submit the orders an approved decision calls for.

    Returns None when there is nothing to do — a rejection, or an exit on a
    symbol we do not actually hold. Never raises for a broker failure: the
    failure is recorded on the order row and surfaced in the result, because a
    raised exception here would land in a background task with nobody to catch it.
    """
    if decision.verdict is not Verdict.APPROVED:
        return None

    qty = decision.computed_qty
    if qty is None or qty <= 0:
        # An approval with no quantity is only legitimate for an exit on a flat
        # symbol. Anything else is a contradiction, and a contradiction in the
        # order path is refused rather than interpreted.
        if not alert.is_exit:
            logger.error(
                "Approved decision carries no quantity; not submitting",
                extra={"decision_id": str(decision_id), "symbol": alert.symbol},
            )
        return None

    if alert.is_exit:
        return await _submit_exit(
            session,
            broker,
            decision_id=decision_id,
            account=account,
            symbol=alert.symbol,
            qty=qty,
        )

    if decision.stop_price is None:
        # Rule 6 guarantees a stop on every approved entry. If one is missing
        # here the engine and this module disagree, and the safe reading of a
        # disagreement about protection is to place nothing.
        logger.error(
            "Approved entry has no stop price; refusing to submit",
            extra={"decision_id": str(decision_id), "symbol": alert.symbol},
        )
        return None

    side = OrderSide.BUY if alert.action is AlertAction.BUY else OrderSide.SELL
    return await submit_entry_with_stop(
        session,
        broker,
        decision_id=decision_id,
        broker_account_id=account.id,
        symbol=alert.symbol,
        side=side,
        order_type=alert.order_type,
        qty=qty,
        limit_price=alert.limit_price if alert.order_type is OrderType.LIMIT else None,
        stop_price=decision.stop_price,
        notifier=notifier,
        account_label=account.label,
    )


async def _submit_exit(
    session: AsyncSession,
    broker: BrokerAdapter,
    *,
    decision_id: uuid.UUID,
    account: BrokerAccount,
    symbol: str,
    qty: Decimal,
) -> ExecutionResult:
    """Close a held position at market.

    Always a market order. A limit exit can sit unfilled while the reason you
    wanted out gets worse, and "I asked to be flat and I am not flat" is the
    state this project refuses to create.
    """
    request = OrderRequest(
        client_order_id=new_client_order_id(),
        symbol=symbol,
        side=OrderSide.SELL,  # spot is long-only in v1 (OQ-6): an exit sells
        order_type=OrderType.MARKET,
        qty=qty,
    )
    row = await record_order(
        session,
        decision_id=decision_id,
        broker_account_id=account.id,
        client_order_id=request.client_order_id,
        request=request,
        role=OrderRole.EXIT,
    )

    try:
        result = await broker.submit_order(request)
    except BrokerError as exc:
        row.status = OrderStatus.REJECTED.value
        row.last_error = exc.code.value
        await session.flush()
        logger.error(
            "Exit order rejected; the position is still open",
            extra={"symbol": symbol, "error_code": exc.code.value},
        )
        return ExecutionResult(entry=None, stop=None, error=exc.code.value)

    apply_broker_result(row, result)
    await session.flush()
    logger.info(
        "Position exit submitted",
        extra={"symbol": symbol, "qty": str(qty), "account_label": account.label},
    )
    return ExecutionResult(entry=result, stop=None)
