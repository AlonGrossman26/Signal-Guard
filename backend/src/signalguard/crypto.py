"""Cryptographic helpers: credential encryption, hashing, signatures.

Three distinct jobs that are easy to conflate, and getting the wrong tool for
each is a classic security bug:

* **Broker credentials** are *encrypted* (AES-GCM), because we need the plaintext
  back to call the exchange.
* **Passwords** are *hashed slowly* (Argon2id), because we never need them back
  and an attacker who steals the table should have to spend years guessing.
* **Webhook endpoint IDs** are *hashed fast and deterministically* (SHA-256 with
  a server-side pepper), because we look one up on every single request, so it
  must be indexable. Argon2 would be correct for a password and completely wrong
  here — it is deliberately slow, and a random salt cannot be indexed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-GCM nonce: 96 bits is the standard size and the one the mode is designed
# around. Generated fresh per encryption and stored alongside the ciphertext —
# reusing a nonce with the same key destroys GCM's security entirely.
NONCE_BYTES = 12

_password_hasher = PasswordHasher()


# --- Broker credentials -------------------------------------------------------


def encrypt_credential(plaintext: str, master_key_b64: str) -> tuple[bytes, bytes]:
    """Encrypt a secret, returning (ciphertext, nonce).

    AES-GCM is authenticated: tampering with the stored ciphertext makes
    decryption fail loudly rather than returning wrong plaintext.
    """
    key = base64.b64decode(master_key_b64)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return ciphertext, nonce


def decrypt_credential(ciphertext: bytes, nonce: bytes, master_key_b64: str) -> str:
    """Decrypt a secret. Call this only at the moment of use, never at load time."""
    key = base64.b64decode(master_key_b64)
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode()


# --- Passwords ----------------------------------------------------------------


def hash_password(password: str) -> str:
    """Argon2id (CLAUDE.md §12)."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, Exception):  # noqa: B014 - any failure is a no
        return False


# --- Tokens and lookup hashes -------------------------------------------------


def generate_endpoint_id() -> str:
    """A long, opaque, per-user webhook token. 256 bits — not guessable."""
    return secrets.token_urlsafe(32)


def hash_endpoint_id(endpoint_id: str, pepper: str) -> str:
    """Deterministic, indexable hash of a webhook token.

    The pepper lives in the environment rather than the database, so stealing the
    database alone does not let an attacker brute-force tokens offline — they
    would need the application's configuration too.
    """
    return hashlib.sha256(f"{pepper}:{endpoint_id}".encode()).hexdigest()


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(24)


# --- Signatures ---------------------------------------------------------------


def compute_hmac(raw_body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the **raw** request body.

    Raw bytes, not the parsed-and-reserialised JSON: re-serialising can reorder
    keys or change whitespace, which changes the signature and breaks every
    legitimate sender.
    """
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_hmac(raw_body: bytes, secret: str, provided_signature: str) -> bool:
    """Constant-time signature comparison.

    `compare_digest` rather than `==` so an attacker cannot learn the correct
    signature byte by byte from how long the comparison takes.
    """
    expected = compute_hmac(raw_body, secret)
    return hmac.compare_digest(expected, provided_signature.strip().lower())


def constant_time_compare(a: str, b: str) -> bool:
    """For the in-body shared secret, which is compared as a plain string."""
    return hmac.compare_digest(a.encode(), b.encode())
