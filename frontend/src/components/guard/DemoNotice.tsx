"use client";

import { RotateCcw, Users } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { api, type DemoActivity, type Summary } from "@/lib/api";
import { timeAgo } from "@/lib/format";

// What another visitor did, in a few words (everyone shares the owner login on the hosted demo).
const DID: Record<string, (d: DemoActivity["data"]) => string> = {
  "demo.run_started": (d) => `ran a ${String(d.scenario ?? "demo").replace(/_/g, " ")} simulation`,
  "demo.reset": () => "reset the demo",
  "demo.sale_announced": () => "announced a sale",
  "settings.updated": () => "changed the settings",
  "settings.switches": () => "flipped the autopilot or kill switch",
  "guard.reset": () => "switched the defences off",
  "incident.closed_by_owner": () => "stood the guard down",
  "device.unblocked": () => "unblocked a device",
  "refund.executed": () => "approved a refund",
  "refund.rejected": () => "kept a payment instead of refunding it",
};

/** Shared hosted demo: says when the state on screen comes from someone else, or from the idle stand-down. */
export function DemoNotice() {
  const { data } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState(false);
  const a = data?.demo_activity;
  if (!a) return null;

  const reset = async () => {
    setBusy(true);
    try {
      await api("/demo/reset", { method: "POST" });
    } finally {
      setBusy(false);
      mutate((k) => typeof k === "string" && (k.startsWith("/dashboard") || k.startsWith("/demo") || k.startsWith("/studio")));
    }
  };

  const text =
    a.kind === "idle_restore"
      ? `The demo sat idle, so it was stood down ${timeAgo(a.at)}: ${((a.data.restored as string[] | undefined) ?? []).join(", ")}. Run a simulation to start fresh.`
      : `Another visitor ${(DID[a.event] ?? (() => "changed the demo"))(a.data)} ${timeAgo(a.at)}. This demo is shared, so what you see may include their changes.`;

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900 sm:px-6 lg:px-8" role="status">
      <Users className="size-4 shrink-0 text-amber-700" />
      <p className="min-w-0 flex-1">{text}</p>
      {a.kind === "other_visitor" && (
        <button
          onClick={reset}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 font-medium text-amber-700 ring-1 ring-amber-200 hover:bg-amber-50 disabled:opacity-50"
        >
          <RotateCcw className="size-3.5" /> Reset demo
        </button>
      )}
    </div>
  );
}
