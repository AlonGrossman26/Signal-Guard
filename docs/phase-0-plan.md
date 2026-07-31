# Phase 0 — Plan

**Status:** awaiting sign-off. No application code has been written.
**Covers board tasks:** P0-1 (restate + assumptions + open questions), P0-2 (file tree), P0-3 (table columns).

Read this top to bottom and reply with decisions on §4 (the open questions), §5 (the tree) and §6 (the
columns). Phase 1 does not start until those three are signed off.

---

## 1. What we are building, in my own words

SignalGuard is a **gatekeeper that sits in the middle of a wire**. On one side, something automated
shouts "buy BTC" — a TradingView alert, a Python script, a bot. On the other side is a broker that
will do exactly what it is told, instantly, with real money, without ever asking "are you sure?".

Today that wire is direct. SignalGuard cuts it and puts a checkpoint in the gap.

Every signal that arrives gets:

1. **Written down first** — before any judgement, before any order. The audit log is not a
   by-product of trading; it is the product. If we crash one line later, the record still exists.
2. **Checked against the user's own rules** — ten of them, in a fixed order, stopping at the first
   failure (§7 of `CLAUDE.md`).
3. **Sized, not just approved** — the signal says *what*, the risk engine decides *how much*. This
   is the part users cannot do reliably by hand and the part that actually protects them.
4. **Forwarded, or refused and logged.** Never silently dropped, never "probably fine".

The thing to internalise: **this product's value is entirely in the "no".** Anyone can forward a
webhook to an exchange in forty lines of Python. The hard, valuable, dangerous part is refusing
correctly — and, critically, refusing correctly *when something is broken*. A missed trade costs a
user one opportunity. A trade that is 10× too large, or has no stop attached, can cost them the
account. Those two outcomes are not symmetric, so the code must not treat them symmetrically.

That asymmetry is why "fail closed" is constraint #1 and why it will sometimes feel excessive. When
the database is slow, when Redis restarts, when the exchange returns a shape we have never seen —
the answer is *reject and tell the user loudly*. Refusing is a safe, recoverable, boring failure.
Guessing is not.

### The one design decision everything else hangs off

`risk/` performs **no I/O**. It does not read a database, call the network, or even look at the
clock. It is handed a complete snapshot — the alert, the account state, the user's config, and the
current time as a parameter — and returns a `Decision`.

Why this matters so much, in plain terms: the risk engine is the part that must be *provably*
correct, and you can only prove something correct if you can run it thousands of times with made-up
inputs in milliseconds. The moment the engine calls a database, testing "what happens at exactly
3 consecutive losses across a daylight-savings boundary after a restart" goes from a 2-millisecond
unit test to a fragile integration test nobody runs. Every I/O call added to `risk/` buys a little
convenience and costs a lot of confidence. So: none, ever, enforced by a test (board task P2-4).

The corollary that is easy to miss: **gathering the snapshot is somebody else's job.** The
orchestration layer fetches account state, reads the clock once, loads the profile, and hands the
whole bundle over. If it cannot assemble a complete snapshot, the engine is never called and the
answer is a rejection — see Q2 and Q5 below.

---

## 2. Assumptions I am making

Correct me on any of these; several change the design materially.

### About the market and the broker

- **A1 — Binance spot testnet, no margin, no leverage, no shorting.** Everything is cash-settled
  buying and selling of an asset you hold.
- **A2 — "Positions" on spot are not real positions.** Binance spot has no position objects; it has
  balances. Holding 0.1 BTC *is* the long position. So `positions` is derived from balances, and
  `close_all_positions()` means "market-sell the base assets back to quote". This affects the
  `positions` table and the kill switch.
- **A3 — USDT is the quote currency**, and equity is denominated in USDT. Symbols look like
  `BTCUSDT`.
- **A4 — Instrument filters come from the exchange at startup and are cached** (`lot_step`,
  `min_qty`, `min_notional`, `tick_size`), never hardcoded, per §7. See OQ-5 for what happens when
  that cache is stale.
- **A5 — Fees are read from the account, not assumed.** The configurable buffer is for slippage and
  headroom, not a substitute for the real fee rate.

### About the signals

