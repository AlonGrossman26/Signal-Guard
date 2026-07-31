"""HMAC signing, replay protection, and dedupe-key derivation."""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime, timedelta

import pytest

from signalguard.crypto import (
    compute_hmac,
    constant_time_compare,
    decrypt_credential,
    encrypt_credential,
    hash_endpoint_id,
    hash_password,
    verify_hmac,
    verify_password,
)
from signalguard.ingress.auth import REPLAY_WINDOW_SEC, AuthError, _check_replay_window
from signalguard.ingress.dedupe import compute_dedupe_key

NOW = datetime(2026, 7, 31, 10, 15, tzinfo=UTC)


# --- HMAC ---------------------------------------------------------------------


def test_valid_signature_verifies() -> None:
    body = b'{"symbol":"BTCUSDT"}'
    assert verify_hmac(body, "secret", compute_hmac(body, "secret"))


def test_tampered_body_fails_verification() -> None:
    signature = compute_hmac(b'{"symbol":"BTCUSDT"}', "secret")
    assert not verify_hmac(b'{"symbol":"ETHUSDT"}', "secret", signature)


def test_wrong_secret_fails_verification() -> None:
    body = b'{"symbol":"BTCUSDT"}'
    assert not verify_hmac(body, "other-secret", compute_hmac(body, "secret"))


def test_signature_is_over_raw_bytes_not_reserialised_json() -> None:
    """Reserialising can reorder keys or change whitespace, breaking every sender."""
    compact = b'{"a":1,"b":2}'
    spaced = b'{"a": 1, "b": 2}'
    assert compute_hmac(compact, "s") != compute_hmac(spaced, "s")


def test_signature_comparison_is_case_insensitive_on_hex() -> None:
    body = b'{"symbol":"BTCUSDT"}'
    assert verify_hmac(body, "secret", compute_hmac(body, "secret").upper())


# --- Replay protection --------------------------------------------------------


def test_fresh_timestamp_accepted() -> None:
    _check_replay_window(NOW.isoformat(), NOW)  # must not raise


def test_old_timestamp_rejected() -> None:
    old = (NOW - timedelta(seconds=REPLAY_WINDOW_SEC + 1)).isoformat()
    with pytest.raises(AuthError, match="replay window"):
        _check_replay_window(old, NOW)


def test_future_timestamp_rejected() -> None:
    future = (NOW + timedelta(seconds=REPLAY_WINDOW_SEC + 1)).isoformat()
    with pytest.raises(AuthError, match="replay window"):
        _check_replay_window(future, NOW)


def test_missing_timestamp_rejected() -> None:
    with pytest.raises(AuthError, match="missing"):
        _check_replay_window(None, NOW)


def test_malformed_timestamp_rejected() -> None:
    with pytest.raises(AuthError, match="malformed"):
        _check_replay_window("not-a-date", NOW)


def test_zulu_suffix_accepted() -> None:
    _check_replay_window("2026-07-31T10:15:00Z", NOW)  # must not raise


# --- Dedupe keys --------------------------------------------------------------


def test_same_signal_id_gives_the_same_key() -> None:
    a = compute_dedupe_key("ep1", "sig-1", "BTCUSDT", "buy", NOW)
    b = compute_dedupe_key("ep1", "sig-1", "BTCUSDT", "buy", NOW)
    assert a == b


def test_different_endpoints_never_collide() -> None:
    """Two users sending the same signal id must not deduplicate each other."""
    a = compute_dedupe_key("ep1", "sig-1", "BTCUSDT", "buy", NOW)
    b = compute_dedupe_key("ep2", "sig-1", "BTCUSDT", "buy", NOW)
    assert a != b


def test_fallback_key_ignores_sub_second_jitter() -> None:
    """A retry milliseconds later is the same signal."""
    a = compute_dedupe_key("ep1", None, "BTCUSDT", "buy", NOW)
    b = compute_dedupe_key("ep1", None, "BTCUSDT", "buy", NOW.replace(microsecond=500000))
    assert a == b


def test_fallback_key_separates_different_seconds() -> None:
    a = compute_dedupe_key("ep1", None, "BTCUSDT", "buy", NOW)
    b = compute_dedupe_key("ep1", None, "BTCUSDT", "buy", NOW + timedelta(seconds=1))
    assert a != b


def test_fallback_key_separates_sides() -> None:
    a = compute_dedupe_key("ep1", None, "BTCUSDT", "buy", NOW)
    b = compute_dedupe_key("ep1", None, "BTCUSDT", "sell", NOW)
    assert a != b


# --- Crypto primitives --------------------------------------------------------


def test_credential_round_trips() -> None:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    ciphertext, nonce = encrypt_credential("binance-api-key", key)
    assert decrypt_credential(ciphertext, nonce, key) == "binance-api-key"


def test_ciphertext_never_contains_the_plaintext() -> None:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    ciphertext, _ = encrypt_credential("binance-api-key", key)
    assert b"binance-api-key" not in ciphertext


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """A fresh nonce per encryption. Reuse would destroy GCM's security."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    first, nonce_a = encrypt_credential("same", key)
    second, nonce_b = encrypt_credential("same", key)
    assert first != second
    assert nonce_a != nonce_b


def test_tampered_ciphertext_fails_to_decrypt() -> None:
    """AES-GCM is authenticated: tampering fails loudly, not silently."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    ciphertext, nonce = encrypt_credential("secret", key)
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    with pytest.raises(Exception):  # noqa: B017 - any failure is correct here
        decrypt_credential(tampered, nonce, key)


def test_password_hash_round_trips() -> None:
    digest = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", digest)
    assert not verify_password("wrong password", digest)


def test_password_hash_is_argon2id() -> None:
    assert hash_password("x").startswith("$argon2id$")


def test_endpoint_hash_is_deterministic_and_peppered() -> None:
    """Deterministic so it can be indexed; peppered so a stolen DB is not enough."""
    assert hash_endpoint_id("token", "pepper") == hash_endpoint_id("token", "pepper")
    assert hash_endpoint_id("token", "pepper") != hash_endpoint_id("token", "other")


def test_constant_time_compare_matches_semantics_of_equality() -> None:
    assert constant_time_compare("a", "a")
    assert not constant_time_compare("a", "b")
