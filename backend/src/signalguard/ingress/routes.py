"""Webhook endpoints (CLAUDE.md §8).

`POST /webhook/{endpoint_id}` responds fast (target <50 ms) after persisting the
alert, then evaluates and executes in the background so a slow broker can never
make the sender time out and retry — which would turn our slowness into their
duplicate.

`POST /webhook/{endpoint_id}/test` runs the identical pipeline and returns the
decision, without ever touching the broker. It is the feature users rely on most,
so it shares the real code path rather than approximating it: a test endpoint
that runs different logic tells you nothing.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard import realtime
from signalguard.config import Settings, get_settings
from signalguard.db.models import Alert, BrokerAccount, EquitySnapshot, Position
from signalguard.db.session import get_session
from signalguard.enums import OrderType, ParseStatus, ReasonCode
from signalguard.ingress import dedupe
from signalguard.ingress.auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    AuthError,
    authenticate,
    resolve_endpoint,
)
from signalguard.ingress.pipeline import (
    AccountStateProvider,
    PipelineResult,
    ReferencePriceProvider,
    evaluate_alert,
)
from signalguard.ingress.ratelimit import RateLimiter
from signalguard.ingress.schema import (
    MAX_BODY_BYTES,
    PayloadError,
    loads_decimal,
    parse_payload,
    redact_payload,
)
from signalguard.redis_client import get_redis
from signalguard.risk.types import OpenPosition
from signalguard.wiring import BrokerSession, execute_approved, notify_undelivered_exit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])

# Used when a user has no risk profile yet, matching the documented default in
# CLAUDE.md §8. Not a fallback for a *missing* answer — a user with no profile
# is rejected by the pipeline anyway; this only keeps the dedupe claim sane.
DEFAULT_DEDUPE_WINDOW_SEC = 60


async def _read_body(request: Request) -> bytes:
    """Read the body, refusing oversized ones before any parsing happens."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise PayloadError(f"body exceeds {MAX_BODY_BYTES} bytes")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise PayloadError(f"body exceeds {MAX_BODY_BYTES} bytes")
    return body


def _extract_body_secret(raw_body: bytes) -> str | None:
    """Peek at the in-body secret without committing to the full schema.

    Needed because authentication has to happen before validation — we must know
    who is talking to us before we care what they said.
    """
    try:
        data = loads_decimal(raw_body)
    except Exception:  # noqa: BLE001 - unparseable body simply has no secret
        return None
    if isinstance(data, dict):
        value = data.get("secret")
        return str(value) if value is not None else None
    return None


# What `/test` assumes when it has nothing better. Matches the worked example in
# CLAUDE.md §11 so the numbers a user sees here line up with the documentation.
DEFAULT_SIM_EQUITY = Decimal("10000")


