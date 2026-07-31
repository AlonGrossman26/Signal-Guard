# SignalGuard

Risk-management middleware that sits between a trading signal source and a broker.

A strategy fires a webhook. SignalGuard validates it against your risk rules, sizes the position
correctly, and only then forwards an order. If a rule is violated the order is **rejected and
logged, never sent**.

The entire value of this product is that it says **no** reliably. Correctness beats features.

> **Testnet only.** Live trading is disabled by a config flag *and* a confirmation phrase *and* a
> database CHECK constraint. Do not attempt to enable it — no phase has authorised it yet.

---

## Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) running.

**1. Create your `.env`**

```powershell
Copy-Item .env.example .env      # PowerShell
```
```bash
cp .env.example .env             # Git Bash / Linux
```

**2. Fill in the secrets.** The app refuses to start without them — that is deliberate, and the
startup error tells you exactly what to run. Generate each one:

```powershell
python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"   # CREDENTIALS_MASTER_KEY
python -c "import secrets;print(secrets.token_urlsafe(32))"                                   # ENDPOINT_ID_PEPPER
python -c "import secrets;print(secrets.token_urlsafe(32))"                                   # SESSION_SECRET
```

Also set `POSTGRES_PASSWORD` to anything you like, and put the same value into the `DATABASE_URL`
line (between `signalguard:` and `@db`).

**3. Start the stack and create the schema**

```powershell
docker compose up -d --build
docker compose exec api uv run alembic upgrade head
```

**4. Check it is alive**

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Expected:

```json
{
  "status": "green",
  "version": "0.1.0",
  "checks": { "postgres": {"status": "up"}, "redis": {"status": "up"} }
}
```

A `503` with `"status": "degraded"` means a dependency is down, and the response names which one.
That is the endpoint working correctly — it reports honestly rather than returning 200 whenever the
web server happens to be up.

---

## Common commands

```powershell
docker compose logs -f api                                   # follow logs (structured JSON)
docker compose exec api uv run pytest -q                     # full test suite
docker compose exec api uv run pytest -m "not integration" -q # no database needed
docker compose exec api uv run ruff check .                  # lint
docker compose exec api uv run mypy src                      # type check
docker compose exec api uv run alembic upgrade head          # apply migrations
docker compose exec api uv run alembic downgrade -1          # roll back one migration
docker compose down -v                                       # stop and wipe the database
```

PowerShell note: `&&` is a parser error in PowerShell 5.1. Chain with `A; if ($?) { B }`.

---

## Project documents

| Document | What it is |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | The rules. Hard constraints, architecture, risk-engine spec. |
| [`PROJECT_MANAGEMENT.md`](./PROJECT_MANAGEMENT.md) | The task board — who is working on what, and open questions. |
| [`docs/phase-0-plan.md`](./docs/phase-0-plan.md) | The plan: assumptions, data model, and the open questions. |

---

## Layout

```
backend/src/signalguard/
  config.py        settings; fails closed on missing secrets
  logging.py       structured JSON + automatic secret redaction
  enums.py         verdicts, reason codes, order states
  db/              models and session management
  api/             HTTP routes (health so far)
  ingress/         webhook receiver            — Phase 3
  risk/            pure decision logic, NO I/O — Phase 2
  execution/       broker adapters             — Phase 4
```

`risk/` will contain **zero I/O** — no database, no network, not even a clock read (the current time
is passed in). That is what makes the engine exhaustively testable, and it is the single most
important design decision in the project.

---

## Status

Phase 1 (skeleton) is complete: Docker Compose, FastAPI, health checks, the full database schema,
config loading, and structured logging. The risk engine, webhook receiver, broker adapter and
dashboard are not built yet — see `PROJECT_MANAGEMENT.md` for the current phase.
