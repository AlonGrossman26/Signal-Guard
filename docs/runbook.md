# SignalGuard — 3am Runbook

You have been paged. This document assumes you are tired, it is dark, and money
is potentially moving the wrong way. It is written to be skimmed top to bottom.

**The one principle:** SignalGuard's whole job is to say *no* reliably. When in
doubt, **lock the account** — that is always safe. A locked account places no new
orders and is continuously flattened by the reconciler. You can investigate
calmly once nothing new can happen.

> Testnet only. As shipped, no adapter can reach a live exchange (the base URL is
> a hardcoded constant and live trading is double-gated in config). If you are
> reading this because "real money moved", that should be impossible with the
> current build — verify `LIVE_TRADING_ENABLED=false` first, and treat a `true`
> as the incident.

---

## 0. Fastest actions (copy/paste)

Commands assume you are on the deploy host, in the repo root, with the stack up.

```bash
# Is the service healthy? (checks Postgres + Redis; 503 if either is down)
curl -fsS http://localhost:8000/health | jq .

# Is the process even alive? (no dependencies touched)
curl -fsS http://localhost:8000/health/live

# Tail the API logs (structured JSON; every line is one event)
docker compose logs -f --tail=200 api

# See what the containers are doing
docker compose ps
```

### Fire the kill switch NOW

Preferred — through the API (locks, sweeps, notifies), as the account's user:

```bash
# You need a logged-in session cookie. If you have credentials:
curl -fsS -c /tmp/sg.cookies -X POST http://localhost:8000/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"..."}'

# List accounts to get the id, then fire:
curl -fsS -b /tmp/sg.cookies http://localhost:8000/api/broker-accounts | jq .
curl -fsS -b /tmp/sg.cookies -X POST \
  http://localhost:8000/api/broker-accounts/<ACCOUNT_ID>/kill | jq .
```

Last resort — **the durable lock is a database column**, so if the API is down
you can still stop trading directly. This is the fail-closed backstop:

```bash
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "UPDATE broker_accounts SET trading_state='LOCKED', locked_at=now(), \
   locked_reason='manual: incident' WHERE id='<ACCOUNT_ID>';"
```

Once `trading_state='LOCKED'`, the risk engine rejects every new signal for that
account (`is_locked` fails closed), and the reconciliation loop flattens any open
order or position on it every cycle until it is flat. Locking is enough to stop
the bleeding; the sweep follows on its own.

To resume trading later — a deliberate, awake decision — unlock:

```bash
curl -fsS -b /tmp/sg.cookies -X POST \
  http://localhost:8000/api/broker-accounts/<ACCOUNT_ID>/unlock | jq .
# or, direct:  UPDATE broker_accounts SET trading_state='ACTIVE',
#              locked_at=NULL, locked_reason=NULL WHERE id='<ACCOUNT_ID>';
```

Nothing unlocks on a timer. If it is locked, a human locked it, and a human
unlocks it.

---

## 1. Triage: what is actually wrong?

Run the health check first. Its `checks` object tells you which dependency is
down.

| Symptom | Likely cause | Go to |
|---|---|---|
| `/health` returns 503, `redis: down` | Redis is unreachable | §2 |
| `/health` returns 503, `postgres: down` | Postgres is unreachable | §3 |
| `/health/live` fails / no response | API process down or crash-looping | §4 |
| Webhooks return 401/404 | auth/endpoint issue, not an outage | §5 |
| Webhooks return 429 | rate limit tripped | §5 |
| Orders not placing, decisions say `BROKER_UNAVAILABLE` | broker unreachable | §6 |
| A position with no protective stop | naked position | §7 |
| Alerts arrive but nothing happens | background eval or reconciler stuck | §8 |

---

## 2. Redis is down

**What SignalGuard does automatically (by design, plan §3 Q5):** it fails closed.
New alerts are **rejected** (the kill-switch state cannot be confirmed, and an
unconfirmable lock reads as LOCKED), but they are still **persisted** — the audit
trail stays intact. The kill switch and reconciler keep working off Postgres.

So a Redis outage is a *degraded* state, not a dangerous one: nothing new trades,
nothing is lost.

