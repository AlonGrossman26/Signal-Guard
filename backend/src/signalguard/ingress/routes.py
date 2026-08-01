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

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard import realtime
from signalguard.config import Settings, get_settings
from signalguard.db.models import Alert
from signalguard.db.session import get_session
from signalguard.enums import ParseStatus, ReasonCode
from signalguard.ingress import dedupe
from signalguard.ingress.auth import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    AuthError,
    authenticate,
    resolve_endpoint,
)
from signalguard.ingress.pipeline import PipelineResult, evaluate_alert
from signalguard.ingress.ratelimit import RateLimiter
from signalguard.ingress.schema import (
    MAX_BODY_BYTES,
    PayloadError,
    loads_decimal,
    parse_payload,
    redact_payload,
)
from signalguard.redis_client import get_redis
from signalguard.wiring import BrokerSession, execute_approved

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


async def _no_broker_state(_account: Any) -> tuple[Decimal, Decimal, tuple[()]]:
    """State provider for the `/test` endpoint, which never touches the broker.

    §8 is explicit that `/test` runs the full risk pipeline **without touching
    the broker**, so there is no account state to fetch and this is not a stub —
    it is the correct behaviour for that endpoint. The live path uses
    `BrokerSession` instead.

    It returns zeroed equity rather than raising, so the whole rule chain still
    runs and the user sees which rule their payload trips. What it cannot do is
    produce a realistic size, so `/test` reports sizing against zero equity — the
    dashboard's Risk-profile page is where a user previews real sizing.
    """
    return Decimal("0"), Decimal("0"), ()


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
) -> dict[str, Any]:
    """Run the full risk pipeline and return the decision. Never touches the broker.

    First-class, not a debug hook: it is how a user finds out their stop is on
    the wrong side *before* a live signal does it for them.
    """
    code, body, alert, payload_data, payload_error, is_duplicate = await _handle(
        request, endpoint_id, session, settings, is_test=True
    )
    if alert is None:
        response.status_code = code
        return body

    result = await evaluate_alert(
        session,
        alert=alert,
        payload_data=payload_data,
        payload_error=payload_error,
        is_duplicate=is_duplicate,
        account_state_provider=_no_broker_state,
        now=datetime.now(UTC),
        is_test=True,
    )
    await session.commit()

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
    }


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
