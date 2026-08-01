# CLAUDE.md — SignalGuard

Guidance for Claude Code working in this repository. Read this before writing anything.

> **Multiple agents work in this repo. Before you start any task, open [`PROJECT_MANAGEMENT.md`](./PROJECT_MANAGEMENT.md).** It is the shared task board — the "Jira" for the agents on this project. Read it to see what everyone is working on, claim the task you're about to do so no one else picks it up, and update it as you go. Coordinating through that board is not optional; two agents silently editing the same file is exactly the kind of collision it exists to prevent. See §2 for the exact protocol.

---

## 1. What this project is

SignalGuard is **risk-management middleware** that sits between a trader's signal source and their broker.

A strategy (TradingView PineScript alert, a Python script, a bot) fires a webhook. SignalGuard receives it, validates it against the user's own risk rules, sizes the position correctly, and only then forwards an order to the broker. If a rule is violated, the order is **rejected and logged, never sent**. The user watches everything on a live dashboard, gets Telegram notifications, and has a kill switch that flattens everything instantly.

**The entire value of this product is that it says _no_ reliably.** Correctness beats features. A missed trade is an annoyance; a wrongly-sized or unprotected trade is a blown account. When behaviour is ambiguous, the correct answer is almost always to reject.

**In scope for v1:** webhook receiver + risk engine + one broker adapter (Binance **testnet**, spot, market and limit orders); auth (email + password, sessions) with per-user risk config; a dashboard (live decision feed, open positions, equity curve, kill switch); Telegram notifications; Docker Compose for local dev. Everything else is out of scope — see §13, and do not scaffold or abstract for it.

---

## 2. How to work in this repo

You are a senior backend engineer with production experience in trading infrastructure. The person you are pairing with is early in their coding journey. That shapes the workflow:

- **Coordinate through the task board.** Before starting, read [`PROJECT_MANAGEMENT.md`](./PROJECT_MANAGEMENT.md), claim your task there (set yourself as owner and mark it in progress), and check what other agents already have in progress so you don't collide or break a layer boundary. Update the task's status as you work and when you finish. Treat it as the single source of truth for who-is-doing-what.
- **A booked or pre-booked task belongs to its owner alone.** Once a task on the board has an owner — whether `IN PROGRESS` (booked, being worked now) or `RESERVED` (pre-booked, staked out for later) — no other agent may work on it, complete it, or reassign it. Treat another agent's owned task as off-limits until they release it (clear the Owner and set it back to `TODO`). Pre-booking with `RESERVED` lets you reserve work you intend to do next so no one else starts it first. Only the owner completes their own task; the human can always override.
- **Keep the docs in sync — never let them contradict each other.** `CLAUDE.md` and `PROJECT_MANAGEMENT.md` (and any other doc) must always agree. When you change a rule, a workflow, or the meaning of a status in one place, update *every* place that references it **in the same commit** — do not leave one document describing the old behaviour. When the human asks you to change a policy, treat every doc that mentions that policy as in scope, even if they named only one; a rule updated in one file but stale in another is exactly the kind of confusion this prevents. If you spot two docs already disagreeing, flag it and reconcile them rather than guessing which is right.
- **Explain each decision in plain language before you implement it.** Not after.
- **Keep files small and clearly named.** Comment anything non-obvious — especially financial arithmetic and anything with an ordering dependency.
- **Work in phases (§14). Stop at the end of every phase and wait for approval before starting the next.** Do not dump the whole project in one response.
- At the end of each phase: show the file tree, list what changed, give the exact commands to verify it, and **state honestly what is not yet handled**. Do not describe partial work as done.
- **Fail closed applies to you too.** If a spec detail is ambiguous, ask — do not improvise behaviour into the risk engine. A guessed rule is worse than no rule, because it looks like it works.
- **When you are unsure, ask — do not guess.** This goes beyond the risk spec. If you are not certain what the human meant, which file or scope they intended, or what the current state of something is, ask one clear, specific question before acting. Never state something as fact when you are not sure it is true, and never quietly assume an interpretation to keep moving. A short question now prevents the confusion of confidently doing — or claiming — the wrong thing.
- Do not add abstractions for the out-of-scope list (§13). No "future-proofing" plugin layers, no unused config keys.

---

## 3. Hard constraints (non-negotiable — violating any of these is a bug)

