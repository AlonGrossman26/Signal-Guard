"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { BrokerAccount } from "@/lib/types";

// The big red button (CLAUDE.md §11): flatten and lock an account. It requires
// an explicit confirmation step, because firing it by accident cancels orders
// and closes positions — this is a two-action control on purpose.
export default function KillSwitch({
  account,
  onChanged,
}: {
  account: BrokerAccount;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const locked = account.trading_state === "LOCKED";

  const fire = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/broker-accounts/${account.id}/kill`);
      setConfirming(false);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  const unlock = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/broker-accounts/${account.id}/unlock`);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2 rounded border border-neutral-800 bg-neutral-900 p-4">
      <div className="flex items-center justify-between">
        <div>
          <div className="font-medium">{account.label}</div>
          <div className="text-sm text-neutral-500">{account.broker}</div>
        </div>
        <span
          className={`rounded px-2 py-0.5 text-xs font-semibold ${
            locked ? "bg-reject/20 text-reject" : "bg-approve/20 text-approve"
          }`}
        >
          {account.trading_state}
        </span>
      </div>

      {locked ? (
        <button
          onClick={unlock}
          disabled={busy}
          className="rounded border border-neutral-700 px-3 py-2 text-sm hover:bg-neutral-800 disabled:opacity-50"
        >
          {busy ? "Unlocking…" : "Unlock (manual, deliberate)"}
        </button>
      ) : confirming ? (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-neutral-300">
            Cancel all orders, close all positions, and lock <b>{account.label}</b>?
          </p>
          <div className="flex gap-2">
            <button
              onClick={fire}
              disabled={busy}
              className="flex-1 rounded bg-reject px-3 py-2 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-50"
            >
              {busy ? "Firing…" : "Yes — FIRE KILL SWITCH"}
            </button>
            <button
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="rounded border border-neutral-700 px-3 py-2 text-sm hover:bg-neutral-800"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setConfirming(true)}
          className="rounded bg-reject/90 px-3 py-2 text-sm font-semibold text-white hover:bg-reject"
        >
          KILL SWITCH
        </button>
      )}

      {error && <p className="text-sm text-reject">{error}</p>}
    </div>
  );
}
