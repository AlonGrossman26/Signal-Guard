# PROJECT_MANAGEMENT.md — SignalGuard Task Board

This is the **shared task board for the agents building SignalGuard** — a lightweight
"Jira" that lives in the repo. `CLAUDE.md` points every agent here. It exists for one
reason: **more than one agent works on this project, and they must not collide.** Read
it before you start, keep it current while you work.

If `CLAUDE.md` is the *rules* of the project, this file is the *state* of the project.

---

## How to use this board (the protocol)

1. **Before starting any work, read the whole board.** See what is `IN PROGRESS` and
   who owns it. Do not pick up a task someone else already owns, and do not start work
   that touches the same files as another agent's in-progress task without coordinating
   first (leave a note in that task's Notes column).
2. **Claim before you code.** Put your agent name in **Owner** and set **Status** to
   `IN PROGRESS`. An unclaimed task is fair game; a claimed one is not.
3. **Respect the layer boundaries** from `CLAUDE.md` §5 (`ingress/` · `risk/` ·
   `execution/`). Tasks are scoped to a layer on purpose so two agents can work in
   parallel without stepping on each other. `risk/` stays pure — no I/O.
4. **Respect phase order and dependencies.** Don't start a task whose `Depends on` isn't
   `DONE`. Phases are sequential (`CLAUDE.md` §14) — finish and get sign-off on one
   before opening the next.
5. **Keep the board honest.** Update **Status** as you progress, and when you finish move
   it to `IN REVIEW` (never straight to `DONE` — the human signs off). Leave a short
   handoff in **Notes**: what changed, what's not yet handled, what the next agent needs.
   Partial work is never marked done. **Fail closed applies here too** — if a task is
   ambiguous, mark it `BLOCKED`, write the open question in Notes, and ask the human.
6. **One task, one owner at a time.** If you must hand off, set Status back to `TODO` (or
   `BLOCKED`) and clear the Owner so it's visibly available.

### Status legend

| Status | Meaning |
|---|---|
| `BACKLOG` | Defined but not ready to start (usually blocked by an earlier phase). |
| `TODO` | Ready to be claimed. All dependencies are `DONE`. |
| `IN PROGRESS` | Actively being worked. Has an Owner. |
| `BLOCKED` | Waiting on a decision, an answer, or another task. Reason in Notes. |
| `IN REVIEW` | Work is complete; awaiting human sign-off. |
| `DONE` | Signed off by the human. Do not reopen — file a new task instead. |

---

## Open questions — must be answered before Phase 1 code (CLAUDE.md §15)

These gate the whole project. Until the human answers them, tasks that depend on them
stay `BLOCKED`. Record answers here as they arrive.

| # | Question | Answer |
|---|---|---|
| Q1 | In-flight order when the kill switch fires mid-submission? | _unanswered_ |
| Q2 | Broker unreachable when an alert arrives — queue or reject? | _unanswered_ |
| Q3 | How is `liquid_equity` defined with open positions — mark-to-market or cash only? | _unanswered_ |
| Q4 | Does editing a risk profile while a position is open apply retroactively? | _unanswered_ |
| Q5 | Failure mode if Redis is down but Postgres is up? | _unanswered_ |

---

## Task board

Seeded from the phases in `CLAUDE.md` §14. Add rows as work is broken down further; give
each a unique ID. **Current phase: 0 — Plan (not started).**

### Phase 0 — Plan (no code)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P0-1 | Restate the project, list assumptions, answer/surface the §15 open questions | — | _unclaimed_ | TODO | — | Get sign-off before any code. |
| P0-2 | Propose the file tree (§5) and get sign-off | — | _unclaimed_ | TODO | — | Sign-off on the §5 layout. |
| P0-3 | Propose exact table columns (§6) and get sign-off | — | _unclaimed_ | TODO | — | Sign-off before writing any migration. |

### Phase 1 — Skeleton

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P1-1 | Docker Compose: Postgres, Redis, API | infra | _unclaimed_ | BACKLOG | P0-2 | `docker compose up` works. |
| P1-2 | FastAPI app + `/health` endpoint | api | _unclaimed_ | BACKLOG | P1-1 | `/health` returns green. |
| P1-3 | Alembic migrations + config loading + structured JSON logging with redaction | db/config | _unclaimed_ | BACKLOG | P0-3, P1-1 | Redaction denylist per §12. |

### Phase 2 — Risk engine (pure)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P2-1 | Pure `risk/` package: decision types + the 10 rules in exact order (§7) | risk | _unclaimed_ | BACKLOG | P0-3 | **Zero I/O.** Time is passed in. |
| P2-2 | Position-sizing transform (§7 rule 9) | risk | _unclaimed_ | BACKLOG | P2-1 | Round down; fee/slippage buffer first. |
| P2-3 | Full test suite for §13 (per-rule tables, ordering, DST, restart, Hypothesis) | tests | _unclaimed_ | BACKLOG | P2-1, P2-2 | No test may touch a real network. |
| P2-4 | Test asserting `risk/` imports no I/O libs (httpx/sqlalchemy/redis/datetime.now) | tests | _unclaimed_ | BACKLOG | P2-1 | Guards the most important design rule. |

### Phase 3 — Ingress

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P3-1 | `POST /webhook/{endpoint_id}`: HMAC auth, timestamp replay guard, body-secret fallback | ingress | _unclaimed_ | BACKLOG | P1-2 | Reject oversized bodies before parsing. |
| P3-2 | Strict schema validation + dedupe key (§8) + Redis rate limit | ingress | _unclaimed_ | BACKLOG | P3-1 | Same alert twice → 1 decision. |
| P3-3 | Persist alert + decision (append-only), run risk in background task | ingress/db | _unclaimed_ | BACKLOG | P2-1, P3-2 | Respond 200 in < 50 ms. |
| P3-4 | `POST /webhook/{endpoint_id}/test` — full pipeline, no broker | ingress | _unclaimed_ | BACKLOG | P3-3 | First-class, not a debug hook. |

### Phase 4 — Execution

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P4-1 | Broker abstract base + Binance testnet adapter (§9) | execution | _unclaimed_ | BACKLOG | P3-3 | Map broker errors to internal enum. |
| P4-2 | Order submission + protective stop (atomic/bracket, else close-and-alert) | execution | _unclaimed_ | BACKLOG | P4-1 | Never leave a naked position. |
| P4-3 | Reconciliation loop + fills → trades → PnL | execution | _unclaimed_ | BACKLOG | P4-1 | Broker is the source of truth. |
| P4-4 | Kill switch: cancel → close all → LOCKED → notify (idempotent endpoint) | execution | _unclaimed_ | BACKLOG | P4-1 | Must work even if risk/WS is down. |

### Phase 5 — Dashboard

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P5-1 | REST API: profiles, broker accounts, decisions, orders, positions, equity, kill switch | api | _unclaimed_ | BACKLOG | P4-x | Decisions filterable by reason code. |
| P5-2 | WebSocket `/ws` fed by Redis pub/sub | api | _unclaimed_ | BACKLOG | P5-1 | Decisions, orders, positions, equity. |
| P5-3 | Next.js frontend — Live, Risk profile, History, Setup pages | frontend | _unclaimed_ | BACKLOG | P5-1, P5-2 | Kill-switch button with confirm step. |

### Phase 6 — Ops

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P6-1 | Telegram notifications | notify | _unclaimed_ | BACKLOG | P4-4 | — |
| P6-2 | Deploy docs + `docs/runbook.md` (the 3am runbook) | docs | _unclaimed_ | BACKLOG | P5-3 | — |

---

## Changelog

Append a line whenever a task changes status, so the history of who-did-what is visible.

| Date (UTC) | Task | Change | By |
|---|---|---|---|
| 2026-07-31 | — | Board created and seeded from CLAUDE.md phases. | setup |
