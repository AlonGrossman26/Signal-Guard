"""The broker factory and the withdrawal-permission check.

Pure unit tests: the factory only decrypts and constructs (no network), and the
permission check runs against httpx's MockTransport.
"""

from __future__ import annotations

import base64
import json
import secrets

import httpx
import pytest

from signalguard.crypto import encrypt_credential
from signalguard.db.models import BrokerAccount
from signalguard.execution.binance_testnet import BinanceTestnetAdapter
from signalguard.execution.factory import (
    UnsupportedBrokerError,
    build_broker,
    decode_broker_credentials,
)

MASTER_KEY = base64.b64encode(secrets.token_bytes(32)).decode()


def _account(broker: str = "binance_spot_testnet") -> BrokerAccount:
    ct, nonce = encrypt_credential(
        json.dumps({"api_key": "AK-live", "api_secret": "AS-live"}), MASTER_KEY
    )
    return BrokerAccount(
        broker=broker,
        label="acct",
        encrypted_credentials=ct,
        credentials_nonce=nonce,
        is_testnet=True,
        is_active=True,
    )


def test_decode_round_trips_credentials() -> None:
    key, secret = decode_broker_credentials(_account(), MASTER_KEY)
    assert key == "AK-live"
    assert secret == "AS-live"


def test_build_broker_constructs_adapter_with_the_decrypted_keys() -> None:
    adapter = build_broker(_account(), MASTER_KEY)
    assert isinstance(adapter, BinanceTestnetAdapter)
    # The decrypted key reached the adapter (used to sign requests).
    assert adapter._api_key == "AK-live"


def test_unsupported_broker_raises() -> None:
    with pytest.raises(UnsupportedBrokerError):
        build_broker(_account(broker="some_other_exchange"), MASTER_KEY)


def _adapter(handler: object) -> BinanceTestnetAdapter:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return BinanceTestnetAdapter(
        "k", "s", client=httpx.AsyncClient(transport=transport, base_url="http://x")
    )


async def test_withdrawal_enabled_true() -> None:
    adapter = _adapter(lambda req: httpx.Response(200, json={"enableWithdrawals": True}))
    assert await adapter.get_withdrawal_enabled() is True
    await adapter.aclose()


async def test_withdrawal_enabled_false() -> None:
    adapter = _adapter(lambda req: httpx.Response(200, json={"enableWithdrawals": False}))
    assert await adapter.get_withdrawal_enabled() is False
    await adapter.aclose()


async def test_withdrawal_unknown_when_endpoint_absent() -> None:
    # Testnet does not expose apiRestrictions — a 404 must read as "unknown".
    adapter = _adapter(lambda req: httpx.Response(404, json={"code": -1121}))
    assert await adapter.get_withdrawal_enabled() is None
    await adapter.aclose()
