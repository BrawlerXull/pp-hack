"use client";

import { FlaskConical, LayoutDashboard, LogOut, Settings, ShieldCheck, Siren, Store } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Suspense, useEffect } from "react";
import useSWR, { SWRConfig } from "swr";
import { api, ApiError, fetcher } from "@/lib/api";
import { useLiveUpdates } from "@/lib/live";
import { SystemPills } from "./SystemPills";

const NAV = [
  { href: "/guard", label: "War Room", icon: LayoutDashboard },
  { href: "/guard/incidents", label: "Incidents", icon: Siren },
  { href: "/guard/settings", label: "Settings", icon: Settings },
  { href: "/guard/lab", label: "Demo lab", icon: FlaskConical },
];

export function GuardShell({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: true }}>
      {/* Reading the URL (usePathname/useParams) on dynamic routes must sit inside Suspense. */}
      <Suspense fallback={<div className="flex min-h-screen items-center justify-center text-sm text-slate-500">Loading…</div>}>
        <Shell>{children}</Shell>
      </Suspense>
    </SWRConfig>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { data: me, error } = useSWR<{ shop_id: string }>("/auth/me", fetcher, { shouldRetryOnError: false });
  const live = useLiveUpdates();

  useEffect(() => {
    if (error instanceof ApiError && error.status === 401) router.replace("/login");
  }, [error, router]);

  // Always render the page (it shows skeletons until data arrives); unauthenticated visitors are
  // redirected to /login as soon as /auth/me answers 401.
  const backendDown = error && !(error instanceof ApiError && error.status === 401);

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-slate-200 bg-white md:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex size-9 items-center justify-center rounded-xl bg-guard">
            <ShieldCheck className="size-5 text-white" />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold text-slate-900">Checkout Guard</p>
            <p className="text-xs text-slate-500">{me ? "Wick & Wax" : "…"}</p>
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 px-3">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === "/guard" ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium ${
                  active ? "bg-blue-50 text-guard" : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                }`}
              >
                <Icon className="size-4" /> {label}
              </Link>
            );
          })}
        </nav>
        <div className="space-y-1 border-t border-slate-100 p-3">
          <Link href="/" className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-50">
            <Store className="size-4" /> View shop
          </Link>
          <button
            onClick={async () => {
              await api("/auth/logout", { method: "POST" });
              router.replace("/login");
            }}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-50"
          >
            <LogOut className="size-4" /> Sign out
          </button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 md:hidden">
          <span className="flex items-center gap-2 text-sm font-semibold"><ShieldCheck className="size-4 text-guard" /> Checkout Guard</span>
          <nav className="flex gap-3 text-xs">
            {NAV.map((n) => (
              <Link key={n.href} href={n.href} className="text-slate-600">{n.label}</Link>
            ))}
          </nav>
        </header>
        {/* System pills on every page, phones included: AI, detector, PayPal, live updates. */}
        <div className="border-b border-slate-200 bg-white/80 px-4 py-2 sm:px-6 lg:px-8">
          <SystemPills live={live} />
        </div>
        <main className="mx-auto w-full max-w-7xl flex-1 space-y-6 p-4 sm:p-6 lg:p-8">
          {backendDown && (
            <p className="rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">
              Can&apos;t reach the Checkout Guard backend. Is it running on :8000?
            </p>
          )}
          {children}
        </main>
      </div>
    </div>
  );
}
