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

## Open questions — ANSWERED (2026-08-02)

**All eleven are now decided.** They had been recorded as *adopted by default* — the agent's own
recommendations, used as working assumptions because the human never said yes or no. That is a
worse state than it looks: the engine was coherent and thoroughly tested **against those
assumptions**, so if one had been wrong the tests would have kept passing while the behaviour was
wrong. No amount of testing finds that; only a decision does.

On 2026-08-02 the human delegated the decisions explicitly ("you answer, give the best answer").
They are recorded below as **decisions of record** and are binding until the human overrides any of
them — which they may do at any time, at the cost of the code change noted in each row.

**Answering them honestly turned up three places where the code did not match the answer.** All
three were fixed in the same commit rather than left as a doc/code contradiction (`CLAUDE.md` §2).

| # | Question | Decision | Code state |
|---|---|---|---|
| Q1 | In-flight order when the kill switch fires mid-submission? | **Let it complete; never abandon it.** You cannot un-send an HTTP request, and abandoning it loses the broker order ID — leaving an order at the exchange we have no record of, on a locked account. `LOCKED` is written first, the sweep runs twice, and the reconciler treats `LOCKED` as a continuously-enforced invariant. The race is not won, it is outlasted. | Matches. No change. |
| Q2 | Broker unreachable — queue or reject? | **Reject.** No account state → no snapshot → no evaluation, and rule 3 already calls a 30s-old signal stale, so a queue would release trades at prices that no longer exist. Queues also fail in bursts: eight signals firing at once after a ten-minute outage would pass exposure caps no single evaluation would have allowed. | **Changed.** The plan named the uncomfortable case — `sell`/`close` are risk-*reducing*, so rejecting one leaves the user holding a position they asked to exit — and said it must be loud. It was a log line. Added `Notifier.undelivered_exit` + `wiring.notify_undelivered_exit`. |
| Q3 | `liquid_equity` with open positions? | **Neither — the name is deleted.** Mark-to-market `total_equity` is the sizing base *and* the drawdown basis; `free_balance` is the affordability ceiling. Two numbers doing two jobs, each named for what it is. Pleasant side effect: a large unrealized loss shrinks both the sizing base and the drawdown numerator, so risk contracts automatically while losing. | Matches. `liquid_equity` appears nowhere in the code. |
| Q4 | Does a profile edit apply retroactively? | **Never to open positions; immediately to the next decision.** Resizing an open position would mean SignalGuard submitting an order no signal asked for. Gates (1,3,4,5,7,8,10) use the profile as it stands at evaluation time. `decisions.rule_snapshot` + `version` keep the audit answerable forever. | **Changed.** The one surprising case — tightening `max_daily_dd_pct` re-evaluates against today's *existing* baseline and can block instantly — was specified to need a UI warning and had none. Added to the Risk-profile form. |
| Q5 | Redis down, Postgres up? | **Reject every new alert, keep recording, keep the kill switch working.** Unknown kill-switch state must read as `LOCKED`; unknown breaker state as `OPEN`; dedupe cannot be guaranteed, and a duplicate is a double order. Ingest degrades to "record and refuse", which is the honest failure. Kill switch, lock reads and reconciliation all work off Postgres. (Companion: **Postgres down → also reject** — no audit trail, no trading.) | Matches. No change. |
| OQ-1 | A second **pipeline** reason-code family? | **Approved, not folded into `INVALID_PAYLOAD`.** Folding would make the dashboard's rejection breakdown lie about *why* trades were refused, and that breakdown is the product's main diagnostic. | Matches, plus one addition — see OQ-6. |
| OQ-2 | Rule 1 precedes rule 2, but the account is named inside the payload. | **Confirmed: check the lock twice.** User-level lock pre-parse (the endpoint identifies the user), account-level lock post-parse. Both return `TRADING_LOCKED`, preserving the specified priority. | Matches. No change. |
| OQ-3 | The fee/slippage buffer as literally specified. | **Closed form approved:** `qty = risk_amount / (stop_distance + entry_price × buffer_rate)`. The literal "subtract the buffer from `risk_amount` first" under-corrects because fees scale with *notional* while `risk_amount` is a slice of *equity* — worked example came out 12% over budget, and **it fails §13's own Hypothesis property test.** The closed form satisfies it by construction; rounding down only adds margin. This is a deliberate, documented deviation from `CLAUDE.md` §7's literal wording. | Matches, and property-tested. No change. |
| OQ-4 | Storing the payload "verbatim" persists the body `secret`. | **Approved: redact `secret`, keep `raw_body_sha256`.** Constraint #6 outranks the word "verbatim"; the hash preserves what redaction costs — the ability to settle an HMAC dispute. | Matches. No change. |
| OQ-5 | Max age for cached instrument filters. | **Serve from cache up to 24h, reject beyond it, refresh continuously in the background.** Refusing outright would turn a brief exchange blip into a full user outage; trusting the cache forever would size against filters that have changed. Refresh now runs hourly from the reconciliation cycle. | Matches. No change. |
| OQ-6 | `action: "sell"` on spot, where shorting does not exist. | **Confirmed:** `buy` opens/increases a long, `sell` and `close` both reduce/close one, and **a `sell` with nothing held is rejected — not read as a short.** Short-side stop arithmetic stays implemented and tested in the pure engine (it costs nothing and is needed the day a futures adapter arrives) but is unreachable through the spot adapter. | **Changed.** An exit on a flat symbol was returning `APPROVED` with quantity 0 — putting "APPROVED" in the decision feed for a signal that placed no order. Now rejects with a new `NO_POSITION_TO_CLOSE` code. |

