"use client";

import { Loader2, ShieldCheck } from "lucide-react";
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
    <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-900 via-slate-900 to-blue-950 px-6">
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-2xl">
        <div className="flex items-center gap-2.5">
          <div className="flex size-10 items-center justify-center rounded-xl bg-guard">
            <ShieldCheck className="size-5 text-white" />
          </div>
          <div>
            <p className="font-semibold text-slate-900">Checkout Guard</p>
            <p className="text-xs text-slate-500">Wick &amp; Wax · owner dashboard</p>
          </div>
        </div>
        <label className="mt-8 block">
          <span className="text-sm font-medium text-slate-700">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            className="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 outline-none focus:border-guard focus:ring-2 focus:ring-guard/20"
          />
        </label>
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
        <button
          disabled={busy || !password}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-lg bg-guard py-2.5 text-sm font-medium text-white hover:bg-guard-dark disabled:opacity-50"
        >
          {busy && <Loader2 className="size-4 animate-spin" />} Sign in
        </button>
        <p className="mt-4 text-center text-xs text-slate-500">Demo password for judges is in the README.</p>
      </form>
    </main>
  );
}
