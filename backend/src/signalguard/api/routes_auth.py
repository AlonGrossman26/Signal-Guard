"""Authentication endpoints: register, login, logout, whoami.

Registration creates the user *and* their default risk profile in one
transaction. That default profile has an empty `allowed_symbols`, so a brand-new
account trades nothing until the user opts a symbol in — the fail-closed default
is deny, not allow (`db/models/risk_profile.py`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from signalguard.api.schemas import (
    LoginRequest,
    RegisterRequest,
    UserResponse,
)
from signalguard.api.security import (
    SESSION_COOKIE_NAME,
    AppSettings,
    CurrentUser,
    DbSession,
    create_session,
    revoke_session,
)
from signalguard.crypto import hash_password, verify_password
from signalguard.db.models import RiskProfile, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> UserResponse:
    """Create an account, its default risk profile, and a logged-in session."""
    user = User(
        id=uuid.uuid4(),
        email=body.email,
        password_hash=hash_password(body.password),
    )
    session.add(user)
    try:
        # Flush the user before the profile: there is no ORM relationship between
        # them, so the unit of work cannot infer that users must be inserted first
        # — we order it explicitly, or the profile's FK has nothing to point at.
        await session.flush()
        session.add(RiskProfile(id=uuid.uuid4(), user_id=user.id))
        await session.flush()
    except IntegrityError as exc:
        # The UNIQUE on email fired. Do not reveal whether the address exists —
        # "email or password" style ambiguity applies to registration too.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="could not create account"
        ) from exc

    await create_session(session, response, settings, user_id=user.id, request=request)
    await session.commit()
    logger.info("User registered", extra={"user_id": str(user.id)})
    return UserResponse(id=user.id, email=user.email, created_at=user.created_at)


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> UserResponse:
    """Exchange email + password for a session cookie.

    A wrong email and a wrong password return the identical 401, and the password
    hash is verified even when the user does not exist, so response timing does
    not leak which addresses are registered.
    """
    result = await session.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    # Constant-ish work whether or not the user exists: verify against the found
    # hash, or against a throwaway to keep the timing similar.
    password_ok = verify_password(
        body.password,
        user.password_hash if user is not None else _DUMMY_HASH,
    )
    if user is None or not user.is_active or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        )

    await create_session(session, response, settings, user_id=user.id, request=request)
    await session.commit()
    return UserResponse(id=user.id, email=user.email, created_at=user.created_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    session: DbSession,
    sg_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> None:
    """Revoke the current session. Safe to call without one."""
    if sg_session:
        await revoke_session(session, response, sg_session)
        await session.commit()
    else:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me")
async def whoami(user: CurrentUser) -> UserResponse:
    """The current session's user. 401 if not logged in."""
    return UserResponse(id=user.id, email=user.email, created_at=user.created_at)


# A precomputed Argon2 hash of a random string. Verifying a wrong password
# against this keeps the "user not found" path roughly as slow as the real one,
# so login timing does not reveal which emails exist.
_DUMMY_HASH = hash_password("signalguard-timing-equaliser-not-a-real-password")
