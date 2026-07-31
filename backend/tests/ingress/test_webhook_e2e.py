"""End-to-end webhook tests against a live Postgres and Redis.

The phase gate for Phase 3 is "duplicate alert -> 1 decision", and that cannot be
proved with mocks: the guarantee is produced by Redis's atomic SET NX and the
database's partial unique index working together. Faking either would test the
fake.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from signalguard.crypto import (
    compute_hmac,
    encrypt_credential,
    generate_endpoint_id,
    hash_endpoint_id,
)
from signalguard.ingress.auth import SIGNATURE_HEADER, TIMESTAMP_HEADER

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

MASTER_KEY = base64.b64encode(secrets.token_bytes(32)).decode()
PEPPER = secrets.token_urlsafe(32)
HMAC_SECRET = "test-hmac-secret"
BODY_SECRET = "test-body-secret"


@pytest.fixture()
async def app_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Boot the real app against real dependencies."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("ENDPOINT_ID_PEPPER", PEPPER)
    monkeypatch.setenv("SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("APP_ENV", "test")

    from signalguard.config import get_settings
    from signalguard.db.session import dispose_engine, init_engine
    from signalguard.redis_client import close_redis, init_redis

    get_settings.cache_clear()
    init_engine(DATABASE_URL)
    init_redis(REDIS_URL)

    from signalguard.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await dispose_engine()
    await close_redis()
    get_settings.cache_clear()


async def _seed_user_with_endpoint(allowed_symbols: list[str] | None = None) -> str:
    """Create a user, profile, broker account and webhook endpoint. Returns the token."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    endpoint_token = generate_endpoint_id()
    user_id = uuid.uuid4()
    hmac_ct, hmac_nonce = encrypt_credential(HMAC_SECRET, MASTER_KEY)
    body_ct, body_nonce = encrypt_credential(BODY_SECRET, MASTER_KEY)

    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, email, password_hash, is_active) "
                "VALUES (:id, :email, 'x', true)"
            ),
            {"id": user_id, "email": f"{user_id}@example.test"},
        )
        await s.execute(
            text(
                "INSERT INTO risk_profiles (id, user_id, allowed_symbols, "
                " max_notional_per_trade, max_total_notional) "
                "VALUES (:id, :user_id, :symbols, 100000, 1000000)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "symbols": allowed_symbols if allowed_symbols is not None else ["BTCUSDT"],
            },
        )
        await s.execute(
            text(
                "INSERT INTO broker_accounts (id, user_id, broker, label, "
                " encrypted_credentials, credentials_nonce, is_testnet, is_active, "
                " trading_state) "
                "VALUES (:id, :user_id, 'binance_spot_testnet', 'binance-testnet-1', "
                "        '\\x00'::bytea, '\\x00'::bytea, true, true, 'ACTIVE')"
            ),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO instruments (broker, symbol, base_asset, quote_asset, "
                " tick_size, lot_step, min_qty, min_notional, status, fetched_at) "
                "VALUES ('binance_spot_testnet', 'BTCUSDT', 'BTC', 'USDT', "
                "        0.01, 0.00001, 0.00001, 10, 'TRADING', now()) "
                "ON CONFLICT (broker, symbol) DO UPDATE SET fetched_at = now()"
            )
        )
        await s.execute(
            text(
                "INSERT INTO webhook_endpoints (id, user_id, endpoint_id_hash, "
                " hmac_secret_encrypted, hmac_secret_nonce, body_secret_encrypted, "
                " body_secret_nonce, is_active) "
                "VALUES (:id, :user_id, :hash, :hc, :hn, :bc, :bn, true)"
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user_id,
                "hash": hash_endpoint_id(endpoint_token, PEPPER),
                "hc": hmac_ct, "hn": hmac_nonce,
                "bc": body_ct, "bn": body_nonce,
            },
        )
        await s.commit()

    await engine.dispose()
    return endpoint_token


