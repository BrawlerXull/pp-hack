"use client";

import { Database, Loader2, Play, RotateCcw, Square } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Badge, Button, Card, Empty } from "@/components/guard/ui";
import { api, type Scenario, type SimRun } from "@/lib/api";
import { OUTCOME_META, timeAgo } from "@/lib/format";

const ABOUT: Record<string, string> = {
  card_testing: "Bot farm hammering the cheapest item with a new card each time, ~70% declined.",
  account_testing: "Stolen PayPal accounts (wallet), many unique payers, cheapest item.",
  slow_drip: "One attempt every 12 s: an attacker trying to stay under the radar.",
  flash_sale: "Real customers after an announced sale: returning shoppers, varied baskets, few declines.",
  paypal_outage: "Normal customers while PayPal returns errors: not an attack.",
  normal_night: "A few ordinary orders.",
};

export default function LabPage() {
  const { data: scenarios } = useSWR<Scenario[]>("/demo/scenarios");
  const { data: runs } = useSWR<SimRun[]>("/demo/runs", { refreshInterval: 2000 });
  const [mode, setMode] = useState<"offline" | "live">("offline");
  const [speed, setSpeed] = useState(3);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function act(label: string, fn: () => Promise<unknown>, done?: string) {
    setBusy(label);
    setMessage(null);
    try {
      const res = await fn();
      setMessage(done ?? (typeof res === "object" ? JSON.stringify(res) : String(res)));
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Demo lab</h1>
        <p className="mt-1 text-sm text-slate-600">
          Run labelled simulations against the real pipeline. <b>Offline</b> synthesises PayPal&apos;s answer (rows marked “SIM”, no PayPal call).{" "}
          <b>Live</b> sends real PayPal sandbox orders with sandbox test cards (needs card processing enabled on the sandbox app).
        </p>
      </div>

      <Card
        title="Scenarios"
        action={
          <div className="flex items-center gap-3 text-sm">
            <div className="flex rounded-lg bg-slate-100 p-0.5">
              {(["offline", "live"] as const).map((m) => (
                <button key={m} onClick={() => setMode(m)} className={`rounded-md px-3 py-1 ${mode === m ? "bg-white shadow-sm" : "text-slate-600"}`}>
                  {m === "offline" ? "Offline" : "Live sandbox"}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-slate-600">
              Speed
              <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="rounded-md border border-slate-300 px-2 py-1">
                {[1, 3, 5, 10].map((s) => <option key={s} value={s}>{s}×</option>)}
              </select>
            </label>
          </div>
        }
      >
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {scenarios?.map((s) => {
            const disabled = mode === "live" && !s.live_supported;
            return (
              <div key={s.name} className="flex flex-col rounded-lg border border-slate-200 p-4">
                <p className="font-medium text-slate-800">{s.name.replace(/_/g, " ")}</p>
                <p className="mt-1 flex-1 text-sm text-slate-600">{ABOUT[s.name]}</p>
                <Button
                  className="mt-3 self-start"
                  variant={s.name === "card_testing" || s.name === "account_testing" || s.name === "slow_drip" ? "danger" : "secondary"}
                  disabled={disabled || !!busy}
                  title={disabled ? "Offline only" : undefined}
                  onClick={() => act(s.name, () => api(`/demo/run/${s.name}`, { method: "POST", json: { mode, speed } }), `Started ${s.name} (${mode}).`)}
                >
                  {busy === s.name ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />} {disabled ? "Offline only" : "Run"}
                </Button>
              </div>
            );
          })}
        </div>
        <div className="mt-5 flex flex-wrap gap-2 border-t border-slate-100 pt-4">
          <Button variant="secondary" disabled={!!busy} onClick={() => act("seed", () => api("/demo/seed?days=28", { method: "POST" }), "Seeded 28 days of normal history.")}>
            <Database className="size-4" /> Seed 28 days of normal history
          </Button>
          <Button variant="secondary" disabled={!!busy} onClick={() => act("reset", () => api("/demo/reset", { method: "POST" }), "Demo reset (history kept).")}>
            <RotateCcw className="size-4" /> Reset demo
          </Button>
          {message && <span className="self-center text-xs text-slate-500">{message}</span>}
        </div>
      </Card>

      <Card title="Runs">
        {!runs?.length ? (
          <Empty>No runs yet.</Empty>
        ) : (
          <ul className="-my-2 divide-y divide-slate-100">
            {runs.map((r) => (
              <li key={r.run_id} className="flex flex-wrap items-center gap-3 py-3 text-sm">
                <span className="w-36 font-medium text-slate-800">{r.scenario.replace(/_/g, " ")}</span>
                <Badge tone={r.mode === "live" ? "info" : "neutral"}>{r.mode}</Badge>
                <div className="h-1.5 w-32 overflow-hidden rounded-full bg-slate-100">
                  <div className="h-full bg-guard" style={{ width: `${(r.done / r.total) * 100}%` }} />
                </div>
                <span className="tabular-nums text-slate-600">{r.done}/{r.total}</span>
                <span className="flex flex-wrap gap-2 text-xs text-slate-600">
                  {Object.entries(r.outcomes).map(([o, n]) => (
                    <span key={o} className="inline-flex items-center gap-1">
                      <span className="size-2 rounded-full" style={{ background: OUTCOME_META[o as keyof typeof OUTCOME_META]?.color }} />
                      {OUTCOME_META[o as keyof typeof OUTCOME_META]?.label ?? o} {n}
                    </span>
                  ))}
                </span>
                <span className="ml-auto text-xs text-slate-500">{r.error ? <span className="text-red-600">{r.error}</span> : r.finished_at ? `done ${timeAgo(r.finished_at)}` : "running"}</span>
                {!r.finished_at && (
                  <Button variant="ghost" className="px-2 py-1" onClick={() => api(`/demo/runs/${r.run_id}/stop`, { method: "POST" })}>
                    <Square className="size-3.5" />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
