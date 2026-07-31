# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

# uv is pinned to an exact version — an unpinned tool version means a build that
# worked yesterday can break today for reasons nothing in this repo explains.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies are installed in their own layer, before the source is copied, so
# editing a .py file does not invalidate the (slow) dependency install.
COPY pyproject.toml ./
RUN uv sync --no-install-project

COPY . .
RUN uv sync

# Run as a non-root user. If the API is ever compromised, this is the difference
# between "an attacker in a container" and "an attacker who is root".
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "signalguard.main:app", "--host", "0.0.0.0", "--port", "8000"]