- **A6 — Money arrives as text, and stays exact.** Price fields in the JSON body are parsed
  straight to `Decimal` without ever passing through a float. Concretely, the JSON is parsed with
  `parse_float=Decimal` / `parse_int=Decimal`, so `"62000.00"` and `62000.00` both land as an exact
  `Decimal` — because by the time a standard JSON parser has produced a Python `float`, the
  precision is already gone and no amount of later `Decimal()` wrapping brings it back. This lets us
  accept unquoted numbers (which TradingView templates emit constantly) without breaking
  constraint #3.
- **A7 — Timestamps must carry a timezone.** A naive timestamp is ambiguous, and ambiguity means
  reject (`INVALID_PAYLOAD`). No "assume UTC".
- **A8 — Market orders have no price in the payload**, so sizing needs a reference price fetched
  from the broker at evaluation time and placed into the snapshot. The slippage buffer exists partly
  to cover the gap between that reference and the real fill.
- **A9 — `endpoint_id` is a credential, not an identifier.** It appears in a URL, so it is treated
  like a password: stored as a hash, never logged, rotatable.

### About the system

- **A10 — Correctness must not assume a single process.** v1 runs one API container, but the
  idempotency test in §13 requires 5 *concurrent* submissions to produce exactly 1 order, so dedupe
  and locking are designed to be correct across processes from day one.
- **A11 — The broker is the source of truth; our tables are a cache** (§10). Reconciliation repairs
  local state, never the reverse.
- **A12 — The system never initiates a trade on its own.** It acts only on an inbound signal or an
  explicit kill switch. Nothing in the product spontaneously opens, resizes, or closes a position.
  This is load-bearing for Q4.
