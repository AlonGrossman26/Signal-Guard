"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/lib/types";

const NAV = [
  { href: "/", label: "Live" },
  { href: "/risk", label: "Risk profile" },
  { href: "/history", label: "History" },
  { href: "/setup", label: "Setup" },
];

// Wraps every authenticated page: guards the session (redirect to /login on
// 401) and renders the top navigation. Rendering children only after the user
// is known avoids a flash of dashboard for a logged-out visitor.
export default function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    api
      .get<User>("/api/auth/me")
      .then((u) => {
        setUser(u);
        setChecked(true);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
        } else {
          setChecked(true);
        }
      });
  }, [router]);

  const logout = async () => {
    await api.post("/api/auth/logout").catch(() => undefined);
    router.replace("/login");
  };

  if (!checked || !user) {
    return (
      <div className="flex h-screen items-center justify-center text-neutral-500">
        Loading…
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-neutral-800 bg-neutral-900">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-6">
            <span className="text-lg font-semibold tracking-tight">SignalGuard</span>
            <nav className="flex gap-1">
              {NAV.map((item) => {
                const active = pathname === item.href;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={`rounded px-3 py-1.5 text-sm ${
                      active
                        ? "bg-neutral-800 text-white"
                        : "text-neutral-400 hover:text-white"
                    }`}
                  >
                    {item.label}
                  </Link>
                );
              })}
            </nav>
          </div>
          <div className="flex items-center gap-3 text-sm text-neutral-400">
            <span>{user.email}</span>
            <button
              onClick={logout}
              className="rounded border border-neutral-700 px-2 py-1 hover:bg-neutral-800"
            >
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </div>
  );
}
