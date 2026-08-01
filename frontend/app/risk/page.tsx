"use client";

import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { api } from "@/lib/api";
import type { RiskProfile } from "@/lib/types";

// Mirrors the engine's closed-form sizing (OQ-3): the buffer is folded into the
// denominator so a worst-case stop-out never loses more than risk_per_trade_pct.
function previewQty(
  equity: number,
  entry: number,
  stop: number,
  riskPct: number,
  bufferBps: number,
): string {
  const stopDistance = Math.abs(entry - stop);
  const denom = stopDistance + entry * (bufferBps / 10000);
  if (denom <= 0) return "—";
  const qty = (equity * riskPct) / denom;
  return qty.toFixed(6);
}

export default function RiskPage() {
  const [profile, setProfile] = useState<RiskProfile | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [symbols, setSymbols] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.get<RiskProfile>("/api/risk-profile").then((p) => {
      setProfile(p);
      setSymbols(p.allowed_symbols.join(", "));
      setForm({
        risk_per_trade_pct: p.risk_per_trade_pct,
        max_daily_dd_pct: p.max_daily_dd_pct,
        min_stop_distance_pct: p.min_stop_distance_pct,
        fee_slippage_buffer_bps: String(p.fee_slippage_buffer_bps),
        consecutive_loss_threshold: String(p.consecutive_loss_threshold),
        circuit_breaker_cooldown_minutes: String(p.circuit_breaker_cooldown_minutes),
        max_open_positions: String(p.max_open_positions),
        max_notional_per_trade: p.max_notional_per_trade,
        max_total_notional: p.max_total_notional,
        daily_reset_time: p.daily_reset_time,
        timezone: p.timezone,
      });
    });
  }, []);

  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const save = async () => {
    setSaving(true);
    setError(null);
    setMessage(null);
    const body = {
      allowed_symbols: symbols
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      risk_per_trade_pct: form.risk_per_trade_pct,
      max_daily_dd_pct: form.max_daily_dd_pct,
      min_stop_distance_pct: form.min_stop_distance_pct,
      fee_slippage_buffer_bps: Number(form.fee_slippage_buffer_bps),
      consecutive_loss_threshold: Number(form.consecutive_loss_threshold),
      circuit_breaker_cooldown_minutes: Number(form.circuit_breaker_cooldown_minutes),
      max_open_positions: Number(form.max_open_positions),
      max_notional_per_trade: form.max_notional_per_trade,
      max_total_notional: form.max_total_notional,
      daily_reset_time: form.daily_reset_time,
      timezone: form.timezone,
    };
    try {
      const updated = await api.put<RiskProfile>("/api/risk-profile", body);
      setProfile(updated);
      setMessage(`Saved (version ${updated.version}).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setSaving(false);
    }
  };

  if (!profile) {
    return (
      <Shell>
        <p className="text-neutral-500">Loading…</p>
      </Shell>
    );
  }

  const qty = previewQty(
    10000,
    62000,
    61000,
    Number(form.risk_per_trade_pct) || 0,
    Number(form.fee_slippage_buffer_bps) || 0,
  );

  const field = (label: string, key: string, help?: string) => (
    <div>
      <label className="mb-1 block text-sm text-neutral-300">{label}</label>
      <input
        value={form[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
        className="w-full rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
      />
      {help && <p className="mt-1 text-xs text-neutral-500">{help}</p>}
    </div>
  );

  return (
    <Shell>
      <h1 className="mb-1 text-xl font-semibold">Risk profile</h1>
      <p className="mb-6 text-sm text-neutral-500">
        Percentages are fractions: 0.01 means 1%. Every save bumps the version and
        applies from the next decision — never retroactively to an open position.
      </p>

      <div className="mb-6 rounded border border-neutral-800 bg-neutral-900 p-4">
        <div className="text-sm text-neutral-400">Live sizing preview</div>
        <div className="mt-1 text-sm">
          Given <b>$10,000</b> equity, a <b>$62,000</b> entry and a <b>$61,000</b> stop,
          this sizes <b className="text-approve">{qty} BTC</b>.
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label className="mb-1 block text-sm text-neutral-300">Allowed symbols</label>
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            placeholder="BTCUSDT, ETHUSDT"
            className="w-full rounded border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
          />
          <p className="mt-1 text-xs text-neutral-500">
            Comma-separated. Empty means nothing trades (fail closed).
          </p>
        </div>
        {field("Risk per trade", "risk_per_trade_pct", "Fraction of equity risked per trade, e.g. 0.01")}
        {field("Max daily drawdown", "max_daily_dd_pct", "e.g. 0.05 = block after a 5% day")}
        {field("Min stop distance", "min_stop_distance_pct", "Reject a stop closer than this to entry")}
        {field("Fee/slippage buffer (bps)", "fee_slippage_buffer_bps")}
        {field("Consecutive loss threshold", "consecutive_loss_threshold")}
        {field("Circuit breaker cooldown (min)", "circuit_breaker_cooldown_minutes")}
        {field("Max open positions", "max_open_positions")}
        {field("Max notional per trade", "max_notional_per_trade")}
        {field("Max total notional", "max_total_notional")}
        {field("Daily reset time", "daily_reset_time", "HH:MM in the timezone below")}
        {field("Timezone", "timezone", "IANA name, e.g. America/New_York")}
      </div>

      <div className="mt-6 flex items-center gap-3">
        <button
          onClick={save}
          disabled={saving}
          className="rounded bg-white px-4 py-2 font-medium text-neutral-950 hover:bg-neutral-200 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        {message && <span className="text-sm text-approve">{message}</span>}
        {error && <span className="text-sm text-reject">{error}</span>}
      </div>
    </Shell>
  );
}
