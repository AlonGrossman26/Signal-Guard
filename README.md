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

**First time here? Follow [`docs/getting-started.md`](./docs/getting-started.md)** — a step-by-step
walkthrough from installing Docker to watching a real order land on the Binance testnet, including
how to get your testnet API key.

The short version, if you already have Docker Desktop running:

```powershell
Copy-Item .env.example .env
# Fill in the three secrets — the app refuses to start without them and the
# error tells you the exact command to generate each one.
docker compose up -d --build
docker compose exec api uv run alembic upgrade head
Invoke-RestMethod http://localhost:8000/health      # expect "status": "green"
```

The dashboard is not containerized yet and runs separately (needs Node 20+):

```powershell
cd frontend
npm install
npm run dev                                          # then open http://localhost:3000
```

Use `localhost`, not `127.0.0.1` — the API's CORS allowlist only accepts the former by default.

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
| [`docs/getting-started.md`](./docs/getting-started.md) | **Start here.** From zero to a live testnet trade, step by step. |
| [`CLAUDE.md`](./CLAUDE.md) | The rules. Hard constraints, architecture, risk-engine spec. |
| [`PROJECT_MANAGEMENT.md`](./PROJECT_MANAGEMENT.md) | The task board — status of every task, and the answered open questions. |
| [`docs/runbook.md`](./docs/runbook.md) | The 3am runbook. Something is broken and you need it fixed now. |
| [`docs/deploy.md`](./docs/deploy.md) | Deployment reference: config, migrations, health, ops. |
| [`docs/phase-0-plan.md`](./docs/phase-0-plan.md) | The original plan: assumptions, data model, and the reasoning behind each decision. |

---

## Layout

```
backend/src/signalguard/
  config.py        settings; fails closed on missing secrets
  logging.py       structured JSON + automatic secret redaction
  enums.py         verdicts, reason codes, order states
  wiring.py        composition root — the only module that knows both ends
  db/              models and session management
  api/             dashboard REST API + WebSocket
  ingress/         webhook receiver, auth, dedupe   — knows HTTP, not brokers
  risk/            pure decision logic, NO I/O      — the 10 rules and sizing
  execution/       broker adapter, orders, reconciler, trades, kill switch
  notify/          Telegram alerts
frontend/          Next.js dashboard: Live, Risk profile, History, Setup
```

`risk/` contains **zero I/O** — no database, no network, no clock reads. It takes a fully populated
snapshot and returns a decision. That is what makes the whole rule set testable in milliseconds, and
a test asserts the purity so it cannot regress.

`risk/` will contain **zero I/O** — no database, no network, not even a clock read (the current time
is passed in). That is what makes the engine exhaustively testable, and it is the single most
important design decision in the project.

---

## Status

Phase 1 (skeleton) is complete: Docker Compose, FastAPI, health checks, the full database schema,
config loading, and structured logging. The risk engine, webhook receiver, broker adapter and
dashboard are not built yet — see `PROJECT_MANAGEMENT.md` for the current phase.
