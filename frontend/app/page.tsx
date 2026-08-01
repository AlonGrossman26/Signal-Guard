"use client";

import { useCallback, useEffect, useState } from "react";
import Shell from "@/components/Shell";
import KillSwitch from "@/components/KillSwitch";
import { api } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import type { BrokerAccount, Decision, Position, WsEvent } from "@/lib/types";

interface FeedItem {
  id: string;
  verdict: string;
  reason_code: string;
  reason_detail: string | null;
  computed_qty: string | null;
  latency_ms: number | null;
  at: string;
}

function fromDecision(d: Decision): FeedItem {
  return {
    id: d.id,
    verdict: d.verdict,
    reason_code: d.reason_code,
    reason_detail: d.reason_detail,
    computed_qty: d.computed_qty,
    latency_ms: d.latency_ms,
    at: d.evaluated_at,
  };
}

export default function LivePage() {
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [accounts, setAccounts] = useState<BrokerAccount[]>([]);

  const loadAccounts = useCallback(() => {
    api.get<BrokerAccount[]>("/api/broker-accounts").then(setAccounts).catch(() => undefined);
  }, []);
  const loadPositions = useCallback(() => {
    api.get<Position[]>("/api/positions").then(setPositions).catch(() => undefined);
  }, []);

  useEffect(() => {
    api
      .get<Decision[]>("/api/decisions?limit=50")
      .then((rows) => setFeed(rows.map(fromDecision)))
      .catch(() => undefined);
    loadPositions();
    loadAccounts();
  }, [loadAccounts, loadPositions]);

  const onEvent = useCallback(
    (event: WsEvent) => {
      if (event.type === "decision" && event.data) {
        const d = event.data as Record<string, unknown>;
        setFeed((prev) =>
          [
            {
              id: String(d.decision_id ?? crypto.randomUUID()),
              verdict: String(d.verdict ?? ""),
              reason_code: String(d.reason_code ?? ""),
              reason_detail: (d.reason_detail as string | null) ?? null,
              computed_qty: (d.computed_qty as string | null) ?? null,
              latency_ms: (d.latency_ms as number | null) ?? null,
              at: new Date().toISOString(),
            },
            ...prev,
          ].slice(0, 200),
        );
      } else if (event.type === "position") {
        loadPositions();
      }
    },
    [loadPositions],
  );

  const wsStatus = useWebSocket(onEvent);

  return (
    <Shell>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Live</h1>
        <span className="flex items-center gap-2 text-sm text-neutral-400">
          <span
            className={`h-2 w-2 rounded-full ${
              wsStatus === "open"
                ? "bg-approve"
                : wsStatus === "connecting"
                  ? "bg-yellow-500"
                  : "bg-reject"
            }`}
          />
          {wsStatus === "open" ? "live" : wsStatus}
        </span>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="lg:col-span-2">
          <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
            Decision feed
          </h2>
          <div className="divide-y divide-neutral-800 rounded border border-neutral-800">
            {feed.length === 0 && (
              <p className="p-4 text-sm text-neutral-500">
                No decisions yet. Fire a test from the Setup page.
              </p>
            )}
            {feed.map((d) => (
              <div key={d.id} className="flex items-center justify-between p-3">
                <div className="flex items-center gap-3">
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-semibold ${
                      d.verdict === "APPROVED"
                        ? "bg-approve/20 text-approve"
                        : "bg-reject/20 text-reject"
                    }`}
                  >
                    {d.reason_code}
                  </span>
                  {d.reason_detail && (
                    <span className="text-sm text-neutral-400">{d.reason_detail}</span>
                  )}
                </div>
                <div className="text-right text-xs text-neutral-500">
                  {d.computed_qty && <div>qty {d.computed_qty}</div>}
                  {d.latency_ms != null && <div>{d.latency_ms} ms</div>}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="flex flex-col gap-6">
          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
              Open positions
            </h2>
            <div className="rounded border border-neutral-800">
              {positions.length === 0 ? (
                <p className="p-4 text-sm text-neutral-500">Flat.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="text-left text-neutral-500">
                    <tr>
                      <th className="p-2">Symbol</th>
                      <th className="p-2">Qty</th>
                      <th className="p-2">uPnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => (
                      <tr key={p.symbol} className="border-t border-neutral-800">
                        <td className="p-2">{p.symbol}</td>
                        <td className="p-2">{p.qty}</td>
                        <td className="p-2">{p.unrealized_pnl ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          <div>
            <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
              Kill switch
            </h2>
            <div className="flex flex-col gap-3">
              {accounts.length === 0 ? (
                <p className="text-sm text-neutral-500">No broker accounts yet.</p>
              ) : (
                accounts.map((a) => (
                  <KillSwitch key={a.id} account={a} onChanged={loadAccounts} />
                ))
              )}
            </div>
          </div>
        </section>
      </div>
    </Shell>
  );
}
