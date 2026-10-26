"use client";

import { Bug, ChevronDown, FlaskConical, Loader2, PartyPopper, RotateCcw } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, type SimRun } from "@/lib/api";
import { Button } from "./ui";

const COLLAPSED_KEY = "cg_demo_dock_collapsed";

const RUN_LABEL: Record<string, string> = {
  card_testing: "Card-testing attack",
  flash_sale: "Flash sale",
  account_testing: "Account testing",
  slow_drip: "Slow drip",
  paypal_outage: "PayPal outage",
  normal_night: "Normal night",
};

/** One-click demo controls on every dashboard page. Everything they create is tagged SIM in the data. */
export function DemoDock() {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // Only rendered client-side (after /auth/me answers), so reading storage on first render is safe.
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSED_KEY) === "1";
    } catch {
      return false; // storage unavailable: start expanded
    }
  });
  const { data: runs } = useSWR<SimRun[]>("/demo/runs", { refreshInterval: (latest) => (latest?.some((r) => !r.finished_at) ? 1500 : 10_000) });
  const active = runs?.find((r) => !r.finished_at);

  function toggle() {
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !c;
    });
  }

  async function run(label: string, fn: () => Promise<unknown>, done: string) {
    setBusy(label);
    setNote(null);
    try {
      await fn();
      setNote(done);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(null);
      mutate((k) => typeof k === "string" && (k.startsWith("/dashboard") || k.startsWith("/demo") || k === "/settings"));
    }
  }

  const attack = () => api("/demo/run/card_testing", { method: "POST", json: { mode: "offline", count: 150, speed: 1 } }); // ~2 min: spreads over a few chart bars

  // The backend announces the 2-hour sale window itself, and only if the run is accepted (409 while one is going).
  const flashSale = () =>
    api("/demo/run/flash_sale", { method: "POST", json: { mode: "offline", count: 60, speed: 1.5, announce_sale: true } });

  if (collapsed) {
    return (
      <button
        onClick={toggle}
        className="fixed right-5 bottom-5 z-40 flex items-center gap-2 rounded-full border border-violet-400/30 bg-night-850/90 px-3.5 py-2 text-xs font-medium text-violet-200 shadow-2xl backdrop-blur hover:bg-night-750"
        aria-label="Show demo controls"
      >
        <FlaskConical className="size-3.5" /> Demo
        {active && <Loader2 className="size-3.5 animate-spin" />}
      </button>
    );
  }

  const pct = active && active.total ? Math.round((active.done / active.total) * 100) : 0;

  return (
    <div className="fixed inset-x-3 bottom-4 z-40 mx-auto max-w-fit rounded-2xl border border-white/10 bg-night-850/90 p-2 shadow-[0_20px_60px_-15px_rgb(0_0_0/0.8)] backdrop-blur-md md:left-64">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 rounded-lg bg-violet-500/15 px-2.5 py-1.5 text-[11px] font-semibold tracking-wide text-violet-200 uppercase" title="Labelled simulation: a scripted attacker and crowd. Offline mode does not call PayPal.">
          <FlaskConical className="size-3.5" /> Simulation
        </span>
        <Button size="sm" variant="danger" disabled={!!busy || !!active} onClick={() => run("attack", attack, "Attack started. Watch the War Room.")}>
          {busy === "attack" ? <Loader2 className="size-3.5 animate-spin" /> : <Bug className="size-3.5" />} Card-testing attack
        </Button>
        <Button size="sm" variant="secondary" disabled={!!busy || !!active} onClick={() => run("sale", flashSale, "Flash sale started.")}>
          {busy === "sale" ? <Loader2 className="size-3.5 animate-spin" /> : <PartyPopper className="size-3.5" />} Flash sale
        </Button>
        <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => run("reset", () => api("/demo/reset", { method: "POST" }), "Demo reset.")}>
          {busy === "reset" ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCcw className="size-3.5" />} Reset
        </Button>
        {active ? (
          <span className="flex items-center gap-2 px-1 text-xs text-ink-2" aria-live="polite">
            <span className="font-medium text-ink-1">{RUN_LABEL[active.scenario] ?? active.scenario}</span>
            <span className="h-1.5 w-20 overflow-hidden rounded-full bg-white/10">
              <span className="block h-full rounded-full bg-violet-400 transition-all" style={{ width: `${pct}%` }} />
            </span>
            <span className="tabular-nums">{active.done}/{active.total}</span>
          </span>
        ) : (
          note && <span className="px-1 text-xs text-ink-3" aria-live="polite">{note}</span>
        )}
        <button onClick={toggle} className="rounded-lg p-1.5 text-ink-3 hover:bg-white/[0.06] hover:text-ink-1" aria-label="Hide demo controls">
          <ChevronDown className="size-4" />
        </button>
      </div>
    </div>
  );
}