1. **Fail closed.** Any error, timeout, ambiguity, missing config, or unparseable field → reject the order. Never "assume and proceed."
2. **Paper / testnet only** until explicitly authorised in a later phase. The live-broker adapter sits behind a config flag that defaults to off and refuses to turn on without an explicit confirmation string.
3. **No floats for money or quantities.** `Decimal` end-to-end in Python, `NUMERIC` in Postgres. Never `float`, never `round()` on prices. Rounding is always explicit and always **down** for quantities.
4. **Idempotency everywhere.** The same alert delivered twice must produce exactly one order. TradingView retries; scripts have bugs; networks duplicate.
5. **Everything auditable.** Every inbound alert and every risk decision is persisted with a machine-readable reason code, *before* and *independently of* whether an order was placed.
6. **Secrets never touch logs.** Broker API keys, webhook secrets and tokens are encrypted at rest and redacted in all log output, error messages, and exception traces.
7. **UTC internally.** Store and compute in UTC; convert only at the display layer. The daily-reset time is a per-user configured timezone and must be tested across a DST boundary.

---

## 4. Environment

The host is **Windows 11**; everything that actually runs does so in **Linux containers**. Keep those two facts separate — application code must never assume a Windows path, a Windows line ending, or a host-local file.

**Shell.** The default shell is **PowerShell 5.1**. It is not bash:

- `&&` and `||` are parser errors. Use `A; if ($?) { B }` to chain conditionally, `A; B` unconditionally.
- No ternary `?:`, no `??`, no `?.`.
- Do not redirect `2>&1` on native executables (`docker`, `git`, `uv`) — 5.1 wraps stderr lines in error records and reports failure on exit code 0. stderr is captured anyway.
- `Set-Content` / `Add-Content` default to ANSI. Pass `-Encoding utf8` for any file another tool will read.
- For multi-line strings (commit messages, SQL) use a single-quoted here-string with `'@` at column 0.

A **Bash tool (Git Bash, POSIX sh)** is also available. Prefer it for `.sh` scripts, heredocs, and anything copied from Linux docs. The two shells take different syntax — pick one per command, don't mix.

**Line endings.** A `.gitattributes` must force `LF` for `*.sh`, `*.py`, `Dockerfile*`, and `*.yml`. A CRLF entrypoint script inside a container fails with an unreadable `not found` error, and that is a debugging rabbit hole worth avoiding permanently.

**Temp files.** Never scatter throwaway scripts, dumps, or scratch output into the repo, and do not use `/tmp` (it does not exist on the host). Use the session scratchpad directory for anything that isn't a deliverable.

**Tooling.** Use the dedicated Read / Glob / Grep tools rather than `Get-Content`, `Get-ChildItem -Recurse`, `Select-String`, `cat`, `find`, or `grep`.

**Docker.** Docker Desktop must be running. Use `docker compose` (v2, space) — not `docker-compose`. From the host, Postgres is `localhost:5432` and Redis `localhost:6379`; from inside a container, address services by their compose service name (`db`, `redis`). Never hardcode `localhost` in application config.

**Git.** Work directly on `main`. Commit each piece of finished work and push it straight to `main` — do **not** create feature branches, and do **not** open pull requests. Push as soon as the work is complete so `main` always reflects the latest state and other agents pick it up on their next pull. End commit messages with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Sharing `main` with other agents — never clobber each other.** Because everyone commits to the same branch, several agents may be committing at once. Follow this every time so two agents' work never collides:

1. **Before you start:** run `git pull --rebase origin main` so you begin from the latest state, then **claim your task on the board** (§2). Claiming is what stops two agents editing the same files — check what others own and stay out of it.
2. **Stay in your lane.** Touch only the files your task owns, respecting the layer boundaries (§5). Two agents editing different files never conflict.
3. **Re-check before you push — this is the end check.** **Commit your work first, *then* `git pull --rebase origin main`** — rebasing a commit is clean, but rebasing with uncommitted changes fails outright. The rebase folds in anything that landed while you worked; re-run the tests and linter and resolve any conflict locally. Only then push. If the push is rejected because another agent pushed first, `pull --rebase` again and retry — **never force-push** `main`.
4. **Commit small, push often.** The shorter the time between pulling and pushing, the smaller the window for a conflict. Don't sit on a large uncommitted change.

**Never commit:** `.env`, any real API key (even testnet), any database dump. `.env.example` carries the key names with empty values.

### Intended commands

These describe the shape the project should have. They do not all work until the phase that builds them.

```powershell
docker compose up -d --build          # Phase 1: full local stack
docker compose logs -f api
Invoke-RestMethod http://localhost:8000/health

docker compose exec api uv run pytest -q                    # full suite
docker compose exec api uv run pytest tests/risk -q          # pure risk engine, no I/O
docker compose exec api uv run alembic revision --autogenerate -m "..."
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run ruff check . ; docker compose exec api uv run mypy src
```

---

## 5. Architecture

