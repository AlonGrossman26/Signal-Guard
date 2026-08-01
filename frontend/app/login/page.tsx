"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post<User>(`/api/auth/${mode}`, { email, password });
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-neutral-800 bg-neutral-900 p-6"
      >
        <h1 className="mb-1 text-xl font-semibold">SignalGuard</h1>
        <p className="mb-6 text-sm text-neutral-500">
          {mode === "login" ? "Sign in to your dashboard." : "Create an account."}
        </p>

        <label className="mb-1 block text-sm text-neutral-400">Email</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          className="mb-4 w-full rounded border border-neutral-700 bg-neutral-950 px-3 py-2"
        />

        <label className="mb-1 block text-sm text-neutral-400">Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={mode === "register" ? 12 : 1}
          className="mb-2 w-full rounded border border-neutral-700 bg-neutral-950 px-3 py-2"
        />
        {mode === "register" && (
          <p className="mb-4 text-xs text-neutral-500">At least 12 characters.</p>
        )}

        {error && <p className="mb-4 text-sm text-reject">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-white px-3 py-2 font-medium text-neutral-950 hover:bg-neutral-200 disabled:opacity-50"
        >
          {busy ? "…" : mode === "login" ? "Sign in" : "Register"}
        </button>

        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
          className="mt-4 w-full text-center text-sm text-neutral-400 hover:text-white"
        >
          {mode === "login"
            ? "Need an account? Register"
            : "Have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}
