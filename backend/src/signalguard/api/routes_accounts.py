"""Broker-account and webhook-endpoint CRUD (CLAUDE.md §11 page 4, §12).

Two credential-bearing resources live here, and both obey the same rule: the
secret goes in encrypted and never comes back out.

* **Broker accounts** hold the exchange API key/secret, envelope-encrypted with
  AES-GCM (§12). The plaintext exists only in the request body and, later, only
  in memory at the moment an order is placed — never in a response, never in a
  log.
* **Webhook endpoints** are the per-user URL a signal source posts to. The
  endpoint token is a credential (`db/models/webhook.py`), so only its hash is
  stored; the token and the signing secrets are shown exactly once, at creation.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from signalguard.api.schemas import (
    BrokerAccountCreate,
    BrokerAccountResponse,
    WebhookEndpointCreated,
    WebhookEndpointResponse,
)
from signalguard.api.security import AppSettings, CurrentUser, DbSession
from signalguard.crypto import (
    encrypt_credential,
    generate_endpoint_id,
    generate_webhook_secret,
    hash_endpoint_id,
)
from signalguard.db.models import BrokerAccount, WebhookEndpoint

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["accounts"])


def _account_response(account: BrokerAccount) -> BrokerAccountResponse:
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


# --- Broker accounts ----------------------------------------------------------


@router.post("/broker-accounts", status_code=status.HTTP_201_CREATED)
async def create_broker_account(
    body: BrokerAccountCreate,
    session: DbSession,
    user: CurrentUser,
    settings: AppSettings,
) -> BrokerAccountResponse:
    """Store an exchange credential, encrypted at rest.

    `is_testnet=True` is forced, matching the database CHECK that refuses a
    non-testnet account: live trading is not authorised in this phase, and the
    API is not a way around that (constraint #2).
    """
    # The two secrets are serialised together and encrypted as one blob, so a
    # single (ciphertext, nonce) pair covers the whole credential.
    plaintext = json.dumps({"api_key": body.api_key, "api_secret": body.api_secret})
    ciphertext, nonce = encrypt_credential(plaintext, settings.credentials_master_key)

    account = BrokerAccount(
        id=uuid.uuid4(),
        user_id=user.id,
        broker=body.broker,
        label=body.label,
        encrypted_credentials=ciphertext,
        credentials_nonce=nonce,
        is_testnet=True,
        is_active=True,
    )
    session.add(account)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # The UNIQUE (user_id, label) fired: labels route webhook signals, so a
        # duplicate would make routing a coin flip (`db/models/broker.py`).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a broker account labelled {body.label!r} already exists",
        ) from exc

    await session.commit()
    logger.info("Broker account created", extra={"broker_account_id": str(account.id)})
    return _account_response(account)


@router.get("/broker-accounts")
async def list_broker_accounts(
    session: DbSession, user: CurrentUser
) -> list[BrokerAccountResponse]:
    result = await session.execute(
        select(BrokerAccount)
        .where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at)
    )
    return [_account_response(a) for a in result.scalars().all()]


@router.delete("/broker-accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_account(
    account_id: uuid.UUID, session: DbSession, user: CurrentUser
) -> None:
    """Delete a broker account.

    Refused while the account is LOCKED: a locked account may still hold a
    position the reconciler is flattening, and deleting it would orphan that
    work. Unlock (a deliberate human act) before removing it.
    """
    account = await _owned_account(session, user.id, account_id)
    from signalguard.enums import TradingState

    if account.trading_state == TradingState.LOCKED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="account is locked; unlock it before deleting",
        )
    await session.delete(account)
    await session.commit()


async def _owned_account(
    session: DbSession, user_id: uuid.UUID, account_id: uuid.UUID
) -> BrokerAccount:
    """Fetch an account, 404 unless it exists and belongs to this user.

    404 (not 403) for someone else's account: confirming that an ID exists but is
    not yours still leaks that it exists.
    """
    result = await session.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id, BrokerAccount.user_id == user_id
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="broker account not found"
        )
    return account


# --- Webhook endpoints --------------------------------------------------------


@router.post("/webhook-endpoints", status_code=status.HTTP_201_CREATED)
async def create_webhook_endpoint(
    session: DbSession, user: CurrentUser, settings: AppSettings
) -> WebhookEndpointCreated:
    """Mint a webhook endpoint and return its token + secrets exactly once.

    After this response the server keeps only a *hash* of the token and
    *encrypted* copies of the secrets, so it genuinely cannot show them again.
    The caller must store them now.
    """
    token = generate_endpoint_id()
    hmac_secret = generate_webhook_secret()
    body_secret = generate_webhook_secret()

    hmac_ct, hmac_nonce = encrypt_credential(hmac_secret, settings.credentials_master_key)
    body_ct, body_nonce = encrypt_credential(body_secret, settings.credentials_master_key)

    endpoint = WebhookEndpoint(
        id=uuid.uuid4(),
        user_id=user.id,
        endpoint_id_hash=hash_endpoint_id(token, settings.endpoint_id_pepper),
        hmac_secret_encrypted=hmac_ct,
        hmac_secret_nonce=hmac_nonce,
        body_secret_encrypted=body_ct,
        body_secret_nonce=body_nonce,
        is_active=True,
    )
    session.add(endpoint)
    await session.commit()
    logger.info("Webhook endpoint created", extra={"webhook_endpoint_id": str(endpoint.id)})

    return WebhookEndpointCreated(
        id=endpoint.id,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        last_used_at=endpoint.last_used_at,
        endpoint_token=token,
        hmac_secret=hmac_secret,
        body_secret=body_secret,
    )


@router.get("/webhook-endpoints")
async def list_webhook_endpoints(
    session: DbSession, user: CurrentUser
) -> list[WebhookEndpointResponse]:
    result = await session.execute(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.user_id == user.id)
        .order_by(WebhookEndpoint.created_at)
    )
    return [
        WebhookEndpointResponse(
            id=e.id,
            is_active=e.is_active,
            created_at=e.created_at,
            last_used_at=e.last_used_at,
        )
        for e in result.scalars().all()
    ]