- **Backend:** Python 3.12, FastAPI, async throughout. `uv` for dependency management.
- **DB:** PostgreSQL 16 via SQLAlchemy 2.0 async + Alembic migrations.
- **Cache/state:** Redis — idempotency keys, rate limits, circuit-breaker and kill-switch state, pub/sub for WebSocket fan-out.
- **Frontend:** Next.js (App Router), TypeScript, Tailwind, TradingView Lightweight Charts.
- **Realtime:** WebSocket backend → dashboard, fed by Redis pub/sub.
- **Deploy:** Dockerfile per service + `docker-compose.yml`.

### Layer boundaries — enforce these strictly

```
ingress/     webhook parsing, auth, dedupe     knows HTTP, knows nothing about brokers
risk/        pure decision logic               NO I/O AT ALL
execution/   broker adapters, order lifecycle  knows brokers, knows nothing about HTTP
```

**`risk/` must contain zero I/O.** It takes a fully-populated snapshot (alert + account state + user config) and returns a `Decision`. No DB calls, no network, no clock reads — **the current time is passed in**. This is what makes the engine testable and it is the single most important design decision in the project. An `import` of `httpx`, `sqlalchemy`, `redis`, or `datetime.now` inside `risk/` is a defect. Add a test that asserts this.

### Proposed layout (needs sign-off in Phase 0)

```
signalguard/
  CLAUDE.md
  docker-compose.yml
  .env.example
  .gitattributes
  backend/
    Dockerfile
    pyproject.toml
    alembic/
    src/signalguard/
      main.py  config.py  logging.py
      db/          models, session
      ingress/     webhook routes, hmac, schema, dedupe
      risk/        PURE — rules, sizing, decision types
      execution/   broker base, binance_testnet, reconciler, killswitch
      api/         REST + /ws
      notify/      telegram
    tests/
      risk/  ingress/  execution/  fakes/
  frontend/
  docs/runbook.md
```

---

## 6. Data model

Propose exact columns and get sign-off **before** writing migrations.

| Table | Contents |
|---|---|
| `users` | id, email, password_hash, created_at |
| `broker_accounts` | user_id, broker, label, encrypted_credentials, is_testnet, is_active |
| `risk_profiles` | user_id, one column per rule parameter (§7), daily_reset_time, timezone |
| `alerts` | raw payload verbatim as JSONB, source_ip, received_at, dedupe_key, parse_status |
| `decisions` | alert_id, verdict (`APPROVED`/`REJECTED`), reason_code, rule_snapshot JSONB, computed_qty, evaluated_at, latency_ms |
| `orders` | decision_id, broker_order_id, symbol, side, type, qty, price, stop_price, status, submitted_at, filled_at, avg_fill_price, fees |
| `positions` | broker_account_id, symbol, qty, avg_entry, unrealized_pnl, updated_at |
| `equity_snapshots` | broker_account_id, equity, taken_at, is_session_baseline |
| `trades` | closed round-trips with realized PnL — this is what the circuit breaker counts |

**`alerts` and `decisions` are append-only. Never `UPDATE`, never `DELETE`.** All money and quantity columns are `NUMERIC`.

---

## 7. The risk engine

Rules evaluate **in this exact order**, short-circuiting on the first rejection. Cheapest and most absolute checks first. Order is part of the spec — a payload breaking several rules must return the highest-priority reason code.

| # | Rule | Reject when | Reason code |
|---|---|---|---|
| 1 | Kill switch / trading lock | account is `LOCKED` | `TRADING_LOCKED` |
| 2 | Payload validity | schema invalid, unknown field, missing required | `INVALID_PAYLOAD` |
| 3 | Staleness | older than `max_alert_age_sec` (30) or >5s in the future | `STALE_ALERT` |
| 4 | Duplicate | `dedupe_key` seen within `dedupe_window_sec` (60) | `DUPLICATE_ALERT` |
| 5 | Symbol allowlist | symbol not in user's allowed list | `SYMBOL_NOT_ALLOWED` |
| 6 | Mandatory stop-loss | stop absent, zero, or wrong side of entry | `NO_STOP_LOSS` |
| 7 | Circuit breaker | consecutive losses ≥ threshold and cooldown not elapsed | `CIRCUIT_BREAKER_OPEN` |
| 8 | Daily drawdown | `(baseline − current) / baseline ≥ max_daily_dd_pct` | `DAILY_DRAWDOWN_HIT` |
| 9 | Position sizing | computed qty below exchange minimum after rounding | `SIZE_BELOW_MINIMUM` |
| 10 | Exposure caps | open positions ≥ max, notional > cap, or symbol already held and `allow_pyramiding` false | `EXPOSURE_LIMIT` |

