"""Session authentication for the dashboard API (CLAUDE.md §3, §12).

The webhook layer authenticates *machines* with HMAC over a raw body. This layer
authenticates *humans* with a login session, and the two must not be confused: a
session cookie can move money through the dashboard, so it is treated as a
credential end to end.

Design decisions that matter:

* **Server-side sessions, not stateless JWTs.** An account that can fire a kill
  switch must be able to *revoke* a session the instant a laptop is lost. A
  signed stateless token cannot be revoked before it expires; a row we can set
  `revoked_at` on can (`db/models/user.py`).
* **Only the hash of the token is stored.** A stolen database must not yield
  working sessions, exactly as for webhook endpoint tokens.
* **`Secure` follows the environment.** The cookie is `HttpOnly` + `SameSite=Lax`
  always, and `Secure` in staging/production. It is not `Secure` under `local`/
  `test` only so the ASGI test client (plain http) can round-trip it — never in
  an environment that faces the network.
"""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.config import Settings, get_settings
from signalguard.crypto import generate_session_token, hash_session_token
from signalguard.db.models import Session as SessionModel
from signalguard.db.models import User
from signalguard.db.session import get_session

SESSION_COOKIE_NAME = "sg_session"


def _client_ip(request: Request) -> str | None:
    """Return the client IP only if it is a real address.

    The `sessions.ip` column is Postgres `INET`, so a non-address host — a test
    client's "testclient", or a hostname a proxy might pass — must be dropped
    rather than handed to the database, which would reject the whole insert.
    """
    if request.client is None:
        return None
    try:
        ipaddress.ip_address(request.client.host)
    except ValueError:
        return None
    return request.client.host

# How long a login lasts before it must be re-established. Long enough not to
# nag a working trader, short enough that an abandoned session does not live
# forever.
SESSION_TTL = timedelta(days=14)


async def create_session(
    session: AsyncSession,
    response: Response,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    request: Request,
) -> None:
    """Mint a session row and set the cookie. The plaintext token exists only here.

    The row stores the token's hash; the cookie carries the token itself. They
    meet again only when a later request is authenticated.
    """
    token = generate_session_token()
    now = datetime.now(UTC)
    session.add(
        SessionModel(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=hash_session_token(token),
            expires_at=now + SESSION_TTL,
            user_agent=request.headers.get("user-agent"),
            ip=_client_ip(request),
        )
    )
    await session.flush()

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.app_env in ("staging", "production"),
        samesite="lax",
        path="/",
    )


async def revoke_session(session: AsyncSession, response: Response, token: str) -> None:
    """Mark the current session revoked and clear the cookie. Idempotent."""
    result = await session.execute(
        select(SessionModel).where(SessionModel.token_hash == hash_session_token(token))
    )
    row = result.scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.flush()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


async def authenticate_token(session: AsyncSession, token: str | None) -> User | None:
    """Resolve a session token to its user, or None.

    Fail closed at every branch: no token, unknown token, revoked, expired, or a
    deactivated user all resolve to None. Shared by the HTTP dependency and the
    WebSocket handshake so both judge a session by exactly the same rules.
    """
    if not token:
        return None

    result = await session.execute(
        select(SessionModel).where(
            SessionModel.token_hash == hash_session_token(token)
        )
    )
    login = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if login is None or login.revoked_at is not None or login.expires_at <= now:
        return None

    user_result = await session.execute(select(User).where(User.id == login.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    sg_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> User:
    """Resolve the logged-in user, or 401. An ambiguous session is never valid."""
    user = await authenticate_token(session, sg_session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "cookie"},
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]
