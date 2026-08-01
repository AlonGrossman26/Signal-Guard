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

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhook"])


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


async def _null_account_state(
    _account: Any,
) -> tuple[Decimal, Decimal, tuple[()]]:
    """Placeholder broker state provider until the adapter lands in Phase 4.

    Returns zeroed state rather than raising, so the pipeline exercises its full
    path. Phase 4 replaces this with the real Binance testnet adapter.
    """
    return Decimal("0"), Decimal("0"), ()


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
        window = 60
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
        account_state_provider=_null_account_state,
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
    """Evaluate an already-persisted alert.

    Runs on its own session, because the request's session is closed by the time
    this executes. Any failure is logged and swallowed — the alert row already
    exists, so the audit trail is intact, and there is no client left to tell.
    """
    from sqlalchemy import select

    from signalguard.db.session import get_session as session_factory

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
                account_state_provider=_null_account_state,
                now=datetime.now(UTC),
                is_test=False,
            )
            await session.commit()
            # Publish to the dashboard only after the decision is durable. A
            # pub/sub failure here is swallowed (see realtime.publish): the row
            # is committed, and the live feed is a convenience on top of it.
            await _publish_decision(alert.user_id, alert.id, evaluation)
    except Exception:
        logger.exception("Background risk evaluation failed", extra={"alert_id": str(alert_id)})


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
