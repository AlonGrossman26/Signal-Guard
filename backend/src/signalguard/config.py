"""Application configuration.

Two hard constraints from CLAUDE.md are enforced here, at startup, rather than
being left to discipline later:

* **Fail closed on missing config (#1).** A missing secret is not defaulted, not
  auto-generated, not warned about — the process refuses to start, and the error
  says exactly how to produce the missing value. A service that boots with a
  half-configured security posture is worse than one that does not boot.
* **Testnet only (#2).** Live trading sits behind a flag that defaults off *and*
  refuses to turn on without an exact confirmation phrase. Two independent
  actions are required, so no single typo or stray environment variable can put
  real money at risk.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Typing this exact phrase is the second of the two deliberate actions needed to
# enable live trading. It is long and awkward on purpose.
LIVE_TRADING_CONFIRMATION_PHRASE = "I UNDERSTAND THIS PLACES REAL ORDERS WITH REAL MONEY"

# Shown when a required secret is absent, so the fix is in the error itself.
_GENERATE_HINTS = {
    "credentials_master_key": (
        'python -c "import base64,secrets;'
        'print(base64.b64encode(secrets.token_bytes(32)).decode())"'
    ),
    "endpoint_id_pepper": 'python -c "import secrets;print(secrets.token_urlsafe(32))"',
    "session_secret": 'python -c "import secrets;print(secrets.token_urlsafe(32))"',
}


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. Always fatal — never caught and ignored."""


class Settings(BaseSettings):
    """Runtime configuration, read from the environment (and `.env` in dev)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Origins allowed to call the API with credentials (the dashboard). Defaults
    # to the Next.js dev server. In production the frontend is served same-origin
    # behind a reverse proxy, so this list is narrow on purpose — a permissive
    # "*" is incompatible with credentialed requests anyway, and would be a
    # cross-site risk for an app that can move money.
    cors_allow_origins: list[str] = ["http://localhost:3000"]

    # Connection strings. Inside Docker these use compose service names (`db`,
    # `redis`), never localhost — see .env.example.
    database_url: str = Field(min_length=1)
    redis_url: str = Field(min_length=1)

    # Secrets. No defaults, by design: an empty value fails validation below.
    credentials_master_key: str = ""
    endpoint_id_pepper: str = ""
    session_secret: str = ""

    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""

    # Telegram notifications (CLAUDE.md §1, §10). Optional: absent means
    # notifications are simply disabled — they are an alerting convenience, never
    # a safety control, so an unconfigured bot must not stop the app from running
    # or a kill switch from firing. App-level for v1; per-user routing is a later
    # enhancement (the data model has no per-user Telegram fields yet).
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @field_validator("credentials_master_key", "endpoint_id_pepper", "session_secret")
    @classmethod
    def _require_secret(cls, value: str, info: ValidationInfo) -> str:
        """Reject absent or obviously too-short secrets, with a copy-pasteable fix."""
        name = info.field_name or "secret"
        if not value.strip():
            hint = _GENERATE_HINTS.get(name, "")
            raise ValueError(
                f"{name.upper()} is not set. SignalGuard will not start without it.\n"
                f"  Generate one with:  {hint}\n"
                f"  Then put it in your .env file."
            )
        # 32 bytes of entropy is the floor for every secret here. Short secrets
        # are the kind of thing that looks fine in dev and is brute-forced later.
        if len(value) < 32:
            raise ValueError(
                f"{name.upper()} is too short ({len(value)} chars, need >= 32). "
                f"Generate a real one:  {_GENERATE_HINTS.get(name, '')}"
            )
        return value

    @field_validator("credentials_master_key")
    @classmethod
    def _master_key_is_32_bytes(cls, value: str) -> str:
        """The AES-GCM master key must decode to exactly 32 bytes (AES-256)."""
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:  # noqa: BLE001 - any decode failure is fatal
            raise ValueError(
                "CREDENTIALS_MASTER_KEY must be base64. Generate one with:  "
                f"{_GENERATE_HINTS['credentials_master_key']}"
            ) from exc
        if len(raw) != 32:
            raise ValueError(
                f"CREDENTIALS_MASTER_KEY must decode to exactly 32 bytes for AES-256, "
                f"got {len(raw)}. Generate one with:  "
                f"{_GENERATE_HINTS['credentials_master_key']}"
            )
        return value

    @field_validator("live_trading_confirmation")
    @classmethod
    def _live_trading_needs_confirmation(cls, value: str, info: ValidationInfo) -> str:
        """Constraint #2: the flag alone is not enough to enable live trading.

        Validated last (field order matters in pydantic) so `live_trading_enabled`
        is already populated in `info.data`.
        """
        enabled = bool(info.data.get("live_trading_enabled", False))
        if enabled and value != LIVE_TRADING_CONFIRMATION_PHRASE:
            raise ValueError(
                "LIVE_TRADING_ENABLED is true, but LIVE_TRADING_CONFIRMATION does not "
                "match the required phrase. Live trading is not authorised in this "
                "phase of the project — set LIVE_TRADING_ENABLED=false."
            )
        if not enabled and value:
            # A stale confirmation left lying around is a loaded gun for the next
            # person who flips the flag "just to see". Refuse the ambiguity.
            raise ValueError(
                "LIVE_TRADING_CONFIRMATION is set but LIVE_TRADING_ENABLED is false. "
                "Clear the confirmation phrase."
            )
        return value

    @property
    def is_testnet_only(self) -> bool:
        """True whenever live trading is not fully authorised. Assume True."""
        return not self.live_trading_enabled


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process.

    Any validation failure is re-raised as ConfigError so the startup path has a
    single obvious thing to catch and report. It is never handled and resumed.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except Exception as exc:  # noqa: BLE001 - deliberately broad; this is fatal
        raise ConfigError(f"Invalid configuration — refusing to start.\n{exc}") from exc