def _simulated_state_provider(
    session: AsyncSession, equity_override: Decimal | None
) -> AccountStateProvider:
    """Account state for `/test`, assembled **without contacting the broker**.

    §8 requires `/test` to run the full pipeline without touching the broker,
    and the original reading of that was "hand the engine zeros". That was
    technically compliant and practically useless: zero equity means a zero risk
    budget, so sizing rounded to nothing and **no signal could ever come back
    APPROVED** — the endpoint could only ever show you failures (F-1).

    Reading our *own* tables is not touching the broker. The reconciler keeps
    `equity_snapshots` and `positions` current, so this reflects the account as
    of the last cycle — real numbers, one cycle stale, and no network call. When
    there is no cached state yet (a brand-new account), it falls back to an
    explicit override or a documented default, and the response says which.
    """

    async def provider(
        account: BrokerAccount,
    ) -> tuple[Decimal, Decimal, tuple[OpenPosition, ...]]:
        latest = (
            await session.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.broker_account_id == account.id)
                .order_by(EquitySnapshot.taken_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        rows = (
            await session.execute(
                select(Position).where(
                    Position.broker_account_id == account.id, Position.qty != 0
                )
            )
        ).scalars().all()
        positions = tuple(
            OpenPosition(
                symbol=p.symbol,
                qty=p.qty,
                avg_entry=p.avg_entry,
                mark_price=p.mark_price if p.mark_price is not None else p.avg_entry,
            )
            for p in rows
        )

        if equity_override is not None:
            equity = free = equity_override
        elif latest is not None and latest.equity > 0:
            equity = latest.equity
            free = latest.free_balance if latest.free_balance is not None else latest.equity
        else:
            equity = free = DEFAULT_SIM_EQUITY

        return equity, free, positions

    return provider


def _simulated_price_provider(
    session: AsyncSession, price_override: Decimal | None
) -> ReferencePriceProvider:
    """Reference price for `/test`, again without contacting the broker.

    A MARKET order carries no price of its own, so with nothing here rule 6
    rejected every market-order test with `NO_STOP_LOSS — no reference price
    available`. Accurate in its detail and badly misleading in its code: it says
    "your stop is wrong" when the stop was fine (F-1).

    Order of preference: an explicit override, then the last mark price the
    reconciler cached for that symbol. Still None if we have neither, which is
    honest — and the endpoint now says so in plain words rather than blaming the
    stop.
    """

    async def provider(account: BrokerAccount, symbol: str) -> Decimal | None:
        if price_override is not None:
            return price_override
        cached = (
            await session.execute(
                select(Position.mark_price).where(
                    Position.broker_account_id == account.id,
                    Position.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        return cached

    return provider


async def _dedupe_window_for(session: AsyncSession, user_id: uuid.UUID) -> int:
    """The user's configured dedupe window, or the documented default.

    Read here rather than assumed, because rule 4's rejection message quotes
    `dedupe_window_sec` back at the user — a hardcoded 60 alongside a message
    claiming otherwise is a risk parameter that silently does nothing.
    """
    from signalguard.db.models import RiskProfile

    result = await session.execute(
        select(RiskProfile.dedupe_window_sec).where(RiskProfile.user_id == user_id)
    )
    window = result.scalar_one_or_none()
    return int(window) if window is not None else DEFAULT_DEDUPE_WINDOW_SEC


async def _handle(
    request: Request,
    endpoint_id: str,
    session: AsyncSession,
    settings: Settings,
    *,
    is_test: bool,
) -> tuple[int, dict[str, Any], Alert | None, dict[str, Any] | None, str | None, bool]:
    """Shared path for the live and test endpoints.

    Returns (status, body, alert, payload_data, payload_error, is_duplicate).
    """
    now = datetime.now(UTC)
    redis = get_redis()

    # 1. Resolve and authenticate before doing anything expensive.
    try:
        endpoint = await resolve_endpoint(session, endpoint_id, settings)
    except AuthError:
        # 404 rather than 401: an unknown endpoint should not confirm that some
        # other endpoint exists.
        return status.HTTP_404_NOT_FOUND, {"detail": "not found"}, None, None, None, False

    limiter = RateLimiter(redis)
    if not await limiter.allow(endpoint.endpoint_id_hash, int(now.timestamp() * 1000)):
        return (
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"detail": "rate limit exceeded"},
            None, None, None, False,
        )

    try:
        raw_body = await _read_body(request)
    except PayloadError as exc:
        return (
            status.HTTP_413_CONTENT_TOO_LARGE,
            {"detail": str(exc)},
            None, None, None, False,
        )

    try:
        auth = authenticate(
            endpoint=endpoint,
            raw_body=raw_body,
            signature=request.headers.get(SIGNATURE_HEADER),
            signature_timestamp=request.headers.get(TIMESTAMP_HEADER),
            body_secret=_extract_body_secret(raw_body),
            settings=settings,
            now=now,
        )
    except AuthError as exc:
        logger.warning(
            "Webhook authentication failed",
            extra={"reason": str(exc), "endpoint_hash": endpoint.endpoint_id_hash[:12]},
        )
        return status.HTTP_401_UNAUTHORIZED, {"detail": "unauthorized"}, None, None, None, False

    # 2. Parse. A failure here is recorded, not discarded — an unparseable alert
    #    is exactly the kind of thing a user needs to see on their dashboard.
    payload_data: dict[str, Any] | None = None
    payload_error: str | None = None
    parse_status = ParseStatus.OK
    try:
        payload = parse_payload(raw_body)
        payload_data = payload.model_dump()
    except PayloadError as exc:
        payload_error = str(exc)
        parse_status = ParseStatus.SCHEMA_INVALID

    # 3. Dedupe key, then the atomic claim in Redis.
    if payload_data is not None:
        dedupe_key = dedupe.compute_dedupe_key(
            endpoint_id,
            payload_data.get("id"),
            str(payload_data["symbol"]),
            str(payload_data["action"]),
            payload_data["timestamp"],
        )
    else:
        # Unparseable bodies still need a stable key so identical retries of the
        # same broken payload do not each create an alert row.
        dedupe_key = hashlib.sha256(
            f"{endpoint_id}:unparseable:".encode() + raw_body
        ).hexdigest()

    is_duplicate = False
    try:
        window = await _dedupe_window_for(session, endpoint.user_id)
        claimed = await dedupe.claim_dedupe_key(redis, dedupe_key, window)
        is_duplicate = not claimed
    except dedupe.StateUnavailableError:
        # Redis is down. We cannot guarantee "exactly once", and an unknown
        # answer is never permission (plan §3, Q5).
        return (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"detail": "state store unavailable", "reason_code": ReasonCode.STATE_UNAVAILABLE},
            None, None, None, False,
        )

    # 4. Persist the alert BEFORE evaluating. If the process dies one line from
    #    here, the record still exists (constraint #5).
    raw_dict = payload_data if payload_data is not None else {}
    alert = Alert(
        id=uuid.uuid4(),
        user_id=endpoint.user_id,
        webhook_endpoint_id=endpoint.id,
        raw_payload=_json_safe(redact_payload(dict(raw_dict))),
        raw_body_sha256=hashlib.sha256(raw_body).digest(),
        content_length=len(raw_body),
        source_ip=request.client.host if request.client else None,
        received_at=now,
        dedupe_key=dedupe_key,
        parse_status=parse_status.value,
        auth_mode=auth.mode.value,
        signature_valid=auth.signature_valid,
    )
    session.add(alert)
    await session.flush()

    return status.HTTP_200_OK, {}, alert, payload_data, payload_error, is_duplicate


def _json_safe(data: dict[str, Any]) -> dict[str, Any]:
    """Make a payload JSON-serialisable without losing Decimal precision."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "value"):  # StrEnum
            out[key] = value.value
        else:
            out[key] = value
    return out


@router.post("/webhook/{endpoint_id}")
async def receive_webhook(
    endpoint_id: str,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Accept a trading signal.

    Responds as soon as the alert is durably recorded. Risk evaluation and
    execution run in the background, so a slow broker cannot make the sender
    time out and retry.
    """
    code, body, alert, payload_data, payload_error, is_duplicate = await _handle(
        request, endpoint_id, session, settings, is_test=False
    )
    if alert is None:
        response.status_code = code
        return body

    await session.commit()

    background.add_task(
        _evaluate_in_background,
        alert_id=alert.id,
        payload_data=payload_data,
        payload_error=payload_error,
        is_duplicate=is_duplicate,
    )
    return {"status": "accepted", "alert_id": str(alert.id)}


@router.post("/webhook/{endpoint_id}/test")
async def test_webhook(
    endpoint_id: str,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    equity: Annotated[Decimal | None, Query(gt=0)] = None,
    price: Annotated[Decimal | None, Query(gt=0)] = None,
) -> dict[str, Any]:
    """Run the full risk pipeline and return the decision. Never touches the broker.

    First-class, not a debug hook (§8): it is how a user finds out their stop is
    on the wrong side *before* a live signal does it for them.

    `equity` and `price` let you ask a what-if — "with $10,000 and BTC at
    $62,000, what would this signal do?" — which is the same question the
    Risk-profile page's live preview answers (§11). Omit them and the endpoint
    uses the account's last reconciled state, which is real data and involves no
    network call. Whatever it used is echoed back under `assumptions`, so a
    number on screen is never unexplained.
    """
    code, body, alert, payload_data, payload_error, is_duplicate = await _handle(
        request, endpoint_id, session, settings, is_test=True
    )
    if alert is None:
        response.status_code = code
        return body

    state_provider = _simulated_state_provider(session, equity)
    result = await evaluate_alert(
        session,
        alert=alert,
        payload_data=payload_data,
        payload_error=payload_error,
        is_duplicate=is_duplicate,
        account_state_provider=state_provider,
        reference_price_provider=_simulated_price_provider(session, price),
        now=datetime.now(UTC),
        is_test=True,
    )
    await session.commit()

    price_provider = _simulated_price_provider(session, price)
    assumptions = await _describe_assumptions(
        result, state_provider, price_provider, equity, price
    )

    return {
        "verdict": result.decision.verdict.value,
        "reason_code": result.decision.reason_code.value,
        "reason_detail": result.decision.reason_detail,
        "computed_qty": (
            str(result.decision.computed_qty)
            if result.decision.computed_qty is not None
            else None
        ),
        "latency_ms": result.latency_ms,
        "alert_id": str(alert.id),
        "assumptions": assumptions,
    }


async def _describe_assumptions(
    result: PipelineResult,
    state_provider: AccountStateProvider,
    price_provider: ReferencePriceProvider,
    equity_override: Decimal | None,
    price_override: Decimal | None,
) -> dict[str, Any]:
    """Explain the inputs `/test` used, so no number on screen is unexplained.

    A simulator that will not show its inputs is one you cannot trust: "0.161
    BTC" means nothing until you know what equity and price produced it.

    The values are re-derived from the same providers the evaluation used rather
    than read back off the decision, because a *rejected* decision often never
    records them — and those are exactly the runs a user is trying to understand.
    """
    account = result.account
    if account is None:
        return {"note": "evaluation stopped before account state was needed"}

    equity, free_balance, positions = await state_provider(account)
    used_default = equity_override is None and equity == DEFAULT_SIM_EQUITY

    if equity_override is not None:
        equity_source = "your ?equity= override"
    elif used_default:
        equity_source = (
            f"an assumed default of {DEFAULT_SIM_EQUITY} — this account has no "
            "reconciled equity yet. Pass ?equity= to test against your own number."
        )
    else:
        equity_source = "your account's last reconciled equity (no broker call)"

    assumptions: dict[str, Any] = {
        "equity": str(equity),
        "free_balance": str(free_balance),
        "open_positions": len(positions),
        "equity_source": equity_source,
        "simulated": True,
        "note": "No broker was contacted — §8 requires this endpoint never to.",
    }

    alert_input = result.alert_input
    reference: Decimal | None = None
    if alert_input is not None:
        if alert_input.order_type is OrderType.LIMIT:
            reference = alert_input.limit_price
            price_source = "the alert's own limit_price"
        else:
            reference = await price_provider(account, alert_input.symbol)
            price_source = (
                "your ?price= override" if price_override is not None
                else "the last mark price cached for this symbol"
            )
    else:
        price_source = "not reached"

    if reference is None and alert_input is not None:
        # The specific case that used to masquerade as NO_STOP_LOSS.
        price_source = (
            "none available — a MARKET order carries no price of its own and "
            "this account has no cached mark price for the symbol. Pass ?price= "
            "to test a market order, or send order_type=limit with a limit_price."
        )

    assumptions["reference_price"] = str(reference) if reference is not None else None
    assumptions["price_source"] = price_source
    return assumptions


async def _evaluate_in_background(
    *,
    alert_id: uuid.UUID,
    payload_data: dict[str, Any] | None,
    payload_error: str | None,
    is_duplicate: bool,
) -> None:
    """Evaluate an already-persisted alert, then execute it if it was approved.

    Runs on its own session, because the request's session is closed by the time
    this executes. Any failure is logged and swallowed — the alert row already
    exists, so the audit trail is intact, and there is no client left to tell.

    **Ordering matters and is not negotiable.** The decision is committed before
    a single order is submitted (constraint #5): if the process dies between the
    two, the audit trail records what we decided and the reconciler discovers
    whatever reached the exchange. The reverse order would allow an order with no
    decision behind it, which is unauditable by construction.
    """
    from signalguard.db.session import get_session as session_factory

    settings = get_settings()
    broker_session = BrokerSession(settings.credentials_master_key)

    try:
        async for session in session_factory():
            result = await session.execute(select(Alert).where(Alert.id == alert_id))
            alert = result.scalar_one_or_none()
            if alert is None:
                return

            evaluation = await evaluate_alert(
                session,
                alert=alert,
                payload_data=payload_data,
                payload_error=payload_error,
                is_duplicate=is_duplicate,
                account_state_provider=broker_session.account_state,
                reference_price_provider=broker_session.reference_price,
                now=datetime.now(UTC),
                is_test=False,
            )
            await session.commit()

            # Publish to the dashboard only after the decision is durable. A
            # pub/sub failure here is swallowed (see realtime.publish): the row
            # is committed, and the live feed is a convenience on top of it.
            await _publish_decision(alert.user_id, alert.id, evaluation)

            # Hand off to the composition root, which owns the execution side.
            # This layer never learns what a broker is (CLAUDE.md §5).
            await execute_approved(
                session, broker_session, evaluation, alert.user_id, get_redis()
            )

            # A rejected *exit* leaves the user still holding the position they
            # asked to leave, so it is escalated rather than merely recorded
            # (plan §3, Q2).
            if payload_data is not None:
                await notify_undelivered_exit(
                    session, evaluation, str(payload_data.get("action", ""))
                )
    except Exception:
        logger.exception(
            "Background risk evaluation failed", extra={"alert_id": str(alert_id)}
        )
    finally:
        await broker_session.aclose()


async def _publish_decision(
    user_id: uuid.UUID, alert_id: uuid.UUID, evaluation: PipelineResult
) -> None:
    """Fan the freshly-made decision out to the user's live dashboard feed."""
    decision = evaluation.decision
    await realtime.publish(
        get_redis(),
        user_id,
        realtime.EventType.DECISION,
        {
            "decision_id": evaluation.decision_id,
            "alert_id": alert_id,
            "verdict": decision.verdict,
            "reason_code": decision.reason_code,
            "reason_detail": decision.reason_detail,
            "computed_qty": decision.computed_qty,
            "latency_ms": evaluation.latency_ms,
        },
    )