Implement exactly this; do not improvise.

**Stop-loss (6).** Long: reject unless `stop_price < entry_price`. Short: unless `stop_price > entry_price`. Also reject if stop distance < `min_stop_distance_pct` of price (default 0.1%) — a near-zero stop produces an absurd position size, and that is the classic way this feature gets exploited by a buggy script.

**Circuit breaker (7).** Count *closed trades* with realized PnL < 0, consecutively, most recent first; any win resets to zero. At the threshold (default 3) set state `OPEN` and either wait `cooldown_minutes` (default 60) or require manual reset — configurable. State lives in **Redis and Postgres** and must survive a restart.

**Daily drawdown (8).** `baseline_equity` is snapshotted at the user's configured daily reset time in their timezone. Loss includes realized *and* unrealized PnL. Once tripped, the block persists until the next reset even if equity recovers. Must be correct across a DST change and a process restart.

**Position sizing (9)** — a *transform*, not just a check:

```
risk_amount   = liquid_equity × risk_per_trade_pct     # e.g. 1%, minus fee+slippage buffer (bps, configurable)
stop_distance = abs(entry_price − stop_price)
raw_qty       = risk_amount / stop_distance
qty           = round_down_to_step(raw_qty, symbol.lot_step)
```

Then verify `qty ≥ symbol.min_qty`, `qty × entry_price ≥ symbol.min_notional`, and `qty × entry_price ≤ min(max_notional_per_trade, available_balance)`. Always round **down**. Subtract the fee + slippage buffer from `risk_amount` *first*, so a worst-case stop-out still loses no more than the stated risk percentage. Fetch `lot_step` / `min_qty` / `min_notional` from the exchange instrument-info endpoint at startup and cache them — **never hardcode them**.

Every decision, approved or rejected, is written to `decisions` with its reason code and a snapshot of the config that produced it, **before** any order is submitted.

---

## 8. Webhook contract

`POST /webhook/{endpoint_id}` — `endpoint_id` is a long random per-user opaque token.

Authenticate with HMAC-SHA256 over the **raw body** in an `X-Signature` header, plus a timestamp header for replay protection. TradingView cannot send custom headers on the free plan, so also support a shared secret inside the JSON body as a documented fallback — and make clear in the UI that this mode is weaker.

Rate limit per endpoint (Redis token bucket). Reject oversized bodies **before** parsing. Accept this schema strictly and reject anything else:

```json
{
  "secret": "...",
  "id": "unique-per-signal-id",
  "timestamp": "2026-07-31T10:15:00Z",
  "account": "binance-testnet-1",
  "symbol": "BTCUSDT",
  "action": "buy | sell | close",
  "order_type": "market | limit",
  "limit_price": "62000.00",
  "stop_price": "61000.00",
  "take_profit": "64000.00"
}
```

`dedupe_key = hash(endpoint_id, id)`; if `id` is absent, `hash(endpoint_id, symbol, action, timestamp_rounded_to_second)`.

Respond `200` fast (**target < 50 ms**) after persisting the alert; run risk evaluation and execution in a background task so the sender never times out.

`POST /webhook/{endpoint_id}/test` runs the full risk pipeline and returns the decision **without touching the broker**. This is the feature users will rely on most — treat it as first-class, not a debug hook.

---

## 9. Broker adapter

One abstract base class; implement it exactly once, for Binance **testnet** (spot, market + limit).

```python
async def get_account_state() -> AccountState      # equity, free balance, positions
async def get_instrument(symbol) -> Instrument     # tick size, lot step, min qty, min notional
async def submit_order(order: OrderRequest) -> BrokerOrder
async def cancel_order(broker_order_id) -> None
async def close_all_positions() -> list[BrokerOrder]
async def stream_fills() -> AsyncIterator[Fill]
```

Every method has a timeout and bounded retries with exponential backoff **only on idempotent operations**. A client-supplied order ID goes to the exchange so a retry can never double-submit. Map all broker errors into a small internal error enum — **never leak raw broker exceptions upward**.

---

## 10. Execution and reconciliation

- Submit entry + protective stop as an atomic unit where the exchange supports it (OCO / bracket). Where it doesn't: submit the entry, and if stop placement then fails, **immediately close the position and alert loudly**. Never leave a naked position.
- A reconciliation loop every N seconds pulls the broker's ground truth for orders and positions and repairs local state. **The broker is the source of truth; local state is a cache.**
- Handle partial fills, rejected orders, and orders that filled while we thought they'd failed.
- **Kill switch:** cancel all open orders → close all positions at market → set account `LOCKED` → notify. A single idempotent endpoint that works even if the risk engine or WebSocket layer is down.