**Fix:**
```bash
docker compose ps redis
docker compose logs --tail=100 redis
docker compose restart redis
curl -fsS http://localhost:8000/health | jq '.checks.redis'
```

Redis is configured `--appendonly yes`, so circuit-breaker and kill-switch fast
state survive a restart. If the append-only file is corrupt and Redis will not
start, you can wipe the volume — Postgres is the durable record, Redis rebuilds:

```bash
docker compose stop redis && docker volume rm signal-guard_redisdata; docker compose up -d redis
```

(Confirm the volume name with `docker volume ls`.)

---

## 3. Postgres is down

This is the serious one: Postgres is the source of truth for the audit trail and
the durable lock.

**What SignalGuard does:** it cannot persist alerts or decisions, so the webhook
endpoint returns an error and TradingView/scripts will retry later. It does **not**
trade blind — no database means no snapshot means no decision.

**Fix:**
```bash
docker compose ps db
docker compose logs --tail=100 db
docker compose restart db
docker compose exec db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

Do **not** `docker volume rm` the `pgdata` volume to "fix" a startup problem —
that deletes every alert, decision, order and trade. The append-only tables are
the compliance record. Restore from backup instead (§10).

If the disk is full (`could not extend file`, `No space left on device`): free
space first (old logs, images: `docker system prune`), then restart. Postgres
will not accept writes while the volume is full.

---

## 4. API is down or crash-looping

```bash
docker compose ps api
docker compose logs --tail=200 api
```

Common causes, newest-first:

- **Config error on boot.** The app fails closed on missing/invalid secrets and
  prints exactly which one. Look for `Invalid configuration — refusing to start`.
  Fix `.env` (see `.env.example`) and `docker compose up -d api`.
- **`LIVE_TRADING_ENABLED=true` without the confirmation phrase** → refuses to
  start. In this phase it should be `false`. Set it to `false`.
- **Migrations not applied / schema drift.** See §9. The app does **not** migrate
  on boot (deliberate); a missing table means migrations were never run.
- **Bad image after a deploy.** Roll back (§11).

Liveness (`/health/live`) intentionally does **not** check dependencies, so a
Postgres blip does not trigger a restart loop. If `/health/live` is failing, the
process itself is the problem, not a dependency.

---

## 5. Webhooks are being rejected

These are **not** outages — they are the system working. Check the `alerts`
table (`parse_status`, `auth_mode`, `signature_valid`) and the decision feed.

| Response | Meaning | Action |
|---|---|---|
| 404 | unknown `endpoint_id` | wrong URL, or endpoint deleted. Not an incident — probing must not confirm which endpoints exist. |
| 401 | bad HMAC signature, stale timestamp, or wrong body secret | sender misconfigured. Check the timestamp header — a replay-protection window rejects old requests. |
| 413 | body too large | sender is sending junk; rejected before parsing, correctly. |
| 429 | rate limit (Redis token bucket) | a strategy is firing too fast. Confirm it is not a runaway loop, then it self-heals as the bucket refills. |
| 200 + decision `INVALID_PAYLOAD` | schema violation | fix the alert template (see the Setup page). |

To confirm what a specific alert did, use the decisions feed filtered by reason
code, or query `decisions` directly (every alert has exactly one live decision).

---

## 6. Broker unreachable (`BROKER_UNAVAILABLE`)

The engine cannot build a snapshot without account state, so it **rejects** rather
than queueing (plan §3 Q2): a queued signal would fire later at a price the
strategy never saw. This is safe — no trade is better than a stale trade.

Check testnet reachability and the API logs for `Broker unreachable during
evaluation`. When the exchange recovers, new alerts evaluate normally; the missed
ones are visible in the decision feed as `BROKER_UNAVAILABLE` and were, correctly,
never sent.

If you also see open positions during the outage, the reconciler will repair
local state from broker truth once connectivity returns — **the broker is the
source of truth**, local tables are a cache.

---

## 7. Naked position (a position with no protective stop)

This is the scenario the whole execution layer is built to prevent, and the
loudest one. It happens if an entry filled but its stop failed to place.

**Immediate action:** fire the kill switch on that account (§0). That flattens
everything, which resolves a naked position by closing it.

**Then investigate:** the execution layer submits entry + protective stop as a
unit; if the stop fails it closes the position and alerts. A Telegram
`⚠️ NAKED POSITION` alert (if notifications are configured) means the automatic
close already ran — verify it in `positions` and `orders`. If it is still open,
the kill switch is your hammer.

---

## 8. Alerts arrive but nothing happens

The webhook responds 200 fast and evaluates in the background, so a 200 does not
mean a decision was reached yet — check the `decisions` table for the alert.

- **No decision row appears at all:** the background task failed. Grep the logs
  for `Background risk evaluation failed` with the `alert_id`. The alert row still
  exists (it is written first), so nothing is lost — re-driving is possible.
- **Decisions appear but positions/orders do not update:** the reconciliation loop
  may be stuck. Look for `Reconciliation cycle failed`. The loop logs and
  continues by design, so one bad cycle is not fatal; a *persistent* failure
  means the broker is unreachable (§6) or credentials are wrong.

### Is the reconciler actually running?

Ask this first whenever local state looks stale — orders stuck in `SUBMITTED`,
positions not moving, an empty equity curve, or a `LOCKED` account that is still
holding something. The loop is what repairs all of those, and if it is not
running none of them will ever correct themselves.

```powershell
docker compose logs api | Select-String "Reconciliation loop"
```

Three possible answers:

| Log line | Meaning |
|---|---|
| `Reconciliation loop started` | Normal. It runs every `RECONCILER_INTERVAL_SEC` (default 15). |
| `Reconciliation loop is DISABLED` | Someone set `RECONCILER_ENABLED=false`. **Nothing is being repaired** — no order repair, no position sync, no equity snapshots, no closed trades, and a `LOCKED` account is not being continuously flattened. Turn it back on unless you know why it is off. |
| Neither line | The process did not get through startup. Check `/health` and the lines above it. |

`Could not build a broker adapter; skipping this account` is not a loop failure —
it means one account's credentials will not decrypt or names an unsupported
broker. The loop deliberately skips it and reconciles everything else, so one bad
account cannot stop the rest. Fix that account's credentials; the others are fine.

---

## 9. Migrations

The API does **not** run migrations on startup — that is a deliberate, reviewed
step, so a container restart never silently changes the schema.

```bash
# Apply all migrations
docker compose exec api uv run alembic upgrade head

