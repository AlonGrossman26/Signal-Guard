"""Configuration must fail closed — a half-configured process never starts."""

from __future__ import annotations

import base64
import secrets

import pytest
from pydantic import ValidationError

from signalguard.config import LIVE_TRADING_CONFIRMATION_PHRASE, Settings


def _valid_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _base_env() -> dict[str, str]:
    return {
        "database_url": "postgresql+asyncpg://u:p@db:5432/signalguard",
        "redis_url": "redis://redis:6379/0",
        "credentials_master_key": _valid_master_key(),
        "endpoint_id_pepper": secrets.token_urlsafe(32),
        "session_secret": secrets.token_urlsafe(32),
    }


def _settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's real .env cannot influence the result.
    return Settings(_env_file=None, **{**_base_env(), **overrides})  # type: ignore[arg-type]


# Settings that this suite asserts *defaults* for. pydantic-settings reads the
# real environment as well as the explicit kwargs, so an ambient APP_ENV=test in
# the shell would otherwise make a passing test fail for a reason that has
# nothing to do with the code (reproduced: `APP_ENV=test pytest`).
_AMBIENT_OVERRIDES = ("APP_ENV", "LOG_LEVEL", "RECONCILER_ENABLED")


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove environment variables that would shadow the defaults under test."""
    for name in _AMBIENT_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


@pytest.mark.usefixtures("clean_env")
def test_valid_config_loads() -> None:
    settings = _settings()
    assert settings.app_env == "local"
    assert settings.is_testnet_only is True


@pytest.mark.usefixtures("clean_env")
def test_reconciler_is_on_by_default() -> None:
    """The broker is the source of truth; a cache nobody refreshes is stale data.

    Off is a deliberate choice an operator has to make, never the default — with
    the loop off there is no order repair, no position sync, no equity snapshots,
    no closed trades, and a LOCKED account stops being continuously enforced.
    """
    settings = _settings()
    assert settings.reconciler_enabled is True
    assert settings.reconciler_interval_sec == 15


def test_reconciler_interval_is_bounded() -> None:
    """Neither a busy-loop against the exchange nor an interval measured in hours."""
    with pytest.raises(ValidationError):
        _settings(reconciler_interval_sec=1)
    with pytest.raises(ValidationError):
        _settings(reconciler_interval_sec=3600)


@pytest.mark.parametrize(
    "field", ["credentials_master_key", "endpoint_id_pepper", "session_secret"]
)
def test_missing_secret_refuses_to_start(field: str) -> None:
    """An absent secret is fatal — never defaulted, never auto-generated."""
    with pytest.raises(ValidationError) as exc:
        _settings(**{field: ""})
    assert field.upper() in str(exc.value)


@pytest.mark.parametrize(
    "field", ["credentials_master_key", "endpoint_id_pepper", "session_secret"]
)
def test_short_secret_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: "tooshort"})


def test_master_key_must_decode_to_32_bytes() -> None:
    """AES-256 needs exactly 32 bytes; a 16-byte key would silently weaken it."""
    sixteen_bytes = base64.b64encode(secrets.token_bytes(16)).decode()
    # Pad past the length floor so this test exercises the byte-length check.
    with pytest.raises(ValidationError) as exc:
        _settings(credentials_master_key=sixteen_bytes + "AAAAAAAAAAAAAAAA")
    assert "32 bytes" in str(exc.value) or "base64" in str(exc.value)


def test_master_key_must_be_base64() -> None:
    with pytest.raises(ValidationError):
        _settings(credentials_master_key="!" * 44)


# --- Constraint #2: live trading takes two deliberate actions -----------------


def test_live_trading_defaults_off() -> None:
    assert _settings().live_trading_enabled is False


def test_live_trading_flag_alone_is_refused() -> None:
    """The flag without the confirmation phrase must not start the process."""
    with pytest.raises(ValidationError) as exc:
        _settings(live_trading_enabled=True, live_trading_confirmation="")
    assert "LIVE_TRADING_CONFIRMATION" in str(exc.value)


def test_live_trading_wrong_phrase_is_refused() -> None:
    with pytest.raises(ValidationError):
        _settings(live_trading_enabled=True, live_trading_confirmation="yes please")


def test_stale_confirmation_without_flag_is_refused() -> None:
    """A confirmation phrase left lying around is a loaded gun. Refuse it."""
    with pytest.raises(ValidationError):
        _settings(
            live_trading_enabled=False,
            live_trading_confirmation=LIVE_TRADING_CONFIRMATION_PHRASE,
        )
