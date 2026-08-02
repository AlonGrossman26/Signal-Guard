"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import Shell from "@/components/Shell";
import { api } from "@/lib/api";
import type { Decision, EquityPoint, Trade } from "@/lib/types";

function EquityChart({ points }: { points: EquityPoint[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: "transparent" },
        textColor: "#a3a3a3",
      },
      grid: {
        vertLines: { color: "#262626" },
        horzLines: { color: "#262626" },
      },
      height: 320,
      timeScale: { timeVisible: true },
      autoSize: true,
    });
    chartRef.current = chart;
    const series = chart.addAreaSeries({
      lineColor: "#16a34a",
      topColor: "rgba(22,163,74,0.3)",
      bottomColor: "rgba(22,163,74,0)",
    });

    // Strictly-increasing unique timestamps: keep the last reading per second.
    const bySecond = new Map<number, number>();
    for (const p of points) {
      const t = Math.floor(new Date(p.taken_at).getTime() / 1000);
      bySecond.set(t, Number(p.equity));
    }
    const data = [...bySecond.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([time, value]) => ({ time: time as UTCTimestamp, value }));
    series.setData(data);
    chart.timeScale().fitContent();

    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [points]);

  return <div ref={containerRef} className="w-full" />;
}

export default function HistoryPage() {
  const [points, setPoints] = useState<EquityPoint[]>([]);
  const [byReason, setByReason] = useState<Record<string, number>>({});
  const [trades, setTrades] = useState<Trade[]>([]);

  useEffect(() => {
    api.get<EquityPoint[]>("/api/equity-curve").then(setPoints).catch(() => undefined);
    api.get<Trade[]>("/api/trades?limit=100").then(setTrades).catch(() => undefined);
    api
      .get<Decision[]>("/api/decisions?limit=200")
      .then((rows) => {
        const counts: Record<string, number> = {};
        for (const d of rows) counts[d.reason_code] = (counts[d.reason_code] ?? 0) + 1;
        setByReason(counts);
      })
      .catch(() => undefined);
  }, []);

  const reasons = Object.entries(byReason).sort((a, b) => b[1] - a[1]);

  return (
    <Shell>
      <h1 className="mb-4 text-xl font-semibold">History</h1>

      <section className="mb-8">
        <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
          Equity curve
        </h2>
        <div className="rounded border border-neutral-800 bg-neutral-900 p-2">
          {points.length === 0 ? (
            <p className="p-4 text-sm text-neutral-500">No equity snapshots yet.</p>
          ) : (
            <EquityChart points={points} />
          )}
        </div>
      </section>

      <section className="mb-8">
        <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
          Trade log
        </h2>
        <div className="overflow-x-auto rounded border border-neutral-800">
          {trades.length === 0 ? (
            <p className="p-4 text-sm text-neutral-500">
              No closed trades yet. A trade appears here once a position has been
              opened and closed — it is what the circuit breaker counts.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase text-neutral-500">
                <tr>
                  <th className="p-3">Closed</th>
                  <th className="p-3">Symbol</th>
                  <th className="p-3">Side</th>
                  <th className="p-3 text-right">Qty</th>
                  <th className="p-3 text-right">Entry</th>
                  <th className="p-3 text-right">Exit</th>
                  <th className="p-3 text-right">Fees</th>
                  <th className="p-3 text-right">Realized PnL</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => {
                  // Compare as a string-free sign test: a leading "-" is the only
                  // thing that decides colour, so no float ever enters the render.
                  const isLoss = t.realized_pnl.trimStart().startsWith("-");
                  return (
                    <tr key={t.id} className="border-t border-neutral-800">
                      <td className="p-3 text-neutral-400">
                        {new Date(t.closed_at).toLocaleString()}
                      </td>
                      <td className="p-3">{t.symbol}</td>
                      <td className="p-3 text-neutral-400">{t.side}</td>
                      <td className="p-3 text-right tabular-nums">{t.qty}</td>
                      <td className="p-3 text-right tabular-nums">{t.entry_price}</td>
                      <td className="p-3 text-right tabular-nums">{t.exit_price}</td>
                      <td className="p-3 text-right tabular-nums text-neutral-400">
                        {t.fees}
                      </td>
                      <td
                        className={`p-3 text-right font-semibold tabular-nums ${
                          isLoss ? "text-reject" : "text-approve"
                        }`}
                      >
                        {t.realized_pnl}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-neutral-500">
          Decisions by reason code
        </h2>
        <div className="rounded border border-neutral-800">
          {reasons.length === 0 ? (
            <p className="p-4 text-sm text-neutral-500">No decisions recorded yet.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {reasons.map(([code, count]) => (
                  <tr key={code} className="border-t border-neutral-800 first:border-t-0">
                    <td className="p-3">
                      <span
                        className={`rounded px-2 py-0.5 text-xs font-semibold ${
                          code === "APPROVED"
                            ? "bg-approve/20 text-approve"
                            : "bg-reject/20 text-reject"
                        }`}
                      >
                        {code}
                      </span>
                    </td>
                    <td className="p-3 text-right tabular-nums">{count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </Shell>
  );
}
