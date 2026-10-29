"use client";

import { FlaskConical, Home, LayoutDashboard, LogOut, Menu, Settings, ShieldCheck, Siren, Store, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Suspense, useEffect } from "react";
import useSWR, { SWRConfig } from "swr";
import { DemoDock } from "@/components/guard/DemoDock";
import { api, ApiError, fetcher } from "@/lib/api";
import { useLiveUpdates } from "@/lib/live";
import { DemoNotice } from "./DemoNotice";
import { SystemPills } from "./SystemPills";

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

  const navLink = (href: string, active: boolean) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
      active ? "bg-blue-50 text-blue-800" : "text-ink-2 hover:bg-gray-100 hover:text-ink-1"
    }`;
  const isActive = (href: string) => (href === "/guard" ? pathname === href : pathname.startsWith(href));
  const current = NAV.find((n) => isActive(n.href));

  return (
    <div className="night flex min-h-screen">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-line bg-white md:flex" aria-label="Checkout Guard">
        <Link href="/guard" className="flex items-center gap-2.5 px-5 pt-5 pb-4">
          <span className="flex size-8 items-center justify-center rounded-lg bg-brand">
            <ShieldCheck className="size-4.5 text-white" aria-hidden />
          </span>
          <span className="leading-tight">
            <span className="block text-[15px] font-semibold text-ink-1">Checkout Guard</span>
            <span className="block text-xs text-ink-3">{me ? "Wick & Wax" : "…"}</span>
          </span>
        </Link>

        <nav className="flex-1 space-y-0.5 px-3" aria-label="Dashboard">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link key={href} href={href} aria-current={isActive(href) ? "page" : undefined} className={navLink(href, isActive(href))}>
              <Icon className="size-4 shrink-0" aria-hidden /> {label}
            </Link>
          ))}
        </nav>

        <div className="space-y-3 px-3 pb-4">
          {me && <DemoDock />}
          <div className="space-y-0.5 border-t border-line pt-3">
            <Link href="/shop" className={navLink("/shop", false)}>
              <Store className="size-4 shrink-0" aria-hidden /> Demo shop
            </Link>
            <Link href="/" className={navLink("/", false)}>
              <Home className="size-4 shrink-0" aria-hidden /> About Checkout Guard
            </Link>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-line bg-white px-4 sm:px-6 lg:px-8">
          <div className="flex min-w-0 items-center gap-2">
            <details className="relative md:hidden">
              <summary className="flex h-9 cursor-pointer list-none items-center gap-2 whitespace-nowrap rounded-lg border border-line px-2.5 text-sm font-medium text-ink-1 [&::-webkit-details-marker]:hidden">
                <Menu className="size-4" aria-hidden /> {current?.label ?? "Menu"}
              </summary>
              <nav className="absolute left-0 z-40 mt-2 w-64 space-y-0.5 rounded-xl border border-line bg-white p-2 shadow-lg" aria-label="Dashboard">
                {NAV.map(({ href, label, icon: Icon }) => (
                  <Link key={href} href={href} aria-current={isActive(href) ? "page" : undefined} className={navLink(href, isActive(href))}>
                    <Icon className="size-4 shrink-0" aria-hidden /> {label}
                  </Link>
                ))}
                <div className="border-t border-line pt-2">{me && <DemoDock />}</div>
              </nav>
            </details>
            <p className="hidden truncate text-sm text-ink-3 md:block">
              PayPal sandbox · <span className="text-ink-2">Wick &amp; Wax</span>
            </p>
          </div>
          <div className="flex items-center gap-2">
            <SystemPills live={live} />
            <button onClick={signOut} className="flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[13px] font-medium text-ink-2 hover:bg-gray-100 hover:text-ink-1">
              <LogOut className="size-4" aria-hidden /> <span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </header>
        <DemoNotice />
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1320px] flex-1 space-y-6 p-4 sm:p-6 lg:p-8">
          {backendDown && (
            <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">
              Can&apos;t reach the Checkout Guard backend. Is it running on :8000?
            </p>
          )}
          {children}
        </main>
      </div>
    </div>
  );
}
