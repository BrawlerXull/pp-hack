import { ShieldCheck, WifiOff } from "lucide-react";

export const metadata = { title: "Offline" };

/** Shown by the service worker when a page can't load without a connection. */
export default function OfflinePage() {
  return (
    <main id="main" className="night flex min-h-screen flex-col items-center justify-center px-6 text-center">
      <span className="flex size-12 items-center justify-center rounded-xl bg-brand">
        <ShieldCheck className="size-6 text-white" aria-hidden />
      </span>
      <h1 className="mt-5 flex items-center gap-2 text-xl font-semibold text-ink-1">
        <WifiOff className="size-5 text-ink-3" aria-hidden /> You&apos;re offline
      </h1>
      <p className="mt-2 max-w-sm text-sm text-ink-2">
        Checkout Guard keeps protecting your checkout on the server. Reconnect to see live attempts, incidents and alerts.
      </p>
      <a href="/guard" className="mt-6 rounded-lg bg-guard px-4 py-2.5 text-sm font-semibold text-white hover:bg-guard-dark">
        Try again
      </a>
    </main>
  );
}
