"use client";

import { Bug, Loader2, PartyPopper, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useSWRConfig } from "swr";
import { api } from "@/lib/api";
import { Button } from "./ui";

/** One-click demo controls. Everything they create is tagged as simulated in the data and the UI. */
export function QuickRun() {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setNote(null);
    try {
      await fn();
      setNote(label === "reset" ? "Demo reset." : "Running. Watch the War Room.");
      mutate((k) => typeof k === "string" && (k.startsWith("/dashboard") || k.startsWith("/demo")));
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(null);
    }
  }

  const attack = () =>
    api("/demo/run/card_testing", { method: "POST", json: { mode: "offline", count: 120, speed: 3 } });

  // The backend announces the 2-hour sale window itself, and only if the run is accepted (no 409 left over).
  const flashSale = () =>
    api("/demo/run/flash_sale", { method: "POST", json: { mode: "offline", count: 60, speed: 3, announce_sale: true } });

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="danger" disabled={!!busy} onClick={() => run("attack", attack)}>
        {busy === "attack" ? <Loader2 className="size-4 animate-spin" /> : <Bug className="size-4" />} Simulate card-testing attack
      </Button>
      <Button variant="secondary" disabled={!!busy} onClick={() => run("sale", flashSale)}>
        {busy === "sale" ? <Loader2 className="size-4 animate-spin" /> : <PartyPopper className="size-4" />} Simulate flash sale
      </Button>
      <Button variant="ghost" disabled={!!busy} onClick={() => run("reset", () => api("/demo/reset", { method: "POST" }))}>
        <RotateCcw className="size-4" /> Reset
      </Button>
      {note && <span className="text-xs text-slate-500">{note}</span>}
      <span className="w-full text-[11px] text-slate-400">
        Simulated attacker (labelled SIM). Offline mode doesn&apos;t call PayPal; switch to live in the Demo lab.
      </span>
    </div>
  );
}