# Where are we?
docker compose exec api uv run alembic current

# History
docker compose exec api uv run alembic history
```

If the API is crash-looping because a table is missing, this is almost always the
cause: the schema was never migrated. Run `upgrade head`.

---

## 10. Backups (Postgres is the record)

The append-only `alerts` and `decisions` tables are the audit trail; `orders`,
`trades`, `positions` and `equity_snapshots` are the operational record. Back up
Postgres, not Redis.

```bash
# Dump
docker compose exec db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup-$(date +%F).sql

# Restore (into an empty database)
cat backup-YYYY-MM-DD.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

---

## 11. Rolling back a bad deploy

```bash
git log --oneline -n 10            # find the last-known-good commit
git checkout <good-sha>
docker compose up -d --build api   # rebuild only the API

# If the bad deploy included a migration, roll it back BEFORE the code:
docker compose exec api uv run alembic downgrade -1
```

Roll the schema back only if the new migration is the problem and is safely
reversible. When unsure, lock every account (§0) and get a second person — a
half-rolled-back schema is worse than a paused system.

---

## Appendix: reason codes you will see

Rule family (produced by the pure engine, in order): `TRADING_LOCKED`,
`INVALID_PAYLOAD`, `STALE_ALERT`, `DUPLICATE_ALERT`, `SYMBOL_NOT_ALLOWED`,
`NO_STOP_LOSS`, `CIRCUIT_BREAKER_OPEN`, `DAILY_DRAWDOWN_HIT`, `SIZE_BELOW_MINIMUM`,
`EXPOSURE_LIMIT`.

Pipeline family (the engine never ran — something upstream failed, fail closed):
`BROKER_UNAVAILABLE`, `STATE_UNAVAILABLE`, `ACCOUNT_NOT_FOUND`,
`INSTRUMENT_UNAVAILABLE`, `INTERNAL_ERROR`.

Every one of these is a *rejection you wanted*. A high rejection count is the
system doing its job, not a bug.
