"""Risk-profile read and update (CLAUDE.md §7, §11 page 2).

Editing a profile bumps its `version`. That version is copied into every future
decision's `rule_snapshot`, so a past decision always shows the rules that
actually produced it — changing a setting today cannot rewrite yesterday's audit
trail, and the change applies only from the next decision onward, never
retroactively to an open position (plan §3, Q4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from signalguard.api.schemas import RiskProfileResponse, RiskProfileUpdate
from signalguard.api.security import CurrentUser, DbSession
from signalguard.db.models import RiskProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/risk-profile", tags=["risk-profile"])


def _to_response(profile: RiskProfile) -> RiskProfileResponse:
    return RiskProfileResponse(
        version=profile.version,
        max_alert_age_sec=profile.max_alert_age_sec,
        future_tolerance_sec=profile.future_tolerance_sec,
        dedupe_window_sec=profile.dedupe_window_sec,
        allowed_symbols=list(profile.allowed_symbols),
        min_stop_distance_pct=str(profile.min_stop_distance_pct),
        consecutive_loss_threshold=profile.consecutive_loss_threshold,
        circuit_breaker_cooldown_minutes=profile.circuit_breaker_cooldown_minutes,
        circuit_breaker_manual_reset=profile.circuit_breaker_manual_reset,
        max_daily_dd_pct=str(profile.max_daily_dd_pct),
        risk_per_trade_pct=str(profile.risk_per_trade_pct),
        fee_slippage_buffer_bps=profile.fee_slippage_buffer_bps,
        max_notional_per_trade=str(profile.max_notional_per_trade),
        max_open_positions=profile.max_open_positions,
        max_total_notional=str(profile.max_total_notional),
        allow_pyramiding=profile.allow_pyramiding,
        daily_reset_time=profile.daily_reset_time.strftime("%H:%M"),
        timezone=profile.timezone,
        updated_at=profile.updated_at,
    )


async def _load_profile(session: DbSession, user: CurrentUser) -> RiskProfile:
    result = await session.execute(
        select(RiskProfile).where(RiskProfile.user_id == user.id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        # Every user gets a profile at registration, so a missing one is a real
        # inconsistency, not a normal 404 the client should paper over.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="risk profile not found"
        )
    return profile


@router.get("")
async def get_risk_profile(session: DbSession, user: CurrentUser) -> RiskProfileResponse:
    return _to_response(await _load_profile(session, user))


@router.put("")
async def update_risk_profile(
    body: RiskProfileUpdate, session: DbSession, user: CurrentUser
) -> RiskProfileResponse:
    """Apply a partial update. Only the fields sent are changed; version bumps.

    An empty body is rejected: a PUT that changes nothing is almost always a
    client bug, and silently bumping the version for it would pollute the audit
    trail with no-op edits.
    """
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="no fields to update"
        )

    profile = await _load_profile(session, user)
    for field, value in changes.items():
        setattr(profile, field, value)
    profile.version += 1

    await session.commit()
    await session.refresh(profile)
    logger.info(
        "Risk profile updated",
        extra={"user_id": str(user.id), "version": profile.version,
               "fields": sorted(changes)},
    )
    return _to_response(profile)
