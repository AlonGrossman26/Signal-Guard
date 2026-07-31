"""Structured JSON logging with automatic secret redaction.

Constraint #6 says secrets never touch logs. Relying on every future call site to
remember that is not a plan — one `logger.info("payload=%s", body)` three months
from now undoes it. So redaction happens centrally, in a filter every record
passes through, and it works two ways:

1. **By field name.** Anything logged as a structured extra whose key looks
   secret-bearing (`api_secret`, `password`, `x-signature`, ...) is replaced,
   recursively, through nested dicts and lists.
2. **By value.** Secrets known at startup (the master key, the pepper, the
   session secret) are registered and scrubbed out of the rendered message and
   any traceback text. This is the backstop for the case field-name matching
   cannot catch: a secret that arrives somewhere we did not anticipate — most
   often inside an exception message.

Both are needed. Field-name matching alone misses stray interpolation; value
matching alone misses secrets we never registered (a user's broker API key,
which is per-user and only known at runtime — those must be passed as named
fields, which is what rule 1 covers).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

REDACTED = "[REDACTED]"

# Substrings matched case-insensitively against structured field names.
#
# Note what is deliberately absent: a bare "key". It would redact innocent fields
# like `key_version` and `idempotency_key`, and a redaction filter that fires on
# harmless fields trains people to stop trusting it.
SECRET_FIELD_MARKERS: frozenset[str] = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "passphrase",
        "token",
        "authorization",
        "credential",
        "api_key",
        "apikey",
        "private_key",
        "signature",
        "cookie",
        "pepper",
        "hmac",
        "endpoint_id",
    }
)

# Standard LogRecord attributes — everything else in __dict__ is a caller extra.
_STANDARD_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

# Literal secret values to scrub from rendered text. Module-level because the
# filter must see them regardless of which logger a record came from.
_REGISTERED_SECRETS: set[str] = set()


def register_secret_value(value: str | None) -> None:
    """Register a literal secret to be scrubbed from all rendered log text.

    Short values are ignored: scrubbing a 4-character string would mangle
    unrelated messages, and anything that short is not a real secret anyway.
    """
    if value and len(value) >= 8:
        _REGISTERED_SECRETS.add(value)


def _is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_FIELD_MARKERS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking fields in a nested structure.

    Depth-limited: a self-referencing structure must not turn a log call into an
    infinite loop. Logging is never allowed to take the process down.
    """
    if _depth > 8:
        return value
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_secret_field(str(k)) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


def scrub_registered_secrets(text: str) -> str:
    """Replace any registered literal secret appearing in rendered text."""
    for secret in _REGISTERED_SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class RedactionFilter(logging.Filter):
    """Applies both redaction passes to every record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_FIELDS:
                continue
            record.__dict__[key] = REDACTED if _is_secret_field(key) else redact(value)
        return True  # never drop a record; redact and pass it on


def _json_default(value: Any) -> str:
    """Decimal must serialise as an exact string, never via float (constraint #3)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, timestamps in UTC (constraint #7)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_registered_secrets(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS:
                payload[key] = value

        if record.exc_info:
            # Tracebacks are the most common accidental secret leak: an exception
            # repr can carry a connection string, complete with password.
            payload["exception"] = scrub_registered_secrets(
                self.formatException(record.exc_info)
            )

        return json.dumps(payload, default=_json_default, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler and redaction filter on the root logger."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; route them through ours so access logs
    # get redacted too, rather than bypassing the filter entirely.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
