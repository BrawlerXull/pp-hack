"use client";

import { FlaskConical, Home, LayoutDashboard, LogOut, Moon, Settings, ShieldCheck, Siren, Store, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Suspense, useEffect } from "react";
import useSWR, { SWRConfig } from "swr";
import { DemoDock } from "@/components/guard/DemoDock";
import { api, ApiError, fetcher } from "@/lib/api";
import { useLiveUpdates } from "@/lib/live";
import { DemoNotice } from "./DemoNotice";
import { SystemPills } from "./SystemPills";
import { LiveDot } from "./ui";

const NAV = [
  { href: "/guard", label: "War Room", icon: LayoutDashboard },
  { href: "/guard/incidents", label: "Incidents", icon: Siren },
  { href: "/guard/investigate", label: "Investigate", icon: Telescope },
  { href: "/guard/settings", label: "Settings", icon: Settings },
  { href: "/guard/lab", label: "Demo lab", icon: FlaskConical },
];

export function GuardShell({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: true }}>
      {/* Reading the URL (usePathname/useParams) on dynamic routes must sit inside Suspense. */}
      <Suspense fallback={<div className="night flex min-h-screen items-center justify-center text-sm text-ink-3">Loading…</div>}>
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
  const signOut = async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      router.replace("/login");
    }
  };

  return (
    <div className="night flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-white/[0.06] bg-night-900/70 backdrop-blur md:flex">
        <Link href="/guard" className="flex items-center gap-3 px-5 pt-6 pb-5">
          <span className="relative flex size-10 items-center justify-center rounded-xl bg-gradient-to-br from-guard to-indigo-600 shadow-[0_10px_30px_-10px_rgb(76_141_255/0.8)]">
            <ShieldCheck className="size-5 text-white" />
            <Moon className="absolute -top-1 -right-1 size-3.5 fill-amber-200 text-amber-200" />
          </span>
          <span className="leading-tight">
            <span className="block text-[15px] font-semibold tracking-tight text-ink-1">Checkout Guard</span>
            <span className="block text-xs text-ink-3">Night guard · {me ? "Wick & Wax" : "…"}</span>
          </span>
        </Link>

        <nav className="flex-1 space-y-1 px-3" aria-label="Dashboard">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = href === "/guard" ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active ? "bg-white/[0.07] text-ink-1 shadow-[inset_2px_0_0_0_var(--color-guard)]" : "text-ink-2 hover:bg-white/[0.04] hover:text-ink-1"
                }`}
              >
                <Icon className={`size-4 ${active ? "text-guard" : "text-ink-3 group-hover:text-ink-2"}`} /> {label}
              </Link>
            );
          })}
        </nav>

        <div className="space-y-2 px-3 pb-3">
          <div className="space-y-0.5 border-t border-white/[0.06] pt-2">
            <Link href="/shop" className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-white/[0.04] hover:text-ink-1">
              <Store className="size-4 text-ink-3" /> Demo shop
            </Link>
            <Link href="/" className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-white/[0.04] hover:text-ink-1">
              <Home className="size-4 text-ink-3" /> About Checkout Guard
            </Link>
            <button onClick={signOut} className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-2 hover:bg-white/[0.04] hover:text-ink-1">
              <LogOut className="size-4 text-ink-3" /> Sign out
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 border-b border-white/[0.06] bg-night-900/85 backdrop-blur md:hidden">
          <div className="flex items-center justify-between px-4 py-3">
            <Link href="/guard" className="flex items-center gap-2 text-sm font-semibold text-ink-1">
              <ShieldCheck className="size-4 text-guard" /> Checkout Guard
              <LiveDot tone={live === "live" ? "good" : "neutral"} />
            </Link>
            <button onClick={signOut} className="rounded-lg p-1.5 text-ink-2 hover:bg-white/[0.06]" aria-label="Sign out">
              <LogOut className="size-4" />
            </button>
          </div>
          <nav className="flex gap-1 overflow-x-auto px-3 pb-2 text-xs" aria-label="Dashboard">
            {NAV.map((n) => {
              const active = n.href === "/guard" ? pathname === n.href : pathname.startsWith(n.href);
              return (
                <Link
                  key={n.href}
                  href={n.href}
                  className={`shrink-0 rounded-full px-3 py-1.5 font-medium ${active ? "bg-white/10 text-ink-1" : "text-ink-2"}`}
                >
                  {n.label}
                </Link>
              );
            })}
          </nav>
        </header>
        <div className="border-b border-white/[0.06] bg-night-900/50 px-4 py-2.5 backdrop-blur sm:px-6 md:sticky md:top-0 md:z-20 lg:px-8">
          <SystemPills live={live} />
        </div>
        <DemoNotice />
        <main className="mx-auto w-full max-w-[1400px] flex-1 space-y-6 p-4 pb-28 sm:p-6 sm:pb-28 lg:p-8 lg:pb-28">
          {backendDown && (
            <p className="rounded-lg border border-red-400/30 bg-red-500/10 px-4 py-2 text-sm text-red-200">
              Can&apos;t reach the Checkout Guard backend. Is it running on :8000?
            </p>
          )}
          {children}
        </main>
      </div>
      {me && <DemoDock />}
    </div>
  );
}
