"""Read models for the dashboard: decisions, orders, positions, equity curve.

Everything here is scoped to the logged-in user and read-only. The decisions and
orders feeds are the audit trail the whole product exists to produce, so they are
ordered newest-first and paginated rather than ever truncated silently.

Ownership is always enforced in the query, never assumed from an ID in the URL: a
decision is reachable only through its alert's `user_id`, an order/position/
equity row only through its broker account's `user_id`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from signalguard.api.schemas import (
    DecisionResponse,
    EquityPoint,
    OrderResponse,
    PositionResponse,
    TradeResponse,
    money_out,
)
from signalguard.api.security import CurrentUser, DbSession
from signalguard.db.models import (
    Alert,
    BrokerAccount,
    Decision,
    EquitySnapshot,
    Order,
    Position,
    Trade,
)
from signalguard.enums import ReasonCode

router = APIRouter(prefix="/api", tags=["feed"])

# Pagination bounds. A caller can page, but cannot ask for an unbounded result
# set that would pin the database and the API process on one request.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

Limit = Annotated[int, Query(ge=1, le=_MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


@router.get("/decisions")
async def list_decisions(
    session: DbSession,
    user: CurrentUser,
    limit: Limit = _DEFAULT_LIMIT,
    offset: Offset = 0,
    reason_code: str | None = None,
    include_tests: bool = False,
) -> list[DecisionResponse]:
    """The decision feed, newest first, filterable by reason code (§11).

    Test-endpoint decisions are excluded by default: they are the user probing
    their own rules, not real signal outcomes, so mixing them into the live feed
    would distort the rejection breakdown.
    """
    stmt = (
        select(Decision)
        .join(Alert, Alert.id == Decision.alert_id)
        .where(Alert.user_id == user.id)
        .order_by(Decision.evaluated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if not include_tests:
        stmt = stmt.where(Decision.is_test.is_(False))
    if reason_code is not None:
        # Validate against the enum so a typo returns an empty list-worth of 422
        # rather than silently matching nothing and looking like "no rejections".
        try:
            code = ReasonCode(reason_code)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"unknown reason_code: {reason_code!r}",
            ) from exc
        stmt = stmt.where(Decision.reason_code == code.value)

    result = await session.execute(stmt)
    return [
        DecisionResponse(
            id=d.id,
            alert_id=d.alert_id,
            broker_account_id=d.broker_account_id,
            verdict=d.verdict,
            reason_code=d.reason_code,
            reason_detail=d.reason_detail,
            computed_qty=money_out(d.computed_qty),
            entry_reference_price=money_out(d.entry_reference_price),
            stop_price=money_out(d.stop_price),
            evaluated_at=d.evaluated_at,
            latency_ms=d.latency_ms,
            is_test=d.is_test,
        )
        for d in result.scalars().all()
    ]


@router.get("/orders")
async def list_orders(
    session: DbSession,
    user: CurrentUser,
    limit: Limit = _DEFAULT_LIMIT,
    offset: Offset = 0,
) -> list[OrderResponse]:
    stmt = (
        select(Order)
        .join(BrokerAccount, BrokerAccount.id == Order.broker_account_id)
        .where(BrokerAccount.user_id == user.id)
        .order_by(Order.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return [
        OrderResponse(
            id=o.id,
            decision_id=o.decision_id,
            symbol=o.symbol,
            side=o.side,
            type=o.type,
            role=o.role,
            qty=str(o.qty),
            price=money_out(o.price),
            stop_price=money_out(o.stop_price),
            status=o.status,
            filled_qty=str(o.filled_qty),
            avg_fill_price=money_out(o.avg_fill_price),
            fees=str(o.fees),
            submitted_at=o.submitted_at,
            filled_at=o.filled_at,
        )
        for o in result.scalars().all()
    ]


@router.get("/positions")
async def list_positions(
    session: DbSession, user: CurrentUser
) -> list[PositionResponse]:
    """Currently open positions across the user's accounts.

    A cache of broker truth, not the source of it (`db/models/position.py`): the
    reconciler overwrites this from the exchange.
    """
    stmt = (
        select(Position)
        .join(BrokerAccount, BrokerAccount.id == Position.broker_account_id)
        .where(BrokerAccount.user_id == user.id)
        .order_by(Position.symbol)
    )
    result = await session.execute(stmt)
    return [
        PositionResponse(
            symbol=p.symbol,
            qty=str(p.qty),
            avg_entry=str(p.avg_entry),
            mark_price=money_out(p.mark_price),
            unrealized_pnl=money_out(p.unrealized_pnl),
            updated_at=p.updated_at,
        )
        for p in result.scalars().all()
    ]


@router.get("/equity-curve")
async def equity_curve(
    session: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> list[EquityPoint]:
    """Equity snapshots oldest-first — the series a chart plots left to right.

    Bounded like everything else, but with a higher ceiling: a chart legitimately
    wants many points, and a snapshot row is small.
    """
    stmt = (
        select(EquitySnapshot)
        .join(BrokerAccount, BrokerAccount.id == EquitySnapshot.broker_account_id)
        .where(BrokerAccount.user_id == user.id)
        .order_by(EquitySnapshot.taken_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [
        EquityPoint(
            equity=str(e.equity),
            free_balance=money_out(e.free_balance),
            taken_at=e.taken_at,
            is_session_baseline=e.is_session_baseline,
        )
        for e in result.scalars().all()
    ]


@router.get("/trades")
async def list_trades(
    session: DbSession,
    user: CurrentUser,
    limit: Limit = _DEFAULT_LIMIT,
    offset: Offset = 0,
) -> list[TradeResponse]:
    """The trade log: closed round-trips, newest first (§11 History page).

    Distinct from `/orders` and not a prettier version of it. An order is an
    instruction; a trade is a finished round-trip with a realized number attached.
    Only trades carry PnL, and only trades are what the circuit breaker counts —
    so this is the feed that explains why a breaker tripped.
    """
    stmt = (
        select(Trade)
        .join(BrokerAccount, BrokerAccount.id == Trade.broker_account_id)
        .where(BrokerAccount.user_id == user.id)
        .order_by(Trade.closed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return [
        TradeResponse(
            id=t.id,
            symbol=t.symbol,
            side=t.side,
            qty=str(t.qty),
            entry_price=str(t.entry_price),
            exit_price=str(t.exit_price),
            realized_pnl=str(t.realized_pnl),
            fees=str(t.fees),
            opened_at=t.opened_at,
            closed_at=t.closed_at,
        )
        for t in result.scalars().all()
    ]