def make_body(**overrides: object) -> bytes:
    payload: dict[str, Any] = {
        "id": f"signal-{uuid.uuid4()}",
        "timestamp": datetime.now(UTC).isoformat(),
        "account": "binance-testnet-1",
        "symbol": "BTCUSDT",
        "action": "buy",
        "order_type": "limit",
        "limit_price": "62000.00",
        "stop_price": "61000.00",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def hmac_headers(body: bytes) -> dict[str, str]:
    return {
        SIGNATURE_HEADER: compute_hmac(body, HMAC_SECRET),
        TIMESTAMP_HEADER: datetime.now(UTC).isoformat(),
        "content-type": "application/json",
    }


async def _count_decisions(alert_id: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine)
    async with maker() as s:
        result = await s.execute(
            text("SELECT count(*) FROM decisions WHERE alert_id = :id"),
            {"id": uuid.UUID(alert_id)},
        )
        count = result.scalar_one()
    await engine.dispose()
    return int(count)


# --- Authentication -----------------------------------------------------------


async def test_unknown_endpoint_returns_404(app_env: AsyncClient) -> None:
    """404 rather than 401 — probing must not confirm which endpoints exist."""
    response = await app_env.post("/webhook/not-a-real-endpoint", content=make_body())
    assert response.status_code == 404


async def test_missing_credentials_rejected(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    response = await app_env.post(f"/webhook/{token}", content=make_body())
    assert response.status_code == 401


async def test_wrong_signature_rejected(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body()
    headers = hmac_headers(body)
    headers[SIGNATURE_HEADER] = "0" * 64
    response = await app_env.post(f"/webhook/{token}", content=body, headers=headers)
    assert response.status_code == 401


async def test_valid_hmac_accepted(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body()
    response = await app_env.post(
        f"/webhook/{token}", content=body, headers=hmac_headers(body)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


async def test_stale_signature_timestamp_rejected(app_env: AsyncClient) -> None:
    """A captured request must not replay forever."""
    token = await _seed_user_with_endpoint()
    body = make_body()
    headers = hmac_headers(body)
    headers[TIMESTAMP_HEADER] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    response = await app_env.post(f"/webhook/{token}", content=body, headers=headers)
    assert response.status_code == 401


async def test_body_secret_fallback_accepted(app_env: AsyncClient) -> None:
    """TradingView's free plan cannot send headers, so this mode must work."""
    token = await _seed_user_with_endpoint()
    body = make_body(secret=BODY_SECRET)
    response = await app_env.post(
        f"/webhook/{token}", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200


async def test_wrong_body_secret_rejected(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body(secret="not-the-secret")
    response = await app_env.post(f"/webhook/{token}", content=body)
    assert response.status_code == 401


# --- The Phase 3 gate: duplicate alert -> exactly one decision -----------------


async def test_duplicate_alert_produces_exactly_one_decision(
    app_env: AsyncClient,
) -> None:
    """The same signal delivered twice must produce one decision, not two.

    Delivered through the real endpoint, with the same signal `id`, exactly as
    TradingView would on a retry.
    """
    token = await _seed_user_with_endpoint()
    body = make_body()

    first = await app_env.post(f"/webhook/{token}/test", content=body, headers=hmac_headers(body))
    second = await app_env.post(f"/webhook/{token}/test", content=body, headers=hmac_headers(body))

    assert first.status_code == 200
    assert second.status_code == 200
    # The second is recognised as a duplicate by the risk engine.
    assert second.json()["reason_code"] == "DUPLICATE_ALERT"


async def test_different_signal_ids_are_not_duplicates(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    for _ in range(2):
        body = make_body()  # a fresh id each time
        response = await app_env.post(
            f"/webhook/{token}/test", content=body, headers=hmac_headers(body)
        )
        assert response.json()["reason_code"] != "DUPLICATE_ALERT"


# --- The /test endpoint -------------------------------------------------------


async def test_test_endpoint_returns_a_decision(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body()
    response = await app_env.post(
        f"/webhook/{token}/test", content=body, headers=hmac_headers(body)
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["verdict"] in ("APPROVED", "REJECTED")
    assert payload["reason_code"]
    assert "latency_ms" in payload


async def test_test_endpoint_rejects_symbol_not_on_allowlist(
    app_env: AsyncClient,
) -> None:
    """The feature users rely on most: finding out *before* a live signal does."""
    token = await _seed_user_with_endpoint(allowed_symbols=["ETHUSDT"])
    body = make_body()
    response = await app_env.post(
        f"/webhook/{token}/test", content=body, headers=hmac_headers(body)
    )
    assert response.json()["reason_code"] == "SYMBOL_NOT_ALLOWED"


async def test_test_endpoint_rejects_a_stop_on_the_wrong_side(
    app_env: AsyncClient,
) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body(stop_price="63000.00")  # above entry, on a long
    response = await app_env.post(
        f"/webhook/{token}/test", content=body, headers=hmac_headers(body)
    )
    assert response.json()["reason_code"] == "NO_STOP_LOSS"


async def test_invalid_payload_is_still_recorded_and_answered(
    app_env: AsyncClient,
) -> None:
    """An unparseable alert is exactly what a user needs to see on the dashboard."""
    token = await _seed_user_with_endpoint()
    body = json.dumps(
        {
            "secret": BODY_SECRET,
            "timestamp": datetime.now(UTC).isoformat(),
            "account": "binance-testnet-1",
            "symbol": "BTCUSDT",
            "action": "buy",
            "leverage": 10,  # unknown field
        }
    ).encode()
    response = await app_env.post(f"/webhook/{token}/test", content=body)
    assert response.status_code == 200
    assert response.json()["reason_code"] == "INVALID_PAYLOAD"


async def test_unknown_account_label_rejected(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    body = make_body(account="does-not-exist")
    response = await app_env.post(
        f"/webhook/{token}/test", content=body, headers=hmac_headers(body)
    )
    assert response.json()["reason_code"] == "ACCOUNT_NOT_FOUND"


async def test_oversized_body_rejected_before_parsing(app_env: AsyncClient) -> None:
    token = await _seed_user_with_endpoint()
    huge = b'{"secret":"' + b"a" * 20000 + b'"}'
    response = await app_env.post(f"/webhook/{token}", content=huge)
    assert response.status_code == 413
