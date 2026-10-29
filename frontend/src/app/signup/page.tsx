"use client";

import { Loader2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AuthCard, Field, FormError } from "@/components/auth/AuthCard";
import { api, ApiError, type AuthOptions } from "@/lib/api";

const MIN_PASSWORD = 8;

export default function SignupPage() {
  return (
    <Suspense>
      <Signup />
    </Suspense>
  );
}

function Signup() {
  const nextParam = useSearchParams().get("next");
  const next = nextParam && nextParam.startsWith("/guard") ? nextParam : "/guard";
  const [form, setForm] = useState({ name: "", email: "", password: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [options, setOptions] = useState<AuthOptions | null>(null);

  useEffect(() => {
    api<AuthOptions>("/auth/options").then(setOptions, () => setOptions(null));
  }, []);

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const tooShort = form.password.length > 0 && form.password.length < MIN_PASSWORD;
  const mismatch = form.confirm.length > 0 && form.confirm !== form.password;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (tooShort || mismatch) return;
    setBusy(true);
    setError(null);
    try {
      await api("/auth/signup", { method: "POST", json: { name: form.name, email: form.email, password: form.password } });
      // A full load: no cached answer from before signing in (or from another account) survives.
      window.location.replace(next);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach Checkout Guard. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  const closed = options?.signup_enabled === false;
  return (
    <AuthCard
      title="Create your account"
      subtitle="Join the Wick & Wax team on Checkout Guard"
      footer={
        <>
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-guard hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      {closed ? (
        <p className="text-sm text-ink-2">Sign-ups are closed. Ask a shop owner to create an account for you.</p>
      ) : (
        <form onSubmit={submit} className="space-y-4" noValidate>
          <Field label="Your name" autoComplete="name" value={form.name} onChange={set("name")} required autoFocus />
          <Field label="Email" type="email" autoComplete="email" inputMode="email" value={form.email} onChange={set("email")} required />
          <Field
            label="Password"
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={set("password")}
            required
            minLength={MIN_PASSWORD}
            aria-invalid={tooShort}
            hint={`At least ${MIN_PASSWORD} characters.`}
          />
          <Field label="Confirm password" type="password" autoComplete="new-password" value={form.confirm} onChange={set("confirm")} required aria-invalid={mismatch} />
          {mismatch && <FormError>The passwords don&apos;t match.</FormError>}
          {error && <FormError>{error}</FormError>}
          {options?.signup_role === "viewer" && (
            <p className="text-xs text-ink-3">
              New members join as <strong className="font-medium text-ink-2">viewers</strong>: you can see everything and run simulations. A shop owner can make you an owner to change
              settings and approve refunds.
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !form.name || !form.email || !form.password || tooShort || mismatch || !form.confirm}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-guard py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-guard-dark disabled:opacity-50"
          >
            {busy && <Loader2 className="size-4 animate-spin" aria-hidden />} Create account
          </button>
        </form>
      )}
    </AuthCard>
  );
}
