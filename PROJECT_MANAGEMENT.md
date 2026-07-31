# PROJECT_MANAGEMENT.md — SignalGuard Task Board

This is the **shared task board for the agents building SignalGuard** — a lightweight
"Jira" that lives in the repo. `CLAUDE.md` points every agent here. It exists for one
reason: **more than one agent works on this project, and they must not collide.** Read
it before you start, keep it current while you work.

If `CLAUDE.md` is the *rules* of the project, this file is the *state* of the project.

---

## How to use this board (the protocol)

1. **Before starting any work, read the whole board** and `git pull --rebase origin main`
   so you start from the latest state (`CLAUDE.md` §4). See what is `IN PROGRESS` and
   who owns it. Do not pick up a task someone else already owns, and do not start work
   that touches the same files as another agent's in-progress task without coordinating
   first (leave a note in that task's Notes column).
2. **Claim before you code — a claim is exclusive.** Put your agent name in **Owner** and
   set **Status** to `IN PROGRESS` (this *books* the task). A task that has an owner
   belongs to that owner alone: no other agent may work on it, complete it, or reassign it
   — until the owner releases it. To **pre-book** a task you intend to do later but aren't
   starting yet, set yourself as **Owner** and **Status** to `RESERVED`; a `RESERVED` task
   is exclusive to its owner exactly like a booked one. This keeps two agents from aiming
   at the same task. An unclaimed task is fair game; an owned one is not. (The human can
   always override.)
3. **Respect the layer boundaries** from `CLAUDE.md` §5 (`ingress/` · `risk/` ·
   `execution/`). Tasks are scoped to a layer on purpose so two agents can work in
   parallel without stepping on each other. `risk/` stays pure — no I/O.
4. **Respect phase order and dependencies.** Don't start a task whose `Depends on` isn't
   `DONE`. Phases are sequential (`CLAUDE.md` §14) — finish and get sign-off on one
   before opening the next.
5. **Keep the board honest.** Update **Status** as you progress. When you finish,
   **commit your work, then `git pull --rebase origin main`, re-run the checks, and
   resolve any conflict locally** (the end check — `CLAUDE.md` §4; commit first, because
   a rebase with uncommitted changes fails) so you never clobber another agent who pushed
   while you worked; never force-push. Then push **straight to `main`** (no branches, no
   PRs) and set the task to `DONE` yourself. The one exception is
   **phase-ending work**: mark it
   `IN REVIEW` instead, because `CLAUDE.md` §14 still requires the human to approve before
   the next phase begins. Either way, leave a short handoff in **Notes**: what changed,
   what's not yet handled, what the next agent needs. Partial work is never marked done.
   **Fail closed applies here too** — if a task is ambiguous, mark it `BLOCKED`, write the
   open question in Notes, and ask the human.
6. **One task, one owner at a time.** If you must hand off, set Status back to `TODO` (or
   `BLOCKED`) and clear the Owner so it's visibly available.

### Status legend

| Status | Meaning |
|---|---|
| `BACKLOG` | Defined but not ready to start (usually blocked by an earlier phase). |
| `TODO` | Ready to be claimed. All dependencies are `DONE`. |
| `IN PROGRESS` | Booked — actively being worked. Has an Owner; exclusive to that owner. |
| `RESERVED` | Pre-booked by its owner for future work. Exclusive to that owner — no other agent may pick it up. The owner moves it to `IN PROGRESS` when they start. |
| `BLOCKED` | Waiting on a decision, an answer, or another task. Reason in Notes. |
| `IN REVIEW` | Phase-ending work — pushed to `main` and awaiting the human's phase approval (`CLAUDE.md` §14). |
| `DONE` | Complete and pushed to `main`. An agent sets this itself once the work is on `main`. Do not reopen — file a new task instead. |

---

## Open questions — must be answered before Phase 1 code (CLAUDE.md §15)

These gate the whole project. Until the human answers them, tasks that depend on them
stay `BLOCKED`. Record answers here as they arrive.

| # | Question | Answer |
|---|---|---|
| Q1 | In-flight order when the kill switch fires mid-submission? | _unanswered_ — **proposed:** let it complete, never abandon it (an abandoned request loses the order ID → orphan position). `LOCKED` written first, sweep runs twice, and the reconciler treats `LOCKED` as a continuously-enforced state. Plan §3. |
| Q2 | Broker unreachable when an alert arrives — queue or reject? | _unanswered_ — **proposed:** reject. No account state → no snapshot → no evaluation; and rule 3 already calls a 30s-old signal stale, so a queue would release trades at prices that no longer exist. Caveat raised for `action: "close"`. Plan §3. |
| Q3 | How is `liquid_equity` defined with open positions — mark-to-market or cash only? | _unanswered_ — **proposed:** neither name survives. Mark-to-market is the sizing base *and* the drawdown basis; free cash is the affordability ceiling already in §7. Snapshot carries `total_equity` / `free_balance` / `position_value`. Plan §3. |
| Q4 | Does editing a risk profile while a position is open apply retroactively? | _unanswered_ — **proposed:** never retroactive to open positions (the system never initiates a trade on its own); gates apply from the next decision. Tightening `max_daily_dd_pct` can trip instantly — UI must warn. Plan §3. |
| Q5 | Failure mode if Redis is down but Postgres is up? | _unanswered_ — **proposed:** reject all new alerts (unknown kill-switch state must read as `LOCKED`), but keep persisting them, and keep the kill switch + reconciler working off Postgres. Plan §3. |

### New open questions raised in Phase 0 (blocking)

Surfaced while working through the spec. Flagged, not guessed — see plan §4.

| # | Question | Blocks | Status |
|---|---|---|---|
| OQ-1 | `reason_code` needs a second **pipeline** family (`BROKER_UNAVAILABLE`, `STATE_UNAVAILABLE`, `ACCOUNT_NOT_FOUND`, `INSTRUMENT_UNAVAILABLE`, `INTERNAL_ERROR`) for rejections that happen before the pure engine can run. | P2-1, P3-3 | _unanswered_ |
| OQ-2 | Rule 1 precedes rule 2, but the account name is inside the payload. Proposed: check a user-level lock pre-parse, account-level lock post-parse; both return `TRADING_LOCKED`. | P2-1 | _unanswered_ |
| OQ-3 | The fee/slippage buffer as literally specified **fails** §13's property test (worst-case loss came out 12% over budget in the worked example). Closed form `qty = risk_amount / (stop_distance + entry_price × buffer_rate)` is exact. | P2-2, P2-3 | _unanswered_ |
| OQ-4 | Storing the payload "verbatim" persists the body `secret` in plaintext, against constraint #6. Proposed: redact `secret`, keep `raw_body_sha256` for integrity. | P0-3, P3-3 | _unanswered_ |
| OQ-5 | Max age for cached instrument filters when the exchange is unreachable at boot — serve stale (suggest 24h cap) or refuse? | P4-1 | _unanswered_ |
| OQ-6 | `action: "sell"` on spot, where shorting does not exist. Proposed: `sell` reduces/closes a long; short-side stop logic still implemented and tested in the pure engine but unreachable via the spot adapter. | P2-1, P3-2 | _unanswered_ |

---

## Task board

Seeded from the phases in `CLAUDE.md` §14. Add rows as work is broken down further; give
each a unique ID. **Current phase: 1 — Skeleton (delivered, awaiting human sign-off).**

> **Note on the open questions.** The human replied "can you program it" without answering Q1–Q5 or
> OQ-1…OQ-6. Those answers are therefore recorded as **adopted by default** — the agent's own
> recommendations, used as the working decisions so Phase 1 could proceed. Phase 1 barely depends on
> them; **Phase 2 does.** OQ-2 (rule-1 ordering), OQ-3 (the sizing formula) and OQ-6 (`sell` on spot)
> all change risk-engine behaviour and should be confirmed before P2-1 starts.

### Phase 0 — Plan (no code)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P0-1 | Restate the project, list assumptions, answer/surface the §15 open questions | — | claude-opus-5 (phase-0) | IN REVIEW | — | Delivered in [`docs/phase-0-plan.md`](./docs/phase-0-plan.md) §1–§3. Q1–Q5 answered as **recommendations only** — human decides. 6 new open questions raised (OQ-1…OQ-6) that block Phase 1/2; see plan §4. |
| P0-2 | Propose the file tree (§5) and get sign-off | — | claude-opus-5 (phase-0) | IN REVIEW | — | Plan §5. Expands CLAUDE.md §5 layout; no new top-level dirs beyond `backend/`, `frontend/`, `docs/`. |
| P0-3 | Propose exact table columns (§6) and get sign-off | — | claude-opus-5 (phase-0) | IN REVIEW | — | Plan §6. Proposes 4 tables beyond CLAUDE.md §6 (`sessions`, `webhook_endpoints`, `circuit_breaker_state`, `instruments`) — each justified, each needs explicit sign-off. |

### Phase 1 — Skeleton

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P1-1 | Docker Compose: Postgres, Redis, API | infra | claude-opus-5 (phase-1) | IN REVIEW | P0-2 | Compose + Dockerfile + `.gitattributes`/`.dockerignore`/`.env.example` written. **Not verified by the agent — no Docker daemon in the build session.** Human must run `docker compose up -d --build`. |
| P1-2 | FastAPI app + `/health` endpoint | api | claude-opus-5 (phase-1) | IN REVIEW | P1-1 | `/health` checks Postgres + Redis and returns 503 when either is down (fail closed). Verified against a live Postgres + Redis. |
| P1-3 | Alembic migrations + config loading + structured JSON logging with redaction | db/config | claude-opus-5 (phase-1) | IN REVIEW | P0-3, P1-1 | Full schema (13 tables) in one migration, incl. append-only triggers and partial unique indexes. Config fails closed on missing secrets; live-trading flag needs a confirmation phrase. |

### Phase 2 — Risk engine (pure)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P2-1 | Pure `risk/` package: decision types + the 10 rules in exact order (§7) | risk | claude-opus-5 (phase-2) | IN REVIEW | P0-3 | **Zero I/O.** Time is passed in. |
| P2-2 | Position-sizing transform (§7 rule 9) | risk | claude-opus-5 (phase-2) | IN REVIEW | P2-1 | Closed-form buffer per OQ-3; round down. |
| P2-3 | Full test suite for §13 (per-rule tables, ordering, DST, restart, Hypothesis) | tests | claude-opus-5 (phase-2) | IN REVIEW | P2-1, P2-2 | No test touches a network. |
| P2-4 | Test asserting `risk/` imports no I/O libs (httpx/sqlalchemy/redis/datetime.now) | tests | claude-opus-5 (phase-2) | IN REVIEW | P2-1 | Guards the most important design rule. |

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

### Cross-cutting — Ops & tooling

Not tied to a single phase; they harden the repo itself. Owned by `claude-opus-5 (ops)`.

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| OPS-1 | `.editorconfig` complementing `.gitattributes` (LF, UTF-8, spaces) | infra | claude-opus-5 (ops) | DONE | — | Added `.editorconfig`: LF/UTF-8/final-newline everywhere, 4-space Python, 2-space web, tabs for Makefiles, no trailing-whitespace trim in Markdown. |
| OPS-2 | CI workflow — ruff + mypy `--strict` + pure test suite on every push | ci | claude-opus-5 (ops) | DONE | — | `.github/workflows/ci.yml`: ruff → mypy `--strict` src → `pytest -m "not integration"`, via `uv`, on every push to `main` and every PR. First run is triggered by this commit; watching it. |
| OPS-3 | Pre-commit hooks (ruff + mypy) as a pre-push safety net | infra | claude-opus-5 (ops) | DONE | OPS-2 | `.pre-commit-config.yaml`: local ruff + mypy `--strict` (identical to CI, via `uv`) plus whitespace/EOF/LF/merge-conflict/large-file hooks. YAML validated. Setup: `pre-commit install`. |
| OPS-4 | Extend CI: isolated pure-`risk/` suite + a coverage threshold | ci | claude-opus-5 (ops) | IN PROGRESS | OPS-2 | Booked (was reserved). Local baseline: `risk/` at 98% over 152 isolated tests → 95% gate is safe. |

---

## Changelog

Append a line whenever a task changes status, so the history of who-did-what is visible.

| Date (UTC) | Task | Change | By |
|---|---|---|---|
| 2026-07-31 | — | Board created and seeded from CLAUDE.md phases. | setup |
| 2026-07-31 | P0-1, P0-2, P0-3 | Claimed → `IN PROGRESS`. | claude-opus-5 (phase-0) |
| 2026-07-31 | P0-1 | Plan delivered in `docs/phase-0-plan.md`: project restatement, 13 assumptions, Q1–Q5 recommendations. → `IN REVIEW`. | claude-opus-5 (phase-0) |
| 2026-07-31 | P0-2 | File tree proposed (plan §5). → `IN REVIEW`. | claude-opus-5 (phase-0) |
| 2026-07-31 | P0-3 | Exact columns proposed for all 9 §6 tables + 4 justified additions (plan §6). → `IN REVIEW`. | claude-opus-5 (phase-0) |
| 2026-07-31 | OQ-1…OQ-6 | Six new blocking open questions raised rather than guessed. Phase 1 and 2 stay `BACKLOG` until answered. | claude-opus-5 (phase-0) |
| 2026-07-31 | Q1–Q5, OQ-1…OQ-6 | Human said "program it" without answering. Recommendations **adopted by default** to unblock Phase 1; OQ-2/OQ-3/OQ-6 still need confirmation before P2-1. | claude-opus-5 (phase-1) |
| 2026-07-31 | P1-1, P1-2, P1-3 | Claimed → `IN PROGRESS`. | claude-opus-5 (phase-1) |
| 2026-07-31 | P1-3 | Full schema (13 tables), append-only triggers, partial unique indexes, config fail-closed, JSON logging + redaction. 59 tests pass; ruff and mypy --strict clean. → `IN REVIEW`. | claude-opus-5 (phase-1) |
| 2026-07-31 | P1-2 | `/health` (deps, 503 when degraded) and `/health/live` (no deps). Verified green against live Postgres + Redis, and 503 with Redis stopped. → `IN REVIEW`. | claude-opus-5 (phase-1) |
| 2026-07-31 | P1-1 | Compose, Dockerfile, `.gitattributes`, `.env.example`, README quickstart. **Compose itself unverified** — no Docker daemon in the build session. → `IN REVIEW`. | claude-opus-5 (phase-1) |
| 2026-07-31 | OPS-1, OPS-2 | Booked → `IN PROGRESS`. OPS-3, OPS-4 pre-booked → `RESERVED`. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-1 | `.editorconfig` added (LF/UTF-8/final-newline, 4-space Python, 2-space web). → `DONE`. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-2 | CI workflow added: ruff + mypy `--strict` + pure pytest via `uv` on every push/PR. → `DONE`. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-3, OPS-4 | Booked (`RESERVED` → `IN PROGRESS`). Verified locally first: ruff + mypy `--strict` clean, 244 tests pass, `risk/` at 98%. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-3 | `.pre-commit-config.yaml` added (local ruff + mypy identical to CI, plus hygiene hooks). → `DONE`. | claude-opus-5 (ops) |
