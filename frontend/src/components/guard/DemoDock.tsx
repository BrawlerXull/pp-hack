"use client";

import { Bug, FlaskConical, Loader2, PartyPopper, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, type SimRun } from "@/lib/api";

const RUN_LABEL: Record<string, string> = {
  card_testing: "Card-testing attack",
  flash_sale: "Flash sale",
  account_testing: "Account testing",
  slow_drip: "Slow drip",
  paypal_outage: "PayPal outage",
  normal_night: "Normal night",
  mixed_traffic: "Busy evening",
  stealth_bot: "Stealth bot",
};

/**
 * The demo controls, in the sidebar of every dashboard page (never floating over content). Everything they
 * create is labelled SIM in the data. More scenarios and modes live in the Demo lab.
 */
export function DemoDock() {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const { data: runs } = useSWR<SimRun[]>("/demo/runs", { refreshInterval: (latest) => (latest?.some((r) => !r.finished_at) ? 1500 : 10_000) });
  const active = runs?.find((r) => !r.finished_at);

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
  const flashSale = () => api("/demo/run/flash_sale", { method: "POST", json: { mode: "offline", count: 60, speed: 1.5, announce_sale: true } });
  const pct = active && active.total ? Math.round((active.done / active.total) * 100) : 0;
  const disabled = !!busy || !!active;
  const item = "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <section aria-labelledby="demo-controls-title" className="rounded-xl border border-line bg-gray-50 p-2">
      <div className="flex items-center justify-between px-2 pt-1 pb-1.5">
        <h2 id="demo-controls-title" className="flex items-center gap-1.5 text-xs font-semibold text-ink-2">
          <FlaskConical className="size-3.5" aria-hidden /> Simulate
        </h2>
        <span className="rounded bg-white px-1.5 py-0.5 text-[11px] font-semibold text-ink-3 ring-1 ring-line" title="Simulated traffic, labelled SIM everywhere. Offline: no PayPal call.">
          SIM
        </span>
      </div>
      <button className={`${item} text-red-700 hover:bg-red-50`} disabled={disabled} onClick={() => run("attack", attack, "Attack started.")}>
        {busy === "attack" ? <Loader2 className="size-4 animate-spin" /> : <Bug className="size-4" aria-hidden />} Card-testing attack
      </button>
      <button className={`${item} text-ink-1 hover:bg-white`} disabled={disabled} onClick={() => run("sale", flashSale, "Flash sale started.")}>
        {busy === "sale" ? <Loader2 className="size-4 animate-spin" /> : <PartyPopper className="size-4 text-ink-3" aria-hidden />} Flash sale
      </button>
      <button className={`${item} text-ink-1 hover:bg-white`} disabled={!!busy} onClick={() => run("reset", () => api("/demo/reset", { method: "POST" }), "Demo reset.")}>
        {busy === "reset" ? <Loader2 className="size-4 animate-spin" /> : <RotateCcw className="size-4 text-ink-3" aria-hidden />} Reset demo
      </button>
      <div className="px-3 pt-1.5 pb-1 text-xs text-ink-3" aria-live="polite">
        {active ? (
          <span className="block">
            <span className="flex justify-between">
              <span className="font-medium text-ink-2">{RUN_LABEL[active.scenario] ?? active.scenario}</span>
              <span className="tabular-nums">
                {active.done}/{active.total}
              </span>
            </span>
            <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-gray-200">
              <span className="block h-full rounded-full bg-guard transition-all" style={{ width: `${pct}%` }} />
            </span>
          </span>
        ) : note ? (
          note
        ) : (
          <Link href="/guard/lab" className="underline-offset-2 hover:text-ink-1 hover:underline">
            More scenarios in the Demo lab
          </Link>
        )}
      </div>
    </section>
  );
}
