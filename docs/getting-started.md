# Getting started — from nothing to a live test trade

This is the **first-run walkthrough**: download the project, start it, connect a testnet broker,
and watch it place a real order on Binance's test exchange. Follow it top to bottom the first time.

Assumes no prior setup. About **30–40 minutes**, most of it waiting for downloads.

- Already running and something broke at 3am? → [`runbook.md`](./runbook.md)
- Want the deployment reference rather than a walkthrough? → [`deploy.md`](./deploy.md)

> **Everything here is fake money.** Binance Spot **Testnet** is a separate exchange with play
> balances. Live trading is blocked three ways — a config flag, a confirmation phrase, and a
> database `CHECK` constraint — and no phase has authorised turning it on. Do not try.

Commands are given for **PowerShell** (the Windows default) and **Git Bash**. Pick one column and
stay in it — the two take different syntax.

> **PowerShell note:** `&&` is a syntax error in PowerShell 5.1. Run the commands one line at a
> time exactly as written, rather than chaining them.

---

## Step 0 — Install the three things you need

| Tool | Why | Where |
|---|---|---|
| **Docker Desktop** | Runs the database, cache and API in containers so you don't install them by hand | [docker.com](https://www.docker.com/products/docker-desktop/) |
| **Git** | Downloads the code | [git-scm.com](https://git-scm.com/downloads) |
| **Node.js 20+** | Runs the dashboard (it is not containerized yet — see step 7) | [nodejs.org](https://nodejs.org/) — pick the LTS build |

Install Docker Desktop first and **launch it**. It has to be actually running, not just installed —
you should see the whale icon in your system tray. Every `docker` command below fails with
"cannot connect to the Docker daemon" if it isn't.

Check all three are ready:

```powershell
docker --version
git --version
node --version
```

---

## Step 1 — Download the code

```powershell
git clone https://github.com/AlonGrossman26/Signal-Guard.git
cd Signal-Guard
```

Everything from here runs from inside that `Signal-Guard` folder.

---

## Step 2 — Create your configuration file

The project ships `.env.example` — a template with the key names and no values. Copy it to `.env`,
which is where your real settings live.

```powershell
Copy-Item .env.example .env
```
```bash
cp .env.example .env                # Git Bash
```

`.env` is in `.gitignore` and must never be committed — it will hold your secrets.

---

## Step 3 — Generate the three secrets

The app **refuses to start** if any of these is missing. That is deliberate: a service that boots
with half its security configured is worse than one that doesn't boot.

Run each command and copy its output into the matching line in `.env`:

```powershell
# CREDENTIALS_MASTER_KEY — encrypts your broker API keys at rest
python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"

# ENDPOINT_ID_PEPPER — stops a stolen database from letting someone forge webhook URLs
python -c "import secrets;print(secrets.token_urlsafe(32))"

# SESSION_SECRET — signs your dashboard login cookie
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

> No Python on your machine? Run each one inside Docker instead:
> `docker run --rm python:3.12-alpine python -c "import secrets;print(secrets.token_urlsafe(32))"`

Then set the database password. Open `.env` in any text editor and:

1. Set `POSTGRES_PASSWORD` to anything you like, e.g. `POSTGRES_PASSWORD=localdevpassword`
2. Put **the same value** into `DATABASE_URL`, between `signalguard:` and `@db`:

```
DATABASE_URL=postgresql+asyncpg://signalguard:localdevpassword@db:5432/signalguard
```

Leave everything else as it is. In particular leave `LIVE_TRADING_ENABLED=false`.

**Why `@db` and not `localhost`?** Inside Docker, containers reach each other by service name.
`localhost` from inside the API container means *the API container itself*, which has no database.

---

## Step 4 — Start the stack

```powershell
docker compose up -d --build
```

First run takes a few minutes — it downloads Postgres, Redis and Python, then builds the API.
Later runs take seconds.

`-d` means "detached" (runs in the background). Check all three are healthy:

```powershell
docker compose ps
```

You want `db`, `redis` and `api` all showing `running`, with `db` and `redis` marked `healthy`.

---

## Step 5 — Create the database tables

The containers are running but the database is empty. Migrations create the schema:

```powershell
docker compose exec api uv run alembic upgrade head
```

This is a separate, deliberate step — the app never silently changes your database schema just
because a container restarted.

---

## Step 6 — Check it's alive

```powershell
Invoke-RestMethod http://localhost:8000/health
```
```bash
curl http://localhost:8000/health   # Git Bash
```

You want:

```json
{"status":"green","checks":{"postgres":{"status":"up"},"redis":{"status":"up"}}}
```

`green` means the API can reach both Postgres and Redis. If you get a 503 the response names which
dependency is down — it reports honestly rather than returning 200 whenever the web server is up.

---

## Step 7 — Start the dashboard

The dashboard is **not** in Docker Compose yet, so it runs separately. Open a **second terminal**,
leave the first one alone, and:

```powershell
cd Signal-Guard\frontend
npm install
npm run dev
```

`npm install` takes a minute the first time. When it says `ready`, open **http://localhost:3000**.

> Use `localhost`, not `127.0.0.1`. The API only accepts browser requests from
> `http://localhost:3000` by default, so `127.0.0.1` gets blocked by CORS and the pages come up
> empty. If you want a different address, change `CORS_ALLOW_ORIGINS` in `.env` and restart the API.

Register an account on the login page — the first user is created by simply registering. Use a real
password (12+ characters); it's hashed with Argon2id and never stored in the clear.

---

## Step 8 — Get your Binance testnet API key

This is a **completely separate site** from binance.com, with play money and no KYC.

1. Go to **[testnet.binance.vision](https://testnet.binance.vision)**
2. Click **Log in with GitHub** — that is the only sign-in it has
3. Click **Generate HMAC_SHA256 Key** and give it any label
4. **Copy both the API Key and the Secret Key now.** The secret is shown once and never again — if
   you lose it, delete the key and generate a new one

Testnet credits your account with fake balances automatically. There is nothing to fund.

**Two warnings.**

- **Never put a real binance.com API key in here.** The database has a `CHECK` constraint that
  refuses to store a non-testnet account, but treat that as a backstop, not your first line of
  defence.
- When you do eventually use a real exchange, create the key with **trading enabled and withdrawals
  disabled**, IP-restricted to your server. SignalGuard refuses to save a key the exchange reports
  as having withdrawal permission — but the testnet does not expose that endpoint at all, so it will
  save your testnet key and log that it could not verify. That is expected behaviour, not a failure.

---

## Step 9 — Connect the broker and set your rules

In the dashboard:

**Setup page** → add a broker account:

- **Label:** `binance-testnet-1` (this exact string goes in your alerts, so keep it simple)
- **API key** and **API secret:** paste from step 8

Then click to create a **webhook endpoint**. You'll get a URL, an HMAC secret and a body secret —
**copy all three now**, they are shown once.

**Risk profile page** → at minimum set:

- **Allowed symbols:** `BTCUSDT`
  An empty allowlist trades **nothing**. That is default-deny on purpose: a new or misconfigured
  profile should trade nothing rather than everything.
- Leave the rest at their defaults to start — 1% risk per trade, 5% daily drawdown, 3 consecutive
  losses to trip the circuit breaker.

The page shows a live sizing preview as you type, so you can see what your settings actually mean
in units before any real signal arrives.

---

## Step 10 — Send a test signal (no broker contact)

Still on the Setup page, use the **test button**. This runs the risk pipeline and returns the
decision **without ever touching the exchange**, so nothing can reach your broker by accident.

The best thing to check here is that **your payload is well-formed and your stop is on the right
side**. Send a *limit* order with the stop above your entry price and you get exactly what you'd
hope for:

```
NO_STOP_LOSS — long stop 63000.00 must be below entry 62000.00
```

That is the check worth rehearsing before a live signal does it for you.

**Know what this endpoint cannot tell you.** Because it deliberately never contacts the broker, it
has no account data, and that shapes the answers:

- **It will never return `APPROVED`.** With no equity figure there is no daily-drawdown baseline, so
  rule 8 fails closed and a perfectly valid signal comes back `DAILY_DRAWDOWN_HIT — no valid equity
  baseline`. That is the fail-closed design working, not your signal being wrong.
- **Market orders always report `NO_STOP_LOSS — no reference price available`.** A market order has
  no price of its own, and fetching the current price is broker work. Use `order_type: "limit"` with
  a `limit_price` when testing, since a limit order carries its own price.
- **A symbol you haven't traded reports `INSTRUMENT_UNAVAILABLE`, not `SYMBOL_NOT_ALLOWED`.** The
  exchange-filter lookup happens before the rule chain, so an uncached symbol is refused before the
  allowlist is ever consulted. Both are rejections; the reason code just names the earlier cause.

So: use `/test` for payload and stop-side mistakes, and use a real signal (step 11) to see approvals
and sizing.

---

## Step 11 — Send a real signal

Now the actual thing. Send a signal to your webhook URL from step 9:

```powershell
$body = @{
  secret      = "<YOUR BODY SECRET>"
  id          = "first-live-test-1"
  timestamp   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  account     = "binance-testnet-1"
  symbol      = "BTCUSDT"
  action      = "buy"
  order_type  = "market"
  stop_price  = "50000.00"
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/webhook/<YOUR TOKEN>" `
  -ContentType "application/json" -Body $body
```

Set `stop_price` **below** the current BTC price — a buy needs its stop underneath the entry, and
not too close to it (a near-zero stop distance produces an absurd position size, which is exactly
what rule 6 exists to catch).

You get `{"status":"accepted"}` back within milliseconds. That is by design: the alert is recorded
immediately, and risk evaluation plus order submission happen in the background so a slow exchange
can never make your strategy time out and retry.

**Now watch the Live page.** Within a second or two you should see the decision appear in the feed.
If it was approved, an entry **and** its protective stop go to the testnet — check
[testnet.binance.vision](https://testnet.binance.vision) and you should see both orders there.

If it was rejected, the reason code tells you exactly which rule stopped it. That is the product
working, not the product failing.

---

## Step 12 — Connect TradingView (optional)

The Setup page gives you a copy-paste alert template. In TradingView, create an alert and:

- **Webhook URL:** your endpoint URL
- **Message:** the template from the Setup page

TradingView's free plan cannot send custom headers, which is why the body-secret mode exists. It is
weaker than HMAC signing — the secret travels in the message body rather than as a signature — and
the UI says so. Use HMAC if your plan allows it.

---

## Stopping and starting

```powershell
docker compose stop          # stop, keep all data
docker compose up -d         # start again
docker compose logs -f api   # watch the logs live
```

**Careful:** `docker compose down -v` deletes all your data, audit trail included. Almost never
what you want.

---

## When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `cannot connect to the Docker daemon` | Docker Desktop isn't running | Launch it, wait for the whale icon |
| API container keeps restarting | A secret is missing or too short | `docker compose logs api` — the error names the exact variable and the command to generate it |
| `/health` returns 503 | Postgres or Redis is down | `docker compose ps`, then `docker compose logs db` |
| Dashboard loads but every page is empty | Opened `127.0.0.1:3000` instead of `localhost:3000` | Use `localhost` — see step 7 |
| Every signal rejects `SYMBOL_NOT_ALLOWED` | Empty allowlist (default-deny) | Add `BTCUSDT` on the Risk profile page |
| Every signal rejects `INSTRUMENT_UNAVAILABLE` | Exchange filters not cached yet | The reconciler fetches them within ~15s of a working broker account existing. Check `docker compose logs api` for `Could not build a broker adapter` — usually wrong API keys |
| Every signal rejects `BROKER_UNAVAILABLE` | The API can't reach the exchange | Check your keys, and that outbound HTTPS to `testnet.binance.vision` isn't blocked |
| Signal rejects `NO_STOP_LOSS` on a market order | Stop on the wrong side, too close to entry, or no reference price | Buy stops go *below* entry, at least 0.1% away. On the **`/test`** endpoint this is expected for market orders — see step 10 |
| `/test` never returns `APPROVED` | Expected — it has no broker data, so there is no equity baseline | Use a real signal (step 11) to see approvals and sizing |

The full triage guide — including the kill switch, backups and rollback — is in
[`runbook.md`](./runbook.md).

---

## What to do next

1. **Let it run for a day** with a strategy firing at it. Watch a real fill get reconciled, a real
   trade appear in the History trade log, the equity curve fill in.
2. **Try the kill switch** on the Live page while holding a position. It cancels orders, closes
   positions, and locks the account — and the reconciler keeps it flat until you explicitly unlock.
3. **Read your rejections.** The History page breaks them down by reason code. That breakdown is
   the fastest way to learn whether your strategy or your risk settings need adjusting.