*Also confirmed:* the §11 UI example ("$10,000 equity, $62,000 entry, $61,000 stop → 0.161 BTC") is
illustrative and does not match the §7 formula, which gives 0.1 BTC before buffers and 0.0889 with
the 20bps buffer. The dashboard computes it live rather than hardcoding the number.

---

### Policy questions answered by the human (2026-08-01)

Two decisions were needed before implementing the Phase 7 hardening; the human answered:

| # | Question | Answer |
|---|---|---|
| PQ-1 | Withdrawal-permission check (§12): what to do when we CAN'T confirm a key's withdrawal permission (testnet doesn't expose it, or exchange offline)? | **Save unless withdrawals are confirmed ON.** Refuse only on a confirmed withdrawal permission; allow + log a warning when unknown. Implemented in H-2. |
| PQ-2 | Per-user Telegram routing model? | **Shared app bot + per-user chat id.** One app-level bot token (env); each user stores their own chat id (falls back to the app-level chat). Implemented in H-3. |

## Task board

Seeded from the phases in `CLAUDE.md` §14. Add rows as work is broken down further; give
each a unique ID.

**Status (2026-08-01, after AUDIT-1 found the gaps and Phase 8 closed them).**

**Phases 0–8 are `DONE`. Every task on this board is complete except two verification
gates that this environment physically cannot run — V-1 and V-2, detailed below and
honestly marked `BLOCKED`, not `DONE`.**

The pipeline is now joined end to end. An approved decision submits a real entry and its
protective stop; the reconciliation loop runs; `trades`, `circuit_breaker_state`,
`equity_snapshots` and the `instruments` cache are all written by live code paths.

Verified on 2026-08-01 against live Postgres 16 + Redis:

| Check | Result |
|---|---|
| Backend suite | **391 passed** (367 before Phase 8, +24 new) |
| `ruff check .` | clean |
| `mypy --strict src` | clean, 60 source files |
| Frontend `tsc --noEmit` + `npm run build` | clean |
| **No test touches a real network (§13)** | **enforced, not assumed** — see the note below |
| Dashboard in a real browser (V-3) | **12/12 checks passed** — Chromium against the live stack |
| Live webhook → decision → audit trail | verified end to end against the running app |

**A defect Phase 8 introduced and Phase 8 fixed.** Wiring `ingress/` to `execution/`
silently turned an existing test (`tests/api/test_realtime_ingress.py`) into one that dialled
`testnet.binance.vision` on every run. It still *passed* — the call failed, the pipeline
fail-closed to `BROKER_UNAVAILABLE`, and the assertion was about something else — so nothing
went red; only the outbound proxy log showed it. §13's "no test may touch a real network" is
now enforced by a socket guard in `tests/conftest.py` that blocks non-loopback connections,
with `tests/test_no_network.py` proving the guard is armed. A suite that quietly depends on
an exchange being reachable is one that fails at 3am for reasons unrelated to the code — and,
with real keys in the environment, one that could place a real order.

> **Note on the open questions.** The human replied "can you program it" without answering Q1–Q5 or
> OQ-1…OQ-6. Those answers are therefore recorded as **adopted by default** — the agent's own
> recommendations, used as the working decisions so Phase 1 could proceed. Phase 1 barely depends on
> them; **Phase 2 does.** OQ-2 (rule-1 ordering), OQ-3 (the sizing formula) and OQ-6 (`sell` on spot)
> all change risk-engine behaviour and should be confirmed before P2-1 starts.

### Phase 0 — Plan (no code)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P0-1 | Restate the project, list assumptions, answer/surface the §15 open questions | — | claude-opus-5 (phase-0) | DONE | — | Delivered in [`docs/phase-0-plan.md`](./docs/phase-0-plan.md) §1–§3. Q1–Q5 answered as **recommendations only** — human decides. 6 new open questions raised (OQ-1…OQ-6) that block Phase 1/2; see plan §4. |
| P0-2 | Propose the file tree (§5) and get sign-off | — | claude-opus-5 (phase-0) | DONE | — | Plan §5. Expands CLAUDE.md §5 layout; no new top-level dirs beyond `backend/`, `frontend/`, `docs/`. |
| P0-3 | Propose exact table columns (§6) and get sign-off | — | claude-opus-5 (phase-0) | DONE | — | Plan §6. Proposes 4 tables beyond CLAUDE.md §6 (`sessions`, `webhook_endpoints`, `circuit_breaker_state`, `instruments`) — each justified, each needs explicit sign-off. |

### Phase 1 — Skeleton

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P1-1 | Docker Compose: Postgres, Redis, API | infra | claude-opus-5 (phase-1) | DONE | P0-2 | Compose + Dockerfile + `.gitattributes`/`.dockerignore`/`.env.example` written. **Not verified by the agent — no Docker daemon in the build session.** Human must run `docker compose up -d --build`. |
| P1-2 | FastAPI app + `/health` endpoint | api | claude-opus-5 (phase-1) | DONE | P1-1 | `/health` checks Postgres + Redis and returns 503 when either is down (fail closed). Verified against a live Postgres + Redis. |
| P1-3 | Alembic migrations + config loading + structured JSON logging with redaction | db/config | claude-opus-5 (phase-1) | DONE | P0-3, P1-1 | Full schema (13 tables) in one migration, incl. append-only triggers and partial unique indexes. Config fails closed on missing secrets; live-trading flag needs a confirmation phrase. |

### Phase 2 — Risk engine (pure)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P2-1 | Pure `risk/` package: decision types + the 10 rules in exact order (§7) | risk | claude-opus-5 (phase-2) | DONE | P0-3 | **Zero I/O.** Time is passed in. |
| P2-2 | Position-sizing transform (§7 rule 9) | risk | claude-opus-5 (phase-2) | DONE | P2-1 | Closed-form buffer per OQ-3; round down. |
| P2-3 | Full test suite for §13 (per-rule tables, ordering, DST, restart, Hypothesis) | tests | claude-opus-5 (phase-2) | DONE | P2-1, P2-2 | No test touches a network. |
| P2-4 | Test asserting `risk/` imports no I/O libs (httpx/sqlalchemy/redis/datetime.now) | tests | claude-opus-5 (phase-2) | DONE | P2-1 | Guards the most important design rule. |

### Phase 3 — Ingress

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P3-1 | `POST /webhook/{endpoint_id}`: HMAC auth, timestamp replay guard, body-secret fallback | ingress | claude-opus-5 (merged) | DONE | P1-2 | Reject oversized bodies before parsing. |
| P3-2 | Strict schema validation + dedupe key (§8) + Redis rate limit | ingress | claude-opus-5 (merged) | DONE | P3-1 | Same alert twice → 1 decision. |
| P3-3 | Persist alert + decision (append-only), run risk in background task | ingress/db | claude-opus-5 (merged) | DONE | P2-1, P3-2 | Respond 200 in < 50 ms. |
| P3-4 | `POST /webhook/{endpoint_id}/test` — full pipeline, no broker | ingress | claude-opus-5 (merged) | DONE | P3-3 | First-class, not a debug hook. |

### Phase 4 — Execution

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P4-1 | Broker abstract base + Binance testnet adapter (§9) | execution | claude-opus-5 (merged) | DONE | P3-3 | Map broker errors to internal enum. |
| P4-2 | Order submission + protective stop (atomic/bracket, else close-and-alert) | execution | claude-opus-5 (merged) | DONE | P4-1 | Never leave a naked position. |
| P4-3 | Reconciliation loop + fills → trades → PnL | execution | claude-opus-5 (merged) | DONE | P4-1 | Broker is the source of truth. |
| P4-4 | Kill switch: cancel → close all → LOCKED → notify (idempotent endpoint) | execution | claude-opus-5 (merged) | DONE | P4-1 | Must work even if risk/WS is down. |

### Phase 5 — Dashboard

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P5-1 | REST API: profiles, broker accounts, decisions, orders, positions, equity, kill switch | api | claude (phase-5) | DONE | P4-x | Full `api/` package under `/api`: session auth (Argon2id, revocable server-side sessions), risk-profile GET/PUT (partial, version-bump, money-as-string, tz validated), broker-account + webhook-endpoint CRUD (credentials write-only), decisions (reason-code filter + pagination) / orders / positions / equity read models, kill switch + unlock (reuses tested `execution.killswitch`). 40 integration tests; ruff + mypy `--strict` clean. Follow-ups now closed by H-1/H-2 (immediate broker sweep + withdrawal-permission check). |
| P5-2 | WebSocket `/ws` fed by Redis pub/sub | api | claude (phase-5) | DONE | P5-1 | Shared `realtime.py` publisher (per-user channel, money-as-strings, fail-soft), authenticated `/ws` endpoint (cookie auth, `connected`/`heartbeat` frames, read-only fan-out, per-user isolation). Decisions wired end-to-end through ingress; orders/positions/equity via an optional `event_sink` in the reconciler (off by default). 13 tests incl. real-Redis round-trip, live-webhook→event e2e, WS scoping, reconciler emission. Suite 335 green; ruff + mypy `--strict` clean. Branch `claude/task-booking-completion-o2trly`. |
| P5-3 | Next.js frontend — Live, Risk profile, History, Setup pages | frontend | claude (phase-5) | DONE | P5-1, P5-2 | Next.js 14 (App Router) + TS + Tailwind + Lightweight Charts. Login/register; `Shell` auth-guard + nav. **Live** (WS-fed decision feed colour-coded by verdict + REST backfill, positions, per-account kill switch with two-step confirm + unlock); **Risk profile** (§7 form + live sizing preview using the OQ-3 closed form, version bump on save); **History** (equity curve via Lightweight Charts + decisions-by-reason-code); **Setup** (broker account create, webhook endpoint mint with token/secrets shown once, TradingView template, test-signal button hitting `/test`). Typed API client (`credentials: include`), auto-reconnecting `useWebSocket`. `npm run build` + `tsc --noEmit` both clean on patched next 14.2.35. Backend: added credentialed CORS (`CORS_ALLOW_ORIGINS`, default `:3000`). |

### Phase 6 — Ops

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P6-1 | Telegram notifications | notify | claude (phase-5) | DONE | P4-4 | `notify/` package: `TelegramClient` (httpx, timeout, bounded retries on transient/5xx/429, token kept out of logs and errors) + `Notifier` (kill-switch/naked-position/circuit-breaker/drawdown messages, HTML-escaped, **fail-soft** — a send failure never breaks the caller). Wired into the kill-switch endpoint best-effort. 14 tests via httpx MockTransport (no network); ruff + mypy `--strict` clean. Per-user routing added by H-3. |
| P6-2 | Deploy docs + `docs/runbook.md` (the 3am runbook) | docs | claude (phase-5) | DONE | P5-3 | `docs/runbook.md` (scenario-driven 3am runbook: fastest actions + kill-switch incl. DB fallback, per-dependency triage, migrations, backups, rollback, reason-code appendix) and `docs/deploy.md` (prereqs, config, compose up, migrations, health, first-user/webhook, WS, ops reference). All commands + endpoints verified against the code. Dep on P5-3 was nominal — docs cover the running backend stack and are valid now. |

### Phase 7 — Hardening (post-review follow-ups)

Closed the three gaps that were flagged as "not yet handled" on P5-1/P6-1, after the
human answered the two open policy questions (see the open-questions section).

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| H-1 | Kill-switch immediate broker sweep | execution/api | claude (phase-5) | DONE | P5-1 | `execution/factory.py` builds the real Binance testnet adapter from an account's encrypted creds (decrypted only at use); `provide_kill_switch_broker` now returns it, so firing the kill switch cancels + closes immediately (falls back to lock-only + reconciler when creds can't be decoded). Unit tests for the factory + adapter permission call; all kill-switch endpoint tests inject fakes so no test hits the network. |
| H-2 | Withdrawal-permission key check (§12) | api/execution | claude (phase-5) | DONE | P5-1 | Adapter `get_withdrawal_enabled()` (True/False/None); account-create refuses only when withdrawals are **confirmed ON**, saves + logs when unknown (testnet doesn't expose it / exchange offline) — per the human's answer. Injectable checker (`provide_key_checker`) so tests never touch the network. |
| H-3 | Per-user Telegram routing | notify/db/api | claude (phase-5) | DONE | P6-1 | Shared app bot + per-user chat id (human's answer). Migration `0002` adds `users.telegram_chat_id`; `GET`/`PUT /api/notifications`; `notifier_for_user` routes to the user's chat, falling back to the app-level chat. Frontend: a Telegram card on Setup. 10 tests (notifications API + per-user notifier). |

### Phase 8 — Wiring (join the layers into a working pipeline)

Raised by AUDIT-1 and **completed on 2026-08-01**. The layers are now joined: an approved
decision submits a real entry and its protective stop, the reconciliation loop runs, and every
table the audit found empty is written by a live code path.

The composition root is a new module, [`wiring.py`](./backend/src/signalguard/wiring.py). It
exists because `CLAUDE.md` §5 says `ingress/` "knows nothing about brokers" — which is exactly
why the two layers were never joined: there was no legal place for the wire. `wiring.py` sits
*above* both and hands ingress a pair of plain callables, so `ingress/` still imports no broker
and `execution/` still imports no HTTP handling.

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| P8-1 | Replace the placeholder broker state provider with the real adapter | ingress/execution | claude-opus-5 (phase-8) | DONE | — | Was `ingress/routes.py` `_null_account_state` (zero equity) on both endpoints, so every live alert rejected `SIZE_BELOW_MINIMUM`. Now `wiring.BrokerSession` builds the real adapter from the account's encrypted credentials and converts broker positions into the engine's `OpenPosition`. One adapter per alert, reused for state + price + submission, closed in a `finally` — three adapters would mean three decrypted credential sets in memory (§12). The `/test` endpoint keeps a zeroed provider *by design*: §8 says it must never touch the broker; renamed `_no_broker_state` and documented so it is not mistaken for the old stub. |
| P8-2 | Execute `APPROVED` decisions — actually submit the order | ingress/execution | claude-opus-5 (phase-8) | DONE | P8-1, P8-4 | New `execution/executor.py` + `wiring.execute_approved`. A rejection submits nothing; an approved entry goes through `submit_entry_with_stop` (no path around the stop); an exit closes what is held at market. **Ordering is constraint #5 taken literally** — the decision is committed *before* any order is submitted, so a crash between them leaves an audit record and a reconcilable order, never an order with no decision behind it. |
| P8-3 | Supply a market reference price for MARKET orders | ingress | claude-opus-5 (phase-8) | DONE | P8-1 | `pipeline.evaluate_alert` now takes a `reference_price_provider` (injected, so ingress still knows no brokers) called after the account resolves. A fetch failure returns None rather than raising, so the miss becomes a proper rule-6 rejection with a reason code instead of an exception. LIMIT orders still use their own price. |
| P8-4 | Populate the `instruments` cache at startup | execution/db | claude-opus-5 (phase-8) | DONE | — | New `execution/instruments.py`: `refresh_instruments` upserts the full `exchangeInfo` set; `refresh_if_stale` runs it from each reconciliation cycle at most hourly, well inside the OQ-5 24h read cap. Upsert not delete-and-insert — a symbol briefly missing from `exchangeInfo` must not lose its cached filters, or the read path would reject every alert for it. |
| P8-5 | Start the reconciliation loop | execution | claude-opus-5 (phase-8) | DONE | P8-1 | Started in `main.lifespan` as a tracked task with a stop event and a bounded 20s drain on shutdown. Off only via the new `RECONCILER_ENABLED`, which logs a **warning** at boot so a disabled reconciler is never silent. An account whose adapter will not build is skipped with a log line instead of killing the cycle — verified live: the running app logged `Could not build a broker adapter; skipping this account` and kept reconciling. `docs/runbook.md` and `docs/deploy.md` updated in the same commit (§2). |
| P8-6 | Write `equity_snapshots` so drawdown has a baseline | execution/db | claude-opus-5 (phase-8) | DONE | P8-5 | Two fixes. The reconciler writes ticks and lazily creates the daily baseline (as before, but it now actually runs); and `pipeline.load_drawdown` now **persists** the baseline it returns, inside a SAVEPOINT so losing the race cannot take the alert row down. The old comment claimed "the caller persists it" and no caller did — meaning every alert re-baselined against current equity, which quietly made the daily drawdown limit unable to trip. |
| P8-7 | Fills → `trades` → realized PnL | execution/db | claude-opus-5 (phase-8) | DONE | P8-2, P8-5 | New `execution/trades.py` `build_trades`: matches a filled STOP/EXIT to the filled ENTRY sharing its `decision_id`, nets fees out of realized PnL, and skips exits with no priced entry rather than inventing one. Idempotent — an exit already referenced by a trade is never re-recorded, so running every cycle is safe. |
| P8-8 | Persist `circuit_breaker_state` | execution/db | claude-opus-5 (phase-8) | DONE | P8-7 | `recompute_circuit_breaker` recounts the streak from the `trades` table using the existing pure `count_consecutive_losses`, and upserts `circuit_breaker_state` (Postgres) plus a Redis mirror (§7 wants both). **Recompute, not increment**: idempotent by construction, so a crash mid-cycle cannot double-count a loss. An already-open breaker keeps its original cooldown rather than re-arming it every cycle — re-arming would mean it never expires. |
| P8-9 | Wire the three unused notifier calls | notify | claude-opus-5 (phase-8) | DONE | P8-2 | `naked_position` fires from `submit_entry_with_stop` when a stop fails (whether or not the emergency close succeeded — a closed position is still an incident). `circuit_breaker_open` fires once on the transition into OPEN, `daily_drawdown_hit` once on the tick that crosses the limit, both from the reconciler so they reach the user at 3am with no signal pending. All routed per-user via `wiring.notifier_for_account` (H-3's shared-bot model). |
| P8-10 | `GET /api/trades` + History trade log | api/frontend | claude-opus-5 (phase-8) | DONE | P8-7 | `GET /api/trades` (owner-scoped, paginated, money as strings) + `TradeResponse`. History page gained a Trade log table; loss/gain colour is decided by a leading `-` on the string, so no float enters the render (constraint #3). Verified rendering in a real browser. |
| P8-11 | Honour the user's configured `dedupe_window_sec` | ingress | claude-opus-5 (phase-8) | DONE | — | `routes.py` now reads the user's `dedupe_window_sec` before claiming the key, instead of hardcoding 60 while rule 4's rejection message quoted the user's configured value back at them. |

### Phase 8 — Test gaps found by the audit

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| T-1 | §13 idempotency: 5 concurrent alerts → exactly **1 order** | tests | claude-opus-5 (phase-8) | DONE | P8-2 | `tests/ingress/test_execution_e2e.py`: five genuinely concurrent POSTs (`asyncio.gather`) of one signal → exactly **1 entry and 1 stop**, asserted on the `orders` table, not on decisions. This is §13's requirement stated in its own terms for the first time. |
| T-2 | End-to-end: alert → decision → order → position | tests | claude-opus-5 (phase-8) | DONE | P8-2, P8-5 | Same file: an approved alert produces an entry **and** a covering stop (`stop.qty == entry.qty`), and a rejected alert produces nothing at all. This is the regression guard for all of Phase 8 — its absence is precisely why the missing call site survived two phase sign-offs. Plus `test_trades_and_breaker.py` (8) and `test_instruments_and_executor.py` (8). |
| T-3 | `tests/test_config.py::test_valid_config_loads` reads ambient env | tests | claude-opus-5 (phase-8) | DONE | — | Fixed with a `clean_env` fixture that clears ambient `APP_ENV`/`LOG_LEVEL`/`RECONCILER_ENABLED` before asserting defaults. Added tests that the reconciler defaults to on and that its interval is bounded. |

### Verification gates

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| V-3 | Dashboard in a browser against a live backend | frontend | claude-opus-5 (phase-8) | DONE | P8-1 | **Done, for the first time.** Chromium driven against the real stack (uvicorn + Postgres 16 + Redis + `next start`): **12/12 checks passed** — register → dashboard, Live feed + kill switch present, Setup, Risk-profile form with a live sizing preview, History showing the equity curve, the new Trade log and the reason-code breakdown, a real `/ws` connection returning `{"type":"connected"}`, and zero console errors. The one error found was a 404 for a missing favicon; fixed (`public/icon.svg` + layout metadata) rather than waved away, because a dashboard that always logs a console error trains its operator to ignore console errors. |
| V-1 | Run `docker compose up -d --build` and confirm `/health` | infra | — | BLOCKED | — | **Partially verified; cannot be finished in this environment.** Progress: the Docker daemon now runs here, and `docker compose config` validates the file and resolves every `.env` interpolation. Blocker: pulling `postgres:16-alpine` / `redis:7-alpine` / the Python base image is refused by the sandbox network policy — `production.cloudfront.docker.com` returns **403 to CONNECT**. Not a code problem and not fixable from inside; it needs a host that can reach Docker Hub. **What this does and does not tell you:** the same three processes (API + Postgres 16 + Redis) were run natively here with migrations applied and `/health` green, so the *stack composition* works — but the `Dockerfile` and the compose runtime themselves remain unproven. |
| V-2 | Binance **testnet round-trip** | execution | — | BLOCKED | P8-2 | **Cannot be run here, and no substitute was accepted as proof.** `testnet.binance.vision:443` is refused by the sandbox network policy (403 to CONNECT, confirmed in the proxy's own failure log). Needs testnet API keys and outbound access. **What is proven instead:** the ingress→execution path is real and was exercised end-to-end against the live app — a webhook produced a persisted decision of `BROKER_UNAVAILABLE`, a reason code that is *only reachable when the code actually tries to talk to a broker* (before Phase 8 the same alert returned `SIZE_BELOW_MINIMUM` from the zeroed placeholder). Order submission itself is covered end-to-end against the fake broker (T-1, T-2). **Still unverified: the adapter's request/response mapping against the real exchange.** That is the one thing only V-2 can establish. |

### Open follow-up (found 2026-08-02 while writing the getting-started guide)

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| F-1 | `/webhook/{id}/test` is weaker than §8 intends | ingress | — | TODO | — | `CLAUDE.md` §8 calls this "the feature users will rely on most" and says to treat it as first-class. Walking it as a new user shows three rough edges, all caused by it having no broker data: (1) **it can never return `APPROVED`** — no equity means no drawdown baseline, so rule 8 fails closed with `DAILY_DRAWDOWN_HIT — no valid equity baseline` on a perfectly good signal; (2) **market orders always report `NO_STOP_LOSS — no reference price available`**, so the most common order type cannot be tested meaningfully; (3) **an uncached symbol reports `INSTRUMENT_UNAVAILABLE` rather than `SYMBOL_NOT_ALLOWED`**, because the filter lookup precedes the rule chain — so the endpoint names a later-fixable cause instead of the user's actual mistake. Each is individually defensible fail-closed behaviour; together they make the feature much less useful than §8 describes. **Not fixed here** — the options (let `/test` read account state read-only, or feed it a declared hypothetical equity) are a design decision for the human, not something to improvise into the risk path. Documented honestly in `docs/getting-started.md` step 10 meanwhile. |

### Accepted deviations (decided, not gaps — do not "fix" without asking)

| ID | Item | Decision |
|---|---|---|
| D-1 | `stream_fills` raises `NotImplementedError` (`binance_testnet.py:398`) | Deliberate for v1: the reconciler polls broker truth, and a websocket user-data stream is a second source of the same information with its own reconnect and sequencing failure modes. Recorded here so it is not mistaken for an oversight — it is listed in the §9 interface, and the interface method exists. Revisit only if polling proves insufficient. |
| D-2 | Entry and stop are two requests, not an OCO/bracket (`execution/orders.py`) | §10 says "atomic where the exchange supports it, otherwise submit the entry and close immediately if the stop fails". The close-and-alert fallback is implemented and tested. If OCO is used on Binance spot later, the fallback path stays — it is what makes a naked position impossible. |

### Cross-cutting — Ops & tooling

Not tied to a single phase; they harden the repo itself. Owned by `claude-opus-5 (ops)`.

| ID | Task | Layer | Owner | Status | Depends on | Notes |
|---|---|---|---|---|---|---|
| OPS-1 | `.editorconfig` complementing `.gitattributes` (LF, UTF-8, spaces) | infra | claude-opus-5 (ops) | DONE | — | Added `.editorconfig`: LF/UTF-8/final-newline everywhere, 4-space Python, 2-space web, tabs for Makefiles, no trailing-whitespace trim in Markdown. |
| OPS-2 | CI workflow — ruff + mypy `--strict` + pure test suite on every push | ci | claude-opus-5 (ops) | DONE | — | `.github/workflows/ci.yml`: ruff → mypy `--strict` src → `pytest -m "not integration"`, via `uv`, on every push to `main` and every PR. First run is triggered by this commit; watching it. |
| OPS-3 | Pre-commit hooks (ruff + mypy) as a pre-push safety net | infra | claude-opus-5 (ops) | DONE | OPS-2 | `.pre-commit-config.yaml`: local ruff + mypy `--strict` (identical to CI, via `uv`) plus whitespace/EOF/LF/merge-conflict/large-file hooks. YAML validated. Setup: `pre-commit install`. |
| OPS-4 | Extend CI: isolated pure-`risk/` suite + a coverage threshold | ci | claude-opus-5 (ops) | DONE | OPS-2 | Added `risk-coverage` CI job: runs `tests/risk` with `--cov=signalguard.risk --cov-fail-under=95` (currently 98.31%). Added `pytest-cov` dep; ignore coverage artifacts. Verified locally; CI run #6 green. |
| OPS-5 | Dependabot config for backend deps + GitHub Actions | ci | claude-opus-5 (ops) | DONE | — | `.github/dependabot.yml`: weekly grouped update PRs for `pip` (/backend) and `github-actions` (/), limit 5 each. YAML + schema validated. |
| OPS-6 | Cache uv deps in CI to speed runs | ci | claude-opus-5 (ops) | DONE | OPS-2 | `enable-cache: true` + `cache-dependency-glob: backend/uv.lock` on setup-uv in both CI jobs. Cache invalidates when the lockfile changes. YAML validated. |
| AUDIT-1 | Read the whole codebase against `CLAUDE.md`; reconcile the board with reality | docs | claude-opus-5 (audit) | DONE | — | Full audit of all 56 backend modules + frontend against the §3 constraints, §7 rules, §9–§11 contracts and §13 test list. Verified the green checks independently (Postgres 16 + Redis started locally, migrations applied: **367 passed**, ruff clean, `mypy --strict` clean, frontend `tsc` + `build` clean). Found the layers were never joined: 11 wiring gaps, 3 test gaps, 2 accepted deviations — filed as **Phase 8**, none of them owned. No source code changed; board and `CLAUDE.md` §14 brought back in sync. |

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
| 2026-07-31 | OPS-4 | CI `risk-coverage` job added: `tests/risk` with a 95% coverage gate (at 98.31%). Added `pytest-cov`; ignore coverage artifacts. → `DONE`. CI run #6 green. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-5, OPS-6 | Added and pre-booked → `RESERVED` (dependabot config; uv CI cache). Dropped a ruff-format gate idea: `ruff format --check` would reformat 43/67 files — out of lane. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-5 | `.github/dependabot.yml` added (weekly grouped pip + github-actions updates). → `DONE`. | claude-opus-5 (ops) |
| 2026-07-31 | OPS-6 | uv dependency caching enabled on both CI jobs (keyed on `backend/uv.lock`). → `DONE`. | claude-opus-5 (ops) |
| 2026-07-31 | P5-1 | Booked → `IN PROGRESS`. Building the dashboard REST API (auth+sessions, risk-profile CRUD, broker-account+webhook CRUD, decisions/orders/positions/equity read models, kill switch). | claude (phase-5) |
| 2026-07-31 | P5-1 | Delivered the `api/` package (7 route modules + schemas + session security) and 40 integration tests. Full suite 326 pass vs live Postgres+Redis; ruff + mypy `--strict` clean. → `IN REVIEW`. Pushed to `claude/task-booking-completion-o2trly` (this session is branch-scoped, not `main`). | claude (phase-5) |
| 2026-07-31 | P3-x, P4-x | **Board/reality reconciliation (flag, not a claim of completion):** the Phase 3 (`0946bb6`) and Phase 4 (`595224f`) code is already merged and its tests pass against live PG+Redis, yet those rows still read `BACKLOG`. Left as-is pending the human — noting the discrepancy here so the board and codebase stop silently disagreeing. Human to confirm their real status. | claude (phase-5) |
| 2026-08-01 | P5-2, P5-3, P6-1, P6-2 | Booked the remaining Phase 5/6 work: P5-2 → `IN PROGRESS`; P5-3, P6-1, P6-2 pre-booked → `RESERVED`. | claude (phase-5) |
| 2026-08-01 | P5-2 | Realtime publisher + `/ws` endpoint; decision fan-out wired through ingress, orders/positions/equity via optional reconciler sink. 13 tests; suite 335 green; ruff + mypy `--strict` clean. → `IN REVIEW`. | claude (phase-5) |
| 2026-08-01 | P6-1 | `notify/` Telegram client + fail-soft Notifier; wired into the kill switch. 14 pure tests (MockTransport). Suite 349 green; ruff + mypy `--strict` clean. → `IN REVIEW`. | claude (phase-5) |
| 2026-08-01 | P6-2 | `docs/runbook.md` (3am runbook) + `docs/deploy.md`; commands/endpoints verified against code. → `IN REVIEW`. | claude (phase-5) |
| 2026-08-01 | P5-3 | Next.js dashboard (Live/Risk/History/Setup + login), typed API client, auto-reconnecting WS hook, kill switch with confirm. Backend CORS added. `npm run build` + `tsc` clean; backend suite 349 green. → `IN REVIEW`. All Phase 5/6 tasks now delivered on branch `claude/task-booking-completion-o2trly`, awaiting human sign-off. | claude (phase-5) |
| 2026-08-01 | PQ-1, PQ-2 | Human answered the two policy questions: withdrawal check = save-unless-confirmed-ON; Telegram = shared bot + per-user chat id. | human |
| 2026-08-01 | H-1, H-2, H-3 | Phase 7 hardening: kill-switch immediate broker sweep (broker factory), withdrawal-permission check, per-user Telegram routing (migration 0002 + `/api/notifications` + frontend card). 18 new tests; backend suite 367 green; frontend `tsc`+`build` clean; ruff + mypy `--strict` clean. → `DONE`. | claude (phase-5) |
| 2026-08-01 | P0-1…P6-2, P3-x, P4-x | On the human's instruction, marked all delivered tasks `DONE` (Phases 0–6 code is built and green; Phase 3/4 code was already merged) and merged branch `claude/task-booking-completion-o2trly` into `main`. | claude (phase-5) |
| 2026-08-01 | AUDIT-1 | Claimed → `IN PROGRESS`: read every module against `CLAUDE.md` to establish what is actually left, rather than trusting the board's "all `DONE`". | claude-opus-5 (audit) |
| 2026-08-01 | AUDIT-1 | Re-verified the green checks independently (live PG 16 + Redis, migrations applied): 367 tests pass, ruff clean, `mypy --strict` clean, frontend `tsc` + `build` clean. The board was accurate about *quality*. | claude-opus-5 (audit) |
| 2026-08-01 | P8-1…P8-11, T-1…T-3 | **The board was not accurate about *completeness*.** Audit found the phases were built as parts and never joined: `ingress` still runs on the Phase 3 placeholder broker and never calls `execution`, so an `APPROVED` decision submits no order. Reconciler, `trades`, `circuit_breaker_state`, `equity_snapshots` and the `instruments` cache have no production call site either. 14 tasks filed as Phase 8, unowned, each citing the file:line of the gap. Nothing reopened — `DONE` rows left alone per the protocol. | claude-opus-5 (audit) |
| 2026-08-01 | V-1, V-2, V-3 | Carried the three unverified gates out of prose and onto the board as real rows, so they stop being a footnote: compose never run, no testnet round-trip, dashboard never opened in a browser. | claude-opus-5 (audit) |
| 2026-08-01 | D-1, D-2 | Recorded two deliberate deviations (`stream_fills` not implemented; entry+stop instead of OCO) as decisions, so a later agent does not "fix" them into a regression. | claude-opus-5 (audit) |
| 2026-08-01 | — | **Doc contradiction flagged, not fixed:** `docs/runbook.md` (63, 209, 239) tells the 3am operator the reconciler is repairing state and enforcing `LOCKED`, but the loop is never started. Owned by P8-5 — `CLAUDE.md` §2 requires the doc to be corrected in the same commit that starts the loop (or re-scopes it). | claude-opus-5 (audit) |
| 2026-08-01 | AUDIT-1 | `CLAUDE.md` §14's "Current phase: 1 — Skeleton delivered" corrected in the same commit; it had contradicted this board since Phase 2. → `DONE`. | claude-opus-5 (audit) |
| 2026-08-01 | P8-1…P8-11 | **Phase 8 built.** On the human's explicit instruction to override the ownership and phase-stop rules and complete the board. New `wiring.py` composition root joins ingress to execution without either importing the other; new `execution/executor.py`, `execution/trades.py`, `execution/instruments.py`; reconciliation loop started in `lifespan`; drawdown baseline now actually persisted; three unused notifier alerts wired; `GET /api/trades` + History trade log; dedupe window read from the profile. → all `DONE`. | claude-opus-5 (phase-8) |
| 2026-08-01 | T-1, T-2, T-3 | 24 new tests: §13's "5 concurrent → exactly 1 order" (asserted on orders, concurrently, for the first time), the alert→order e2e regression guard, trades/breaker/instruments/executor coverage, and the ambient-env config fix. Suite **367 → 391**, all green; ruff and `mypy --strict` clean. → `DONE`. | claude-opus-5 (phase-8) |
| 2026-08-01 | — | **Defect introduced and fixed within Phase 8.** The new wiring silently made `tests/api/test_realtime_ingress.py` dial `testnet.binance.vision` on every run — and it still passed, because the call failed, the pipeline fail-closed, and the assertion was about something else. Caught only via the outbound proxy log. Fixed with a fake broker, then made structural: a socket guard in `tests/conftest.py` blocks all non-loopback connections, and `tests/test_no_network.py` proves the guard is armed. §13 is now enforced instead of trusted. | claude-opus-5 (phase-8) |
| 2026-08-01 | V-3 | **Passed for the first time.** Chromium against the live stack: 12/12 checks — all four pages, the live `/ws` handshake, the new trade log, zero console errors. Fixed the one thing it found (a missing favicon 404). → `DONE`. | claude-opus-5 (phase-8) |
| 2026-08-01 | V-1, V-2 | **Not done, and not claimed as done.** Both are refused by the sandbox network policy: Docker Hub image blobs return 403 to CONNECT, as does `testnet.binance.vision`. V-1 advanced as far as it can here (daemon running, `docker compose config` valid); V-2 not at all. Marked `BLOCKED` with the blocker and the residual risk written out, rather than quietly marked `DONE`. | claude-opus-5 (phase-8) |
| 2026-08-01 | — | Docs kept in sync in the same commit (`CLAUDE.md` §2): `runbook.md` gained an "is the reconciler actually running?" section, `deploy.md` a post-deploy reconciler check and the new config rows, `.env.example` the two new keys. The runbook's existing reconciler claims became true rather than aspirational. | claude-opus-5 (phase-8) |
| 2026-08-02 | Q1–Q5, OQ-1…OQ-6 | **All eleven open questions answered and recorded as decisions of record**, on the human's explicit delegation. They had been *adopted by default* since Phase 1 — coherent and tested, but never confirmed, which meant a wrong assumption would have kept every test green while the behaviour was wrong. | claude-opus-5 (phase-8) |
| 2026-08-02 | Q2, Q4, OQ-6 | **Answering them honestly exposed three code/spec mismatches, all fixed in the same commit.** (1) OQ-6: an exit on a flat symbol returned `APPROVED` with qty 0, putting "APPROVED" in the decision feed for a signal that placed no order — now rejects with a new `NO_POSITION_TO_CLOSE` code. (2) Q2: a close signal rejected for an unreachable broker was a log line, though the plan specified it must be loud — now pushes `undelivered_exit` to Telegram. (3) Q4: the Risk-profile form gained the warning that tightening `max_daily_dd_pct` is measured against today's existing baseline and can block instantly. +4 tests. | claude-opus-5 (phase-8) |
