"use client";

import { ArrowLeft, Loader2, Lock, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { method: "POST", json: { password } });
      router.replace("/guard");
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Wrong password" : "Can't reach the Checkout Guard backend");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="night relative flex min-h-screen flex-col items-center justify-center px-6">
      <Link href="/" className="absolute top-6 left-6 flex items-center gap-1.5 text-sm text-ink-3 hover:text-ink-1">
        <ArrowLeft className="size-4" /> Back
      </Link>
      <form onSubmit={submit} className="panel w-full max-w-sm p-8">
        <div className="flex items-center gap-3">
          <span className="relative flex size-11 items-center justify-center rounded-xl bg-brand shadow-sm">
            <ShieldCheck className="size-5 text-white" />
          </span>
          <div>
            <p className="font-semibold text-ink-1">Checkout Guard</p>
            <p className="text-xs text-ink-3">Wick &amp; Wax · owner dashboard</p>
          </div>
        </div>
        <label className="mt-8 block">
          <span className="text-sm font-medium text-ink-2">Password</span>
          <span className="relative mt-1.5 block">
            <Lock className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-3" aria-hidden />
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
              className="w-full rounded-lg border border-line bg-gray-50 py-2.5 pr-3 pl-9 text-ink-1 outline-none focus:border-guard/60 focus:ring-2 focus:ring-guard/20"
            />
          </span>
        </label>
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
        <button
          disabled={busy || !password}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg bg-guard py-2.5 text-sm font-medium text-white shadow-sm hover:bg-guard-dark disabled:opacity-50"
        >
          {busy && <Loader2 className="size-4 animate-spin" />} Sign in
        </button>
        <p className="mt-4 text-center text-xs text-ink-3">Judging? The demo password is in the README.</p>
      </form>
    </main>
  );
}
