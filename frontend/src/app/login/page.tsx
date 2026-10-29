"use client";

import { Loader2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AuthCard, Field, FormError } from "@/components/auth/AuthCard";
import { api, ApiError, type AuthOptions } from "@/lib/api";

export default function LoginPage() {
  return (
    <Suspense>
      <Login />
    </Suspense>
  );
}

/** Only same-site dashboard paths: ?next= must never send someone to another site after signing in. */
function safeNext(next: string | null): string {
  return next && next.startsWith("/guard") ? next : "/guard";
}

function Login() {
  const next = safeNext(useSearchParams().get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [options, setOptions] = useState<AuthOptions | null>(null);

  useEffect(() => {
    api<AuthOptions>("/auth/options").then(setOptions, () => setOptions(null));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { method: "POST", json: { email, password } });
      // A full load: no cached answer from before signing in (or from another account) survives.
      window.location.replace(next);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "Wrong email or password."
          : err instanceof ApiError && err.status === 429
            ? "Too many attempts. Wait a few minutes and try again."
            : "Can't reach Checkout Guard. Try again in a moment.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard
      title="Sign in"
      subtitle="Wick & Wax · Checkout Guard dashboard"
      footer={
        options?.signup_enabled !== false && (
          <>
            New here?{" "}
            <Link href={`/signup${next !== "/guard" ? `?next=${encodeURIComponent(next)}` : ""}`} className="font-medium text-guard hover:underline">
              Create an account
            </Link>
          </>
        )
      }
    >
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Email" type="email" autoComplete="email" inputMode="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        <Field label="Password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error && <FormError>{error}</FormError>}
        <button
          type="submit"
          disabled={busy || !email || !password}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-guard py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-guard-dark disabled:opacity-50"
        >
          {busy && <Loader2 className="size-4 animate-spin" aria-hidden />} Sign in
        </button>
      </form>
      {options && (
        <div className="mt-6 rounded-lg border border-line bg-gray-50 p-3 text-sm">
          <p className="font-medium text-ink-1">Judging the demo?</p>
          <p className="mt-0.5 text-ink-3">Use the shared owner account. Its password is in the README.</p>
          <button
            type="button"
            onClick={() => {
              setEmail(options.demo_email);
              document.querySelector<HTMLInputElement>('input[type="password"]')?.focus();
            }}
            className="mt-2 font-medium text-guard hover:underline"
          >
            Fill in {options.demo_email}
          </button>
        </div>
      )}
    </AuthCard>
  );
}