- **A13 — One risk profile per user** (per §6's `risk_profiles.user_id`), not one per broker
  account.

---

## 3. Your five questions

These are **recommendations with reasoning, not decisions.** Reply with a yes, a no, or a different
answer, and I will record it on the board.

### Q1 — What happens to an in-flight order when the kill switch fires mid-submission?

**Recommendation: let it complete, then reconcile it away. Never abandon it.**

The trap here is trying to "cancel" a request that is already on the network. You cannot un-send an
HTTP request, and if you abandon it you lose the response — which means you lose the broker order ID
— which means an order may now exist at the exchange that we have no record of. That is the single
worst state this system can reach: a naked position, on a locked account, invisible to us.

So the design is:

1. **`LOCKED` is written first**, to Postgres and Redis, before any sweeping begins. The execution
   layer re-checks the lock immediately before every submit, so new orders stop at once.
2. **In-flight submissions are allowed to finish** and their results recorded. We always know what
   we sent, because the `orders` row is written with status `PENDING_SUBMIT` and our own
   `client_order_id` *before* the HTTP call. A lost response is then recoverable: we ask the broker
   about that client ID.
3. **The sweep runs more than once.** Cancel-all then close-all, then repeat after a short settle
   window, because an order that landed during the first pass would otherwise be missed.
4. **`LOCKED` is a state, not an event.** This is the important part. While an account is locked,
   the reconciliation loop treats *any* open order or non-flat position as a violation and flattens
   it. So the race isn't solved by winning it — it's solved by continuously enforcing the invariant
   until reality matches it.

That last point is why I would rather spend the effort on a good reconciler than on elaborate
mid-flight cancellation logic. One is achievable; the other is a distributed-systems fantasy.

### Q2 — Broker unreachable when an alert arrives: queue or reject?

**Recommendation: reject.** You asked me to argue one side, so:

- **We cannot evaluate the rules without the broker.** Equity feeds rule 8 (drawdown) and rule 9
  (sizing); open positions feed rule 10 (exposure). No account state means no snapshot, and a
  guessed or stale snapshot is exactly the "assume and proceed" that constraint #1 forbids.
- **Trading signals expire.** Rule 3 already declares an alert older than 30 seconds stale. A queue
  that holds a signal for 90 seconds and then releases it is, by the project's own definition,
  submitting a stale trade — at a price that no longer exists, for a setup that may have already
  invalidated.
- **Queues fail in bursts.** The broker returns after ten minutes and eight queued signals fire at
  once, straight through the exposure caps in a way no single evaluation would have allowed.
- **The asymmetry decides it.** A missed trade is an annoyance; a trade filled at a price the
  strategy never saw is how accounts blow up.

The uncomfortable case, which I want to name rather than hide: **`action: "close"` is
risk-*reducing*.** Rejecting it leaves the user in a position they asked to exit. I still recommend
rejecting — we cannot place the closing order either, so accepting it would be a lie — but it should
trigger an immediate, loud notification ("your close signal could not be delivered, the broker is
unreachable, here is the position you still hold") rather than a quiet log line. If you would rather
close signals retry for a bounded window, say so and I will spec it explicitly; I will not improvise
it.

This rejection happens *outside* the pure engine (there is no snapshot to evaluate), which raises
OQ-1 below.

### Q3 — How is `liquid_equity` defined when positions are open?

**Recommendation: neither, exactly — the two jobs need two different numbers, and I would delete the
name `liquid_equity` because it invites exactly this confusion.**

- **The risk base for sizing is mark-to-market total equity** (cash + current value of holdings).
  Reason: `risk_per_trade_pct` should mean "1% of my account", stable and predictable. If it meant
  1% of *free cash*, your third trade would be far smaller than your first for no reason the user
  understands, and the dashboard preview in §11 ("given $10,000 equity…") would not match reality.
- **The hard ceiling is free cash.** The spec already contains this: `qty × entry_price ≤
  min(max_notional_per_trade, available_balance)`. On spot you cannot buy with money that is already
  spent, so affordability is a separate, later, absolute check.
- **Daily drawdown uses mark-to-market**, unambiguously — §7 says the loss includes unrealized PnL,
  which is definitionally mark-to-market.

So: **mark-to-market sets the intent, free cash sets the limit.** Both lines are already in §7; they
were just sharing one confusing name. My concrete proposal is that the snapshot carries three
explicit fields — `total_equity`, `free_balance`, `position_value` — and the term `liquid_equity`
never appears in the code. Naming each number for exactly what it is, is the cheapest available
defence against this class of bug.

Pleasant side effect: a large unrealized loss shrinks both the sizing base and the drawdown
numerator, so risk automatically contracts while losing. That is the behaviour you want.

### Q4 — Does editing a risk profile while a position is open apply retroactively?

**Recommendation: never retroactively to open positions; immediately to the next decision.** Split
by rule type:

- **Sizing (9) and the stop rules (6) cannot be re-applied to an open position.** The position is
  already sized and the stop is already resting at the exchange. Resizing it would mean SignalGuard
  submitting an order that no signal asked for — violating A12, and genuinely alarming behaviour to
  discover as a user.
- **The gates (1, 3, 4, 5, 7, 8, 10) apply at evaluation time, using the profile as it stands at
  that instant.** Tighten your exposure cap and the *next* signal feels it immediately. Nothing
  currently open is touched.
- **One case deserves a UI warning:** tightening `max_daily_dd_pct` mid-session re-evaluates against
  the *existing* baseline, so it can trip the breaker instantly. I think that is correct — a user
  tightening their risk should have it bite now — but it is surprising enough that the form must say
  so out loud.
- **Audit records are never rewritten.** `decisions.rule_snapshot` captures the config that produced
  each decision, so "which rules was I under at 14:32?" stays answerable forever. I propose adding a
  `version` integer to `risk_profiles`, bumped on every edit and copied into each snapshot, so that
  question is answerable with a cheap lookup instead of a JSONB diff.

### Q5 — What is the failure mode if Redis is down but Postgres is up?

**Recommendation: reject every new alert, keep recording, and keep the kill switch working.**

Redis holds dedupe keys, rate limits, circuit-breaker state, kill-switch state, and WebSocket
pub/sub. Taking them one at a time:

- **Kill-switch state is decisive.** If we cannot read it, we cannot prove the account is *not*
  locked. Unknown must be treated as `LOCKED`. This alone forces rejection.
- **Circuit-breaker state, likewise** — unknown is treated as `OPEN`.
- **Dedupe is impossible to guarantee.** Without Redis we cannot cheaply answer "seen in the last
  60 seconds", and a duplicate that slips through is a double order — constraint #4 broken, real
  money lost.

So while Redis is unavailable: alerts are still **received and persisted** (Postgres is up, so the
audit trail is intact — we never silently drop), each gets a decision row with a rejection code, and
the user is notified loudly. Ingest degrades to "record and refuse", which is the honest failure.

Three things must keep working, and they are the interesting half of the answer:

1. **The kill switch itself**, because §10 requires it to work even when other layers are down. It
   writes `LOCKED` to **Postgres** as the durable record, updates Redis opportunistically, and
   drives the broker sweep directly. It must not have a hard Redis dependency.
2. **The lock read path**, which becomes: try Redis → on failure fall back to Postgres → if both are
   unreachable, reject. Postgres is the record; Redis is the fast path. This is why
   `broker_accounts.trading_state` is a real column rather than Redis-only state.
3. **Reconciliation**, which needs only Postgres and the broker, and is what flattens positions on
   locked accounts.

Worth stating the companion case you did not ask about: **if Postgres is down, we also reject** —
constraint #5 requires the alert and decision to be persisted before any order is placed. No audit
trail, no trading.

---

## 4. New open questions I need answered

Working through the spec surfaced six genuine ambiguities. Per §2 of `CLAUDE.md` I am flagging
rather than guessing — a guessed risk rule is worse than no rule, because it looks like it works.

**OQ-1 — `decisions.reason_code` needs a second family of codes.**
The ten codes in §7 are *rule* outcomes produced by the pure engine. But some rejections happen
before the engine can run at all (broker unreachable per Q2, Redis down per Q5, account not
configured, instrument data unavailable). Those still need a decision row with a machine-readable
reason. I propose adding a **pipeline** family, kept visibly distinct from the rule family:
`BROKER_UNAVAILABLE`, `STATE_UNAVAILABLE`, `ACCOUNT_NOT_FOUND`, `INSTRUMENT_UNAVAILABLE`,
`INTERNAL_ERROR`. All fail closed. **Approve these five codes, or tell me to fold them into
`INVALID_PAYLOAD`?** (I recommend against folding — it would make the dashboard's rejection
breakdown lie about *why* trades were refused.)

**OQ-2 — Rule 1 runs before rule 2, but the lock is per account and the account name is inside the
payload.**
You cannot know which broker account is locked until you have parsed the body — yet rule 1 (kill
switch) is specified to run before rule 2 (payload validity). Proposed resolution: the lock is
checked **twice**. A cheap **user-level** trading lock is checked pre-parse (we know the user from
`endpoint_id`), then the **account-level** lock is checked once the payload names the account. Both
return `TRADING_LOCKED`, preserving the spec's priority order. **Confirm this reading?**

**OQ-3 — The fee/slippage buffer as literally specified does not achieve its stated goal.**
§7 says subtract the buffer from `risk_amount` first, so that a worst-case stop-out still loses no
more than the stated risk percentage. But fees and slippage scale with **notional**, not with
`risk_amount`, so subtracting a percentage of the latter under-corrects. With $10,000 equity, 1%
risk, entry $62,000, stop $61,000, buffer 20 bps:

| Approach | qty | Worst-case loss at the stop | Budget |
|---|---|---|---|
| Literal (`risk_amount × (1 − buffer)`) | 0.0998 | **$112.18** | $100 — over by 12% |
| Closed form (below) | 0.0889 | **$100.00** | $100 — exact |

The fix needs no iteration, just algebra. Solving
`qty × stop_distance + qty × entry_price × buffer_rate = risk_amount` gives:

```
qty = risk_amount / (stop_distance + entry_price × buffer_rate)
```

This matters beyond neatness: the Hypothesis property test in §13 asserts that realized loss at the
stop never exceeds `risk_per_trade_pct` of equity. **The literal formula fails that test**; this one
passes it by construction, and rounding down only makes it safer. **May I implement the closed
form?**

**OQ-4 — Storing the alert payload "verbatim" conflicts with "secrets never touch logs".**
§6 wants the raw payload in JSONB; the body-secret auth mode (§8) puts a live shared secret inside
that payload. Storing it verbatim persists a credential in plaintext in the database, which is the
spirit — if not the letter — of constraint #6. Proposal: store the payload with the `secret` field
replaced by `"[REDACTED]"`, plus a `raw_body_sha256` of the true bytes so integrity is still
auditable and HMAC disputes are still resolvable. **Approve?**

**OQ-5 — How stale may cached instrument data get?**
§7 says fetch `lot_step` / `min_qty` / `min_notional` at startup and cache. If the exchange is
unreachable at boot, do we start with yesterday's cached filters, or refuse to trade? Fail-closed
says refuse, but that turns a brief exchange blip into a full outage for the user. Proposal: serve
from cache up to a **max age** (suggest 24h), reject with `INSTRUMENT_UNAVAILABLE` beyond it, and
re-fetch in the background continuously. **Confirm the policy and the max age?**

**OQ-6 — `action: "sell"` is ambiguous on spot.**
There is no shorting on Binance spot (A1), yet §7's stop-loss rule specifies short-side behaviour
(`stop_price > entry_price`). Proposal: for v1, `buy` opens/increases a long, `sell`
reduces/closes a long, and `close` flattens the symbol entirely; a `sell` with no existing position
is rejected rather than treated as a short. The short-side stop logic still gets implemented and
tested in the pure engine — it is pure arithmetic, costs nothing, and is needed the moment a futures
adapter appears — but it is unreachable through the spot adapter. **Confirm?**

*Minor, non-blocking:* the §11 UI example ("$10,000 equity, $62,000 entry, $61,000 stop → 0.161
BTC") does not match the §7 formula, which yields 0.1 BTC at 1% risk before buffers, or 0.0889 with
the 20 bps buffer. I assume the number was illustrative; I will make the dashboard preview compute
it live rather than hardcode it.

---

## 5. Proposed file tree (P0-2)

Expanding `CLAUDE.md` §5. The repo root already holds `CLAUDE.md` and `PROJECT_MANAGEMENT.md`, so
the tree *is* the repo root — no extra `signalguard/` wrapper directory.

```
Signal-Guard/
  CLAUDE.md
  PROJECT_MANAGEMENT.md
  docker-compose.yml
  .env.example                    # key names, empty values — never real keys
  .gitattributes                  # forces LF on *.sh, *.py, Dockerfile*, *.yml
  .dockerignore
  docs/
    phase-0-plan.md               # this file
    runbook.md                    # Phase 6
  backend/
    Dockerfile
    pyproject.toml                # uv-managed
    alembic.ini
    alembic/
      env.py
      versions/
    src/signalguard/
      main.py                     # FastAPI app factory, lifespan, router wiring
      config.py                   # pydantic-settings; fails loudly on missing required keys
      logging.py                  # structured JSON + secret-redaction filter
      errors.py                   # internal error enum; no broker exception ever escapes past this
      db/
        base.py  session.py       # async engine + session factory
        models/                   # one module per table (§6)
      ingress/
        routes.py                 # POST /webhook/{endpoint_id} and /test
        auth.py                   # HMAC over raw body, timestamp replay guard, body-secret fallback
        schema.py                 # strict pydantic model; Decimal-safe JSON loading (A6)
        dedupe.py                 # dedupe_key derivation + Redis window + Postgres backstop
        ratelimit.py              # Redis token bucket
      risk/                       # ★ PURE — zero I/O, no clock reads, time passed in
        types.py                  # Snapshot, Decision, ReasonCode, RiskConfig
        rules.py                  # the 10 rules, in order, short-circuiting
        sizing.py                 # the position-sizing transform (rule 9)
        engine.py                 # evaluate(snapshot) -> Decision
      execution/
        base.py                   # abstract broker adapter (§9)
        binance_testnet.py        # the one implementation
        orders.py                 # submit + protective stop; never leave a naked position
        reconciler.py             # broker truth → local state; enforces LOCKED continuously
        killswitch.py             # idempotent; must work with Redis down
        state.py                  # circuit-breaker + kill-switch state, Redis + Postgres
      api/
        auth.py  profiles.py  accounts.py  decisions.py  positions.py  equity.py
        ws.py                     # /ws fan-out from Redis pub/sub
      notify/
        telegram.py               # Phase 6
    tests/
      risk/                       # pure unit tests — the bulk of the suite
      ingress/
      execution/
      fakes/                      # fake broker: timeout, partial fill, reject, double-fill
      test_risk_purity.py         # asserts risk/ imports no I/O libs (board task P2-4)
  frontend/                       # Phase 5 — Next.js App Router, TS, Tailwind
```

Two notes on the tree:

- **`risk/` has no `__init__` re-exports of anything I/O-shaped**, and `test_risk_purity.py` walks
  its imports to prove it. That test is cheap and guards the project's most important property.
- **`.gitattributes` is the very first file of Phase 1.** A CRLF line ending on an entrypoint script
  inside a Linux container produces a `not found` error that reads like the file is missing. It is a
  genuinely miserable afternoon, and one file prevents it permanently. I did not add it in Phase 0
  because Phase 0 ships no code — but it should be commit #1 of Phase 1.

---

## 6. Proposed data model (P0-3)

Conventions across every table:

- **Primary keys** are `UUID` (v4), so IDs are never guessable or enumerable in a URL.
- **All money and quantity columns** are `NUMERIC(36, 18)`. Never `float`, never `double precision`.
  36 digits with 18 decimals comfortably covers crypto precision at both extremes.
- **All percentages** are `NUMERIC(9, 6)` stored as fractions — `0.01` means 1%, not `1`. One
  representation, decided once, so no code ever has to guess which convention a column uses.
- **All timestamps** are `TIMESTAMPTZ`, stored in UTC. The only place a local timezone exists is the
  user's `daily_reset_time` pairing (§7 rule 8) and the display layer.
- **`alerts` and `decisions` are append-only, enforced by a database trigger** that raises on
  `UPDATE` and `DELETE` — not merely by convention. A convention is a comment; a trigger is a rule.

### Tables from `CLAUDE.md` §6

**`users`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `email` | TEXT UNIQUE NOT NULL | stored lowercase-normalised |
| `password_hash` | TEXT NOT NULL | Argon2id (§12) |
| `is_active` | BOOLEAN NOT NULL DEFAULT true | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

**`broker_accounts`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → users NOT NULL | |
| `broker` | TEXT NOT NULL | `'binance_spot_testnet'` for v1 |
| `label` | TEXT NOT NULL | what the webhook's `account` field matches |
| `encrypted_credentials` | BYTEA NOT NULL | AES-GCM ciphertext |
| `credentials_nonce` | BYTEA NOT NULL | GCM nonce, never reused |
| `key_version` | INT NOT NULL DEFAULT 1 | lets the master key rotate without downtime |
| `is_testnet` | BOOLEAN NOT NULL DEFAULT true | |
| `is_active` | BOOLEAN NOT NULL DEFAULT true | |
| `trading_state` | TEXT NOT NULL DEFAULT `'ACTIVE'` | `ACTIVE` \| `LOCKED` — the durable kill-switch record (Q5) |
| `locked_at`, `locked_reason` | TIMESTAMPTZ NULL, TEXT NULL | |
| `created_at`, `updated_at` | TIMESTAMPTZ NOT NULL | |

Constraints: `UNIQUE (user_id, label)` — the payload selects the account by label, so it must be
unambiguous. **`CHECK (is_testnet = true)`** — constraint #2 enforced in the schema itself, so the
database physically refuses to hold a live-trading account until the migration that relaxes it.

**`risk_profiles`** — one row per user (A13), one column per rule parameter.

| Column | Type | Default | Rule |
|---|---|---|---|
| `id` / `user_id` | UUID PK / UUID FK UNIQUE | | |
| `version` | INT NOT NULL | 1 | bumped on every edit; copied into each decision snapshot (Q4) |
| `max_alert_age_sec` | INT NOT NULL | 30 | 3 |
| `future_tolerance_sec` | INT NOT NULL | 5 | 3 |
| `dedupe_window_sec` | INT NOT NULL | 60 | 4 |
| `allowed_symbols` | TEXT[] NOT NULL | `'{}'` | 5 — **empty means nothing is allowed**, not everything |
| `min_stop_distance_pct` | NUMERIC(9,6) NOT NULL | 0.001 | 6 |
| `consecutive_loss_threshold` | INT NOT NULL | 3 | 7 |
| `circuit_breaker_cooldown_minutes` | INT NOT NULL | 60 | 7 |
| `circuit_breaker_manual_reset` | BOOLEAN NOT NULL | false | 7 |
| `max_daily_dd_pct` | NUMERIC(9,6) NOT NULL | 0.05 | 8 |
| `risk_per_trade_pct` | NUMERIC(9,6) NOT NULL | 0.01 | 9 |
| `fee_slippage_buffer_bps` | INT NOT NULL | 20 | 9 |
| `max_notional_per_trade` | NUMERIC(36,18) NOT NULL | | 9, 10 |
| `max_open_positions` | INT NOT NULL | 3 | 10 |
| `max_total_notional` | NUMERIC(36,18) NOT NULL | | 10 |
| `allow_pyramiding` | BOOLEAN NOT NULL | false | 10 |
| `daily_reset_time` | TIME NOT NULL | `'00:00'` | 8 |
| `timezone` | TEXT NOT NULL | `'UTC'` | 8 — IANA name, validated on write |
| `created_at`, `updated_at` | TIMESTAMPTZ NOT NULL | | |

`CHECK` constraints on every percentage (`> 0 AND < 1`) and every threshold (`> 0`). The
`allowed_symbols` default of empty is deliberate: a brand-new profile trades nothing until the user
opts a symbol in.

**`alerts`** *(append-only)*

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK NOT NULL | resolved from the endpoint |
| `webhook_endpoint_id` | UUID FK NOT NULL | |
| `raw_payload` | JSONB NOT NULL | `secret` field redacted — see OQ-4 |
| `raw_body_sha256` | BYTEA NOT NULL | integrity proof of the true bytes |
| `content_length` | INT NOT NULL | |
| `source_ip` | INET | |
| `received_at` | TIMESTAMPTZ NOT NULL | |
| `dedupe_key` | TEXT NOT NULL | §8 derivation |
| `parse_status` | TEXT NOT NULL | `OK` \| `INVALID_JSON` \| `SCHEMA_INVALID` \| `OVERSIZED` \| `AUTH_FAILED` |
| `auth_mode` | TEXT NOT NULL | `HMAC` \| `BODY_SECRET` — so the weaker mode is visible in the audit |
| `signature_valid` | BOOLEAN NOT NULL | |

Indexes: `(dedupe_key, received_at)` for the Postgres dedupe backstop, `(user_id, received_at DESC)`
for the dashboard feed.

**`decisions`** *(append-only)*

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `alert_id` | UUID FK NOT NULL | |
| `broker_account_id` | UUID FK NULL | null when we never got as far as resolving it |
| `verdict` | TEXT NOT NULL | `APPROVED` \| `REJECTED` |
| `reason_code` | TEXT NOT NULL | rule family (§7) or pipeline family (OQ-1) |
| `reason_detail` | TEXT NULL | human-readable, for the dashboard |
| `rule_snapshot` | JSONB NOT NULL | full config as it was at decision time |
| `risk_profile_version` | INT NOT NULL | cheap join back to "which rules applied" |
| `computed_qty` | NUMERIC(36,18) NULL | |
| `entry_reference_price` | NUMERIC(36,18) NULL | the price sizing actually used (A8) |
| `stop_price` | NUMERIC(36,18) NULL | |
| `evaluated_at` | TIMESTAMPTZ NOT NULL | |
| `latency_ms` | INT NOT NULL | |
| `is_test` | BOOLEAN NOT NULL DEFAULT false | `/test` decisions never reach the broker |

**`UNIQUE (alert_id) WHERE is_test = false`** — a partial unique index making "one alert, one live
decision" a database guarantee rather than an application promise. This is the structural backstop
behind the idempotency test.

**`orders`**

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `decision_id` | UUID FK NOT NULL | |
| `broker_account_id` | UUID FK NOT NULL | |
| `client_order_id` | TEXT NOT NULL | **ours**, written before the HTTP call (Q1) |
| `broker_order_id` | TEXT NULL | null until the exchange answers |
| `symbol`, `side`, `type` | TEXT NOT NULL | |
| `role` | TEXT NOT NULL | `ENTRY` \| `STOP` \| `TAKE_PROFIT` \| `EXIT` \| `KILL_SWITCH` |
| `qty` | NUMERIC(36,18) NOT NULL | |
| `price`, `stop_price` | NUMERIC(36,18) NULL | |
| `status` | TEXT NOT NULL | `PENDING_SUBMIT` \| `SUBMITTED` \| `PARTIALLY_FILLED` \| `FILLED` \| `CANCELLED` \| `REJECTED` \| `UNKNOWN` |
| `filled_qty` | NUMERIC(36,18) NOT NULL DEFAULT 0 | |
| `avg_fill_price` | NUMERIC(36,18) NULL | |
| `fees`, `fee_asset` | NUMERIC(36,18) NOT NULL DEFAULT 0, TEXT NULL | |
| `submitted_at`, `filled_at`, `updated_at` | TIMESTAMPTZ | |
| `last_error` | TEXT NULL | mapped internal error code, never a raw broker exception (§9) |

`UNIQUE (broker_account_id, client_order_id)`, index on `broker_order_id`. `role` is not in §6 — I
need it to tell an entry from its protective stop, which §10's "never leave a naked position" logic
depends on.

**`positions`** — a cache of broker truth (A11), never authoritative.

`id` PK · `broker_account_id` FK · `symbol` · `qty` NUMERIC · `avg_entry` NUMERIC · `mark_price`
NUMERIC · `unrealized_pnl` NUMERIC · `updated_at` TIMESTAMPTZ. `UNIQUE (broker_account_id, symbol)`.

**`equity_snapshots`**

`id` PK · `broker_account_id` FK · `equity` NUMERIC NOT NULL (mark-to-market, per Q3) ·
`free_balance` NUMERIC · `position_value` NUMERIC · `taken_at` TIMESTAMPTZ NOT NULL ·
`is_session_baseline` BOOLEAN NOT NULL DEFAULT false · `session_date` DATE NULL (the *user-local*
trading day a baseline belongs to).

**`UNIQUE (broker_account_id, session_date) WHERE is_session_baseline`** — exactly one baseline per
trading day, enforced by the database. This is what makes the DST and restart tests in §13 pass
structurally: a restart cannot mint a second baseline, and a DST day is still one `session_date`.

**`trades`** — closed round-trips; the circuit breaker's input.

`id` PK · `broker_account_id` FK · `symbol` · `side` · `qty` NUMERIC · `entry_price` NUMERIC ·
`exit_price` NUMERIC · `realized_pnl` NUMERIC NOT NULL · `fees` NUMERIC · `opened_at` ·
`closed_at` TIMESTAMPTZ NOT NULL · `entry_order_id` FK NULL · `exit_order_id` FK NULL.
Index `(broker_account_id, closed_at DESC)` — exactly the "consecutive losses, most recent first"
query.

### Four tables beyond §6 — each needs explicit sign-off

I am not adding these speculatively; each is required by a rule that §6's list does not cover.

1. **`sessions`** — §3 asks for session auth, and revocable sessions need storage.
   `id` PK · `user_id` FK · `token_hash` TEXT UNIQUE (hash only, never the raw token) ·
   `created_at` · `expires_at` · `revoked_at` NULL · `user_agent` · `ip`.

2. **`webhook_endpoints`** — `endpoint_id` is a credential (A9), so it cannot live as a plain column
   on `users`. `id` PK · `user_id` FK · `endpoint_id_hash` TEXT UNIQUE · `hmac_secret_encrypted`
   BYTEA · `body_secret_encrypted` BYTEA · `is_active` · `created_at` · `last_used_at` ·
   `rotated_at`. Note the hash must be **deterministic** (SHA-256 with a server-side pepper), not
   Argon2 — we look the endpoint up by value on every request, so it must be indexable and fast.
   Argon2 is right for passwords precisely because it is slow, which makes it wrong here.

3. **`circuit_breaker_state`** — §7 says this state lives in Redis *and* Postgres and must survive a
   restart, so it needs a durable home. `broker_account_id` PK FK · `state` (`CLOSED`|`OPEN`) ·
   `consecutive_losses` INT · `opened_at` · `cooldown_until` · `last_counted_trade_id` FK ·
   `updated_at`.

4. **`instruments`** — §7 says cache the exchange filters, never hardcode them. `(broker, symbol)`
   PK · `base_asset` · `quote_asset` · `tick_size` · `lot_step` · `min_qty` · `min_notional` ·
   `status` · `fetched_at`. `fetched_at` is what OQ-5's staleness policy reads.

The kill-switch `LOCKED` state deliberately does *not* get its own table — it lives on
`broker_accounts.trading_state`, so the lock is durable, readable with the account, and survives
Redis being down (Q5).

---

## 7. What is not handled yet

Being explicit, per §2 of `CLAUDE.md`:

- **No code exists.** No Docker Compose, no FastAPI app, no migrations. Phase 1 has not started.
- **Six open questions (OQ-1…OQ-6) are unresolved**, and three of them block real work: OQ-3 changes
  the sizing formula (Phase 2), OQ-1 and OQ-2 change the reason-code enum and rule-1 evaluation
  order (Phase 2 and 3).
- **Your Q1–Q5 answers are recommendations, not decisions.** The board records them as proposed.
- **No `.gitattributes` yet**, so line endings are not yet pinned. First commit of Phase 1.
- **Nothing here has been executed or verified** — Phase 0 produces no runnable artifact by design.
  The first verifiable milestone is Phase 1's `docker compose up` and a green `/health`.

### What I need from you to start Phase 1

1. Sign off (or amend) **§5 the file tree** and **§6 the columns**, including the four extra tables.
2. Decide **Q1–Q5**.
3. Answer **OQ-1 … OQ-6**, or tell me which to defer.

I will not write application code until those land.
