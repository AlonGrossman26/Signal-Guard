# SignalGuard — Frontend

Next.js (App Router) + TypeScript + Tailwind dashboard for SignalGuard. Four
pages, all session-authenticated against the backend `/api`:

- **Live** (`/`) — streaming decision feed (colour-coded by verdict, each
  rejection showing its reason), open positions, and the kill switch (a two-step
  confirm control) per account. Fed live by the `/ws` WebSocket, with a REST
  backfill on load.
- **Risk profile** (`/risk`) — a form for the §7 parameters with a live sizing
  preview ("given $10,000 equity, a $62,000 entry and a $61,000 stop, this sizes
  X BTC"). Saving bumps the profile version.
- **History** (`/history`) — the equity curve (TradingView Lightweight Charts)
  and a breakdown of decisions by reason code.
- **Setup** (`/setup`) — add a testnet broker account, mint a webhook endpoint
  (token + secrets shown once), copy a ready-made TradingView alert template, and
  send a test signal through the full risk pipeline without touching the broker.

## Develop

```bash
npm install
# Point the UI at the backend (defaults to http://localhost:8000 if unset):
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local
npm run dev                          # http://localhost:3000
```

The backend must be running and must allow this origin for credentialed requests
(`CORS_ALLOW_ORIGINS` in the backend `.env`, default `http://localhost:3000`).

## Verify

```bash
npm run typecheck   # tsc --noEmit
npm run build       # production build
```

## Notes

- Money and quantities are strings end to end (never floats) — constraint #3
  applies on the wire and in this UI.
- Auth is a session cookie; every request sends `credentials: "include"`. The
  `Shell` component guards each page and redirects to `/login` on 401.
