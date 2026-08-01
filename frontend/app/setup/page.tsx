"use client";

import { useCallback, useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { API_BASE, api } from "@/lib/api";
import type { BrokerAccount, WebhookEndpointCreated } from "@/lib/types";

export default function SetupPage() {
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);
  const [label, setLabel] = useState("binance-testnet-1");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [created, setCreated] = useState<WebhookEndpointCreated | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadAccounts = useCallback(() => {
    api.get<BrokerAccount[]>("/api/broker-accounts").then(setAccounts).catch(() => undefined);
  }, []);
  useEffect(() => loadAccounts(), [loadAccounts]);

  const addAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/broker-accounts", {
        label,
        api_key: apiKey,
        api_secret: apiSecret,
      });
      setApiKey("");
      setApiSecret("");
      loadAccounts();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    }
  };

  const createEndpoint = async () => {
    setError(null);
    try {
      setCreated(await api.post<WebhookEndpointCreated>("/api/webhook-endpoints"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    }
  };

  return (
    <Shell>
      <h1 className="mb-6 text-xl font-semibold">Setup</h1>

      <section className="mb-8">
        <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
          Broker accounts
        </h2>
        <div className="mb-3 rounded border border-neutral-800">
          {accounts.length === 0 ? (
            <p className="p-3 text-sm text-neutral-500">None yet.</p>
          ) : (
            accounts.map((a) => (
              <div
                key={a.id}
                className="flex justify-between border-b border-neutral-800 p-3 text-sm last:border-b-0"
              >
                <span>{a.label}</span>
                <span className="text-neutral-500">{a.trading_state}</span>
              </div>
            ))
          )}
        </div>
        <form
          onSubmit={addAccount}
          className="grid gap-2 rounded border border-neutral-800 bg-neutral-900 p-4 md:grid-cols-3"
        >
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="label"
            className="rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
          />
          <input
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="testnet API key"
            className="rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
          />
          <input
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            placeholder="testnet API secret"
            type="password"
            className="rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            className="rounded bg-white px-3 py-2 text-sm font-medium text-neutral-950 hover:bg-neutral-200 md:col-span-3"
          >
            Add account (testnet only)
          </button>
        </form>
        <p className="mt-2 text-xs text-neutral-500">
          Create the key with trading enabled and withdrawals disabled, IP-restricted
          to your server.
        </p>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
          Webhook endpoint
        </h2>
        <button
          onClick={createEndpoint}
          className="rounded bg-white px-4 py-2 text-sm font-medium text-neutral-950 hover:bg-neutral-200"
        >
          Create a new webhook endpoint
        </button>

        {created && <CreatedEndpoint created={created} />}
      </section>

      {error && <p className="mt-4 text-sm text-reject">{error}</p>}
    </Shell>
  );
}

function CreatedEndpoint({ created }: { created: WebhookEndpointCreated }) {
  const url = `${API_BASE}/webhook/${created.endpoint_token}`;
  const template = JSON.stringify(
    {
      secret: created.body_secret,
      id: "{{strategy.order.id}}",
      timestamp: "{{timenow}}",
      account: "binance-testnet-1",
      symbol: "{{ticker}}",
      action: "buy",
      order_type: "limit",
      limit_price: "{{close}}",
      stop_price: "{{plot_0}}",
    },
    null,
    2,
  );

  return (
    <div className="mt-4 rounded border border-yellow-700/50 bg-yellow-950/20 p-4">
      <p className="mb-3 text-sm text-yellow-500">
        These are shown once. Copy them now — the server only stores hashes and cannot
        show them again.
      </p>
      <Field label="Webhook URL" value={url} />
      <Field label="HMAC secret (preferred)" value={created.hmac_secret} />
      <Field label="Body secret (TradingView fallback)" value={created.body_secret} />
      <div className="mt-3">
        <div className="mb-1 text-sm text-neutral-300">TradingView alert message</div>
        <pre className="overflow-x-auto rounded border border-neutral-800 bg-neutral-950 p-3 text-xs text-neutral-300">
          {template}
        </pre>
        <p className="mt-1 text-xs text-neutral-500">
          The in-body secret is weaker than an HMAC signature. Use HMAC headers where
          your sender supports them.
        </p>
      </div>
      <TestForm token={created.endpoint_token} bodySecret={created.body_secret} />
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-2">
      <div className="text-xs text-neutral-500">{label}</div>
      <code className="block overflow-x-auto rounded bg-neutral-950 px-2 py-1 text-xs">
        {value}
      </code>
    </div>
  );
}

function TestForm({ token, bodySecret }: { token: string; bodySecret: string }) {
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const runTest = async () => {
    setBusy(true);
    setResult(null);
    const body = {
      secret: bodySecret,
      id: `test-${Date.now()}`,
      timestamp: new Date().toISOString(),
      account: "binance-testnet-1",
      symbol: "BTCUSDT",
      action: "buy",
      order_type: "limit",
      limit_price: "62000.00",
      stop_price: "61000.00",
    };
    try {
      const res = await fetch(`${API_BASE}/webhook/${token}/test`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      setResult(
        `${data.verdict ?? "?"} — ${data.reason_code ?? "?"}` +
          (data.computed_qty ? ` (qty ${data.computed_qty})` : ""),
      );
    } catch {
      setResult("request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-4 border-t border-neutral-800 pt-3">
      <button
        onClick={runTest}
        disabled={busy}
        className="rounded border border-neutral-600 px-3 py-2 text-sm hover:bg-neutral-800 disabled:opacity-50"
      >
        {busy ? "Testing…" : "Send a test signal (no broker)"}
      </button>
      {result && (
        <p className="mt-2 text-sm">
          Decision: <b>{result}</b>
        </p>
      )}
      <p className="mt-1 text-xs text-neutral-500">
        Runs the full risk pipeline and returns the decision without touching the
        broker.
      </p>
    </div>
  );
}
