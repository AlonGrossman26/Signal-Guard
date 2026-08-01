# SignalGuard — Deployment

How to stand SignalGuard up, from nothing to a running, health-green stack. For
what to do when it breaks, see [`runbook.md`](./runbook.md).

> **Testnet only.** As shipped, SignalGuard cannot reach a live exchange: the
> broker base URL is a hardcoded constant and live trading is double-gated in
> config (a flag *and* an exact confirmation phrase). Do not attempt to point it
> at production — that is a later, explicitly-authorised phase.

---

## 1. Prerequisites

- **Docker** with Compose v2 (`docker compose`, space — not `docker-compose`).
- The repo checked out. All commands run from the repo root.
- Ports **8000** (API), **5432** (Postgres), **6379** (Redis) free on the host.

Everything runs in Linux containers. The host OS does not matter; the app never
assumes a host path or line ending.

---

## 2. Configure

Copy the example env file and fill in **every** value. The app fails closed on
missing secrets — it refuses to start and the error tells you the exact command
to generate each one.

```bash
cp .env.example .env          # Git Bash / Linux
# Copy-Item .env.example .env # PowerShell
```

Generate the three required secrets:

```bash
# CREDENTIALS_MASTER_KEY — 32 random bytes, base64 (AES-256 envelope key)
python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"

# ENDPOINT_ID_PEPPER and SESSION_SECRET
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

Fill in:

| Variable | Notes |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | read by the `db` container to initialise Postgres. Set a real password. |
| `DATABASE_URL` | must match the three above. Host is the compose service name `db`, **never** `localhost`. |
| `REDIS_URL` | `redis://redis:6379/0` — service name, not localhost. |
| `CREDENTIALS_MASTER_KEY` / `ENDPOINT_ID_PEPPER` / `SESSION_SECRET` | the generated secrets. No defaults; blank = refuses to boot. |
| `LIVE_TRADING_ENABLED` | leave `false`. `true` also demands the confirmation phrase or the app will not start. |
| `RECONCILER_ENABLED` / `RECONCILER_INTERVAL_SEC` | leave `true` / `15`. The loop repairs order state, syncs positions, records equity, builds closed trades, refreshes exchange filters, and keeps a `LOCKED` account flat. Off means none of that happens; the app warns at boot when it is. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | optional. Both blank = notifications disabled. Notifications are never a safety control, so leaving them off is fine. |

**Never commit `.env`, and never put a real API key in `.env.example`.**

---

## 3. Bring the stack up

```bash
docker compose up -d --build
```

The `api` container waits for Postgres and Redis to pass their healthchecks
before it starts, so it never boots into a half-initialised database.

---

## 4. Run migrations

The API does **not** migrate on startup — schema changes are an explicit,
reviewed step, never a silent side effect of a restart. Apply them once the stack
is up:

```bash
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run alembic current   # verify
```

---

## 5. Verify

```bash
# Full readiness — green only when Postgres AND Redis both answer.
curl -fsS http://localhost:8000/health | jq .
# {"status":"green","version":"...","checks":{"postgres":{"status":"up"},"redis":{"status":"up"}}}

# Process liveness only (no dependencies).
curl -fsS http://localhost:8000/health/live
```

A 503 from `/health` means a dependency is down — the check reports honestly
rather than returning 200 whenever the web framework is up. See the runbook §1.

Also confirm the reconciliation loop came up, because `/health` does not cover it:

```bash
docker compose logs api | grep "Reconciliation loop"
# Reconciliation loop started      <- expected
# Reconciliation loop is DISABLED  <- someone set RECONCILER_ENABLED=false
```

Without it nothing repairs broker state: orders stay in whatever status they were
last seen in, positions and the equity curve stop updating, no closed trades are
recorded (so the circuit breaker never counts a loss), and a `LOCKED` account is
no longer continuously flattened. Treat a missing line as a failed deploy.

---

## 6. First user, first webhook

The dashboard REST API lives under `/api` and uses session cookies.

```bash
# Register (creates the account, a fail-closed default risk profile, and logs in)
curl -fsS -c cookies.txt -X POST http://localhost:8000/api/auth/register \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"a-long-passphrase"}'

# A new profile allows NO symbols by default — opt one in before it will trade:
curl -fsS -b cookies.txt -X PUT http://localhost:8000/api/risk-profile \
  -H 'content-type: application/json' \
  -d '{"allowed_symbols":["BTCUSDT"]}'

# Add a broker account (testnet credentials; withdrawals disabled, IP-restricted):
curl -fsS -b cookies.txt -X POST http://localhost:8000/api/broker-accounts \
  -H 'content-type: application/json' \
  -d '{"label":"binance-testnet-1","api_key":"...","api_secret":"..."}'

# Mint a webhook endpoint — the token and secrets are shown ONCE, store them now:
curl -fsS -b cookies.txt -X POST http://localhost:8000/api/webhook-endpoints | jq .
```

Point your strategy at `POST /webhook/<endpoint_token>`. Use the HMAC signature
header where you can; TradingView's free plan cannot send headers, so the in-body
`secret` is the documented (weaker) fallback.

**Test before you trust it.** `POST /webhook/<token>/test` runs the full risk
pipeline and returns the decision **without touching the broker** — this is how
you confirm your stop is on the right side and your sizing is sane before a live
signal does it for you.

---

## 7. Live updates (WebSocket)

The dashboard subscribes to `GET /ws` (same session cookie). It streams decisions,
order-status changes, position updates and equity ticks, fed by Redis pub/sub.
On connect it sends a `{"type":"connected"}` frame; after that, events; a
`{"type":"heartbeat"}` every ~25s keeps the socket warm. It is read-only.

---

## 8. Operations quick reference

```bash
docker compose logs -f api                 # structured JSON logs, one event per line
docker compose ps                          # container/health status
docker compose exec api uv run pytest -q   # full test suite (needs the DB/Redis up)
docker compose exec api uv run ruff check . # lint
docker compose exec api uv run mypy src     # types (strict)
docker compose down                        # stop (keeps volumes/data)
docker compose down -v                     # stop AND DELETE all data — the audit
                                           # trail included. Almost never what you want.
```

Backups, rollback, and the kill switch are in [`runbook.md`](./runbook.md).
