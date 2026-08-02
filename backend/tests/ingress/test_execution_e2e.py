"""The webhook drives a real order — end to end (CLAUDE.md §13).

This is the regression guard for the whole of Phase 8. Every execution test
before it called `submit_entry_with_stop` directly, which is exactly why nobody
noticed for two phase sign-offs that **no code path ever called it**: the unit
tests were green and the product placed no orders.

So these tests start where a signal actually starts — an HTTP POST to the live
webhook — and assert on what ends up in the `orders` table. A fake broker is
injected at the composition root, so no test touches a network (§13).

The two guarantees under test:

* an approved alert produces an entry **and** its protective stop;
* five concurrent deliveries of the same alert produce exactly **one** order,
  which is §13's idempotency requirement stated in its own terms.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from signalguard.crypto import (
    compute_hmac,
    encrypt_credential,
    generate_endpoint_id,
    hash_endpoint_id,
)
from signalguard.ingress.auth import SIGNATURE_HEADER, TIMESTAMP_HEADER
from signalguard.risk.types import OpenPosition
from tests.fakes.fake_broker import FakeBroker

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

MASTER_KEY = base64.b64encode(secrets.token_bytes(32)).decode()
PEPPER = secrets.token_urlsafe(32)
HMAC_SECRET = "test-hmac-secret-for-execution-e2e"

# Enough equity that a 1% risk budget sizes a real position, and a stop far
# enough from entry that sizing lands comfortably above the exchange minimums.
EQUITY = Decimal("100000")
ENTRY = Decimal("60000")
STOP = Decimal("58000")


@pytest.fixture()
async def wired_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real app, with a fake broker injected at the composition root."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("ENDPOINT_ID_PEPPER", PEPPER)
    monkeypatch.setenv("SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("APP_ENV", "test")
    # The reconciler would race these assertions by repairing orders underneath
    # them. It has its own tests; this one is about the ingress -> execution path.
    monkeypatch.setenv("RECONCILER_ENABLED", "false")

    from signalguard.config import get_settings
    from signalguard.db.session import dispose_engine, init_engine
    from signalguard.redis_client import close_redis, init_redis

    get_settings.cache_clear()
    init_engine(DATABASE_URL)
    init_redis(REDIS_URL)

    broker = FakeBroker(equity=EQUITY, free_balance=EQUITY)

    # Patch the seam, not the route: `BrokerSession` is what the webhook asks for
    # broker access, so replacing its behaviour exercises the real wiring.
    from signalguard import wiring

    def fake_adapter_for(self: Any, account: Any) -> Any:
        self._adapter = broker
        self._account_id = str(account.id)
        return broker

    async def fake_reference_price(self: Any, account: Any, symbol: str) -> Decimal:
        return ENTRY

    async def fake_account_state(
        self: Any, account: Any
    ) -> tuple[Decimal, Decimal, tuple[OpenPosition, ...]]:
        self._adapter = broker
        self._account_id = str(account.id)
        return EQUITY, EQUITY, ()

    monkeypatch.setattr(wiring.BrokerSession, "adapter_for", fake_adapter_for)
    monkeypatch.setattr(wiring.BrokerSession, "reference_price", fake_reference_price)
    monkeypatch.setattr(wiring.BrokerSession, "account_state", fake_account_state)

    from signalguard.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, broker

    await dispose_engine()
    await close_redis()
    get_settings.cache_clear()


async def _seed() -> str:
    """Create a user, profile, broker account, instrument and endpoint."""
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    token = generate_endpoint_id()
    user_id = uuid.uuid4()
    hmac_ct, hmac_nonce = encrypt_credential(HMAC_SECRET, MASTER_KEY)
    creds_ct, creds_nonce = encrypt_credential(
        json.dumps({"api_key": "k", "api_secret": "s"}), MASTER_KEY
    )

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
                " max_notional_per_trade, max_total_notional, risk_per_trade_pct) "
                "VALUES (:id, :u, ARRAY['BTCUSDT'], 100000, 1000000, 0.01)"
            ),
            {"id": uuid.uuid4(), "u": user_id},
        )
        await s.execute(
            text(
                "INSERT INTO broker_accounts (id, user_id, broker, label, "
                " encrypted_credentials, credentials_nonce, is_testnet, is_active, "
                " trading_state) "
                "VALUES (:id, :u, 'binance_spot_testnet', 'binance-testnet-1', "
                "        :c, :n, true, true, 'ACTIVE')"
            ),
            {"id": uuid.uuid4(), "u": user_id, "c": creds_ct, "n": creds_nonce},
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
                "VALUES (:id, :u, :h, :hc, :hn, :hc, :hn, true)"
            ),
            {
                "id": uuid.uuid4(), "u": user_id,
                "h": hash_endpoint_id(token, PEPPER),
                "hc": hmac_ct, "hn": hmac_nonce,
            },
        )
        await s.commit()
    await engine.dispose()
    return token


def _body(signal_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "id": signal_id or uuid.uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "account": "binance-testnet-1",
            "symbol": "BTCUSDT",
            "action": "buy",
            "order_type": "market",
            "stop_price": str(STOP),
        }
    ).encode()


def _headers(body: bytes) -> dict[str, str]:
    return {
        SIGNATURE_HEADER: compute_hmac(body, HMAC_SECRET),
        TIMESTAMP_HEADER: datetime.now(UTC).isoformat(),
        "content-type": "application/json",
    }


async def _orders_for(token_user_email_like: str) -> list[dict[str, Any]]:
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        rows = await s.execute(
            text(
                "SELECT o.role, o.status, o.qty, o.symbol, o.client_order_id "
                "FROM orders o "
                "JOIN broker_accounts b ON b.id = o.broker_account_id "
                "JOIN users u ON u.id = b.user_id "
                "WHERE u.email = :e ORDER BY o.role"
            ),
            {"e": token_user_email_like},
        )
        out = [dict(r._mapping) for r in rows]
    await engine.dispose()
    return out


async def _email_for_endpoint(token: str) -> str:
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        row = await s.execute(
            text(
                "SELECT u.email FROM users u "
                "JOIN webhook_endpoints w ON w.user_id = u.id "
                "WHERE w.endpoint_id_hash = :h"
            ),
            {"h": hash_endpoint_id(token, PEPPER)},
        )
        email = row.scalar_one()
    await engine.dispose()
    return str(email)


# --- T-2: the pipeline actually reaches the broker ----------------------------


async def test_approved_alert_submits_an_entry_and_its_stop(wired_app: Any) -> None:
    """The gap the audit found: an approved decision must produce real orders.

    Before Phase 8 this test would have failed on `len(orders) == 0` — the
    decision was written, and nothing was ever sent.
    """
    client, broker = wired_app
    token = await _seed()
    email = await _email_for_endpoint(token)

    body = _body()
    response = await client.post(f"/webhook/{token}", content=body, headers=_headers(body))
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    orders = await _orders_for(email)
    roles = {o["role"] for o in orders}
    assert roles == {"ENTRY", "STOP"}, f"expected an entry and a stop, got {orders}"

    entry = next(o for o in orders if o["role"] == "ENTRY")
    stop = next(o for o in orders if o["role"] == "STOP")
    assert entry["symbol"] == "BTCUSDT"
    assert entry["qty"] > 0
    # The stop must cover what the entry actually took on. A stop for less than
    # the position leaves part of it naked, which is the thing §10 forbids.
    assert stop["qty"] == entry["qty"]

    # And it reached the broker, not just the table.
    assert len(broker.submit_calls) == 2


async def test_a_rejected_alert_submits_nothing(wired_app: Any) -> None:
    """The other half of the contract: a rejection places no order at all."""
    client, broker = wired_app
    token = await _seed()
    email = await _email_for_endpoint(token)

    # ETHUSDT is not on the allowlist, so rule 5 rejects.
    payload = json.loads(_body())
    payload["symbol"] = "ETHUSDT"
    body = json.dumps(payload).encode()

    response = await client.post(f"/webhook/{token}", content=body, headers=_headers(body))
    assert response.status_code == 200

    assert await _orders_for(email) == []
    assert broker.submit_calls == []


# --- T-1: §13 idempotency, stated in orders -----------------------------------


async def test_five_concurrent_identical_alerts_produce_exactly_one_order(
    wired_app: Any,
) -> None:
    """§13: "the same alert submitted 5x concurrently produces exactly 1 order".

    Not 5 requests in sequence — five genuinely in flight at once, which is the
    shape TradingView retries actually take. The guarantee comes from Redis's
    atomic claim in `dedupe.claim_dedupe_key`; this proves it holds under race
    *and* that the thing it protects is the order, not merely the decision.
    """
    client, broker = wired_app
    token = await _seed()
    email = await _email_for_endpoint(token)

    signal_id = uuid.uuid4().hex
    body = _body(signal_id)
    headers = _headers(body)

    responses = await asyncio.gather(
        *(client.post(f"/webhook/{token}", content=body, headers=headers) for _ in range(5))
    )
    assert all(r.status_code == 200 for r in responses)

    orders = await _orders_for(email)
    entries = [o for o in orders if o["role"] == "ENTRY"]
    assert len(entries) == 1, f"expected exactly one entry order, got {len(entries)}"

    # One entry, one stop — five deliveries, one position.
    assert len([o for o in orders if o["role"] == "STOP"]) == 1
    assert len(broker.submit_calls) == 2
