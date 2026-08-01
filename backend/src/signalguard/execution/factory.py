"""Construct a live broker adapter from a stored account (CLAUDE.md §5, §12).

This is the seam between "an account row with encrypted credentials" and "an
object that can talk to the exchange". It lives in the execution layer because
that is the layer that knows brokers; the API layer calls it but never learns how
a broker is built.

The credentials are decrypted **only here, only at the moment of use**, and the
plaintext never leaves this function's frame (§12). The envelope format — a JSON
object of `{api_key, api_secret}`, AES-GCM encrypted — is the one written by the
broker-account create endpoint.
"""

from __future__ import annotations

import json

from signalguard.crypto import decrypt_credential
from signalguard.db.models import BrokerAccount
from signalguard.execution.base import BrokerAdapter
from signalguard.execution.binance_testnet import BinanceTestnetAdapter

# The only broker implemented in v1 (§9). Multiple brokers are out of scope, so
# this is a match on a known value, not a plugin registry.
_BINANCE_TESTNET = "binance_spot_testnet"


class UnsupportedBrokerError(RuntimeError):
    """The account names a broker we do not implement."""


def decode_broker_credentials(account: BrokerAccount, master_key: str) -> tuple[str, str]:
    """Decrypt an account's stored credentials into (api_key, api_secret)."""
    plaintext = decrypt_credential(
        account.encrypted_credentials, account.credentials_nonce, master_key
    )
    data = json.loads(plaintext)
    return str(data["api_key"]), str(data["api_secret"])


def build_broker(account: BrokerAccount, master_key: str) -> BrokerAdapter:
    """Build the adapter for an account. Raises for an unsupported broker.

    Constructing the adapter opens no sockets — the credentials are decrypted and
    stored in memory, and the first network call happens only when a method is
    invoked.
    """
    if account.broker != _BINANCE_TESTNET:
        raise UnsupportedBrokerError(account.broker)
    api_key, api_secret = decode_broker_credentials(account, master_key)
    return BinanceTestnetAdapter(api_key, api_secret)