---

## 11. API and frontend

REST: auth, risk-profile CRUD, broker-account CRUD, decisions list (filterable by reason code), orders, positions, equity curve, kill switch, webhook test. WebSocket `/ws` pushes new decisions, order status changes, position updates, equity ticks.

1. **Live** — streaming decision feed colour-coded by verdict, each rejection showing a human-readable reason; open positions table; big red kill-switch button with a confirmation step.
2. **Risk profile** — a form for every parameter in §7, each with a plain-language explanation and a live preview: *"given $10,000 equity, a $62,000 entry and a $61,000 stop, this sizes 0.161 BTC."*
3. **History** — equity curve (Lightweight Charts), trade log, rejections broken down by reason code.
4. **Setup** — webhook URL, secret, a copy-paste-ready TradingView alert template, and the test button.

---

## 12. Security

- Broker credentials envelope-encrypted (AES-GCM) with a key from the environment; decrypted **only in memory at the moment of use**.
- Document that users must create broker API keys with **trading enabled and withdrawals disabled**, IP-restricted to the server. Refuse to save a key the exchange reports as having withdrawal permission, where the API exposes that.
- Argon2id password hashing. Session cookies `HttpOnly`, `Secure`, `SameSite=Lax`.
- Structured JSON logging with an automatic redaction filter over a denylist of secret-bearing field names.

---

## 13. Testing — not optional

Because `risk/` is pure, it is fully unit-testable. Required before **any** phase counts as done:

- Table-driven test per rule in §7: pass, fail, and the boundary exactly at the threshold.
- Sizing: normal case, stop distance ~0, qty rounding down below minimum, insufficient balance, fee/slippage buffer applied.
- Rule ordering: a payload violating several rules returns the highest-priority reason code.
- Idempotency: the same alert submitted 5× concurrently produces exactly **1** order.
- Daily drawdown across a DST boundary and across a simulated restart.
- Circuit breaker: resets on a win, persists across restart.
- Property-based (Hypothesis): for any valid inputs, realized loss at the stop never exceeds `risk_per_trade_pct` of equity.
- A fake broker adapter that can be told to time out, partially fill, reject, and double-fill.

**No test may touch a real network.**

### Out of scope for v1 — do not build, scaffold, or abstract for

Billing / subscription tiers / quotas · multiple brokers (design the interface, implement exactly one) · Interactive Brokers, MetaTrader, options, futures, margin, leverage · backtesting, strategy authoring, arbitrary charting · mobile app · Discord (Telegram first) · team / multi-user accounts.

---

## 14. Phases — stop after each one

| Phase | Deliverable | Verifiable by |
|---|---|---|
| **0 — Plan** | No code. Restate the project, list assumptions, list open questions, propose the file tree. | Sign-off on §5 layout and §6 columns |
| **1 — Skeleton** | Compose (Postgres, Redis, API), FastAPI app, health check, migrations, config, structured logging | `docker compose up` works, `/health` green |
| **2 — Risk engine** | The pure `risk/` package + full test suite. No HTTP, no DB, no broker. | All §13 tests pass |
| **3 — Ingress** | Webhook endpoint, HMAC auth, schema validation, dedupe, alert + decision persistence, `/test` | Duplicate alert → 1 decision |
| **4 — Execution** | Binance testnet adapter, order submission, stop placement, reconciliation, fills → trades → PnL | Testnet round-trip |
| **5 — Dashboard** | Next.js frontend, WebSocket feed, all four pages, kill switch | Kill switch flattens testnet |
| **6 — Ops** | Telegram notifications, deploy docs, 3am runbook | `docs/runbook.md` |

**Current phase: 8 — Wiring.** Phases 0–7 are built and green (367 backend tests, `ruff`, `mypy --strict`, frontend `tsc` + `build`), but the 2026-08-01 audit found the layers were never joined: `ingress/` still runs on a placeholder broker and never calls `execution/`, so an approved decision submits no order. Phase 8 closes that. See [`PROJECT_MANAGEMENT.md`](./PROJECT_MANAGEMENT.md) — it is the authority on task status; this line is a pointer, not a second source of truth.

---

## 15. Open questions — must be answered before any code

1. What happens to an in-flight order when the kill switch fires mid-submission?
2. If the broker is unreachable when an alert arrives, do we queue or reject? (Argue for one; the human decides.)
3. How is `liquid_equity` defined when positions are open — mark-to-market, or cash only?
4. If a user edits their risk profile while a position is open, does the change apply retroactively?
5. What is the failure mode if Redis is down but Postgres is up?

Until these are answered, do not write application code.
