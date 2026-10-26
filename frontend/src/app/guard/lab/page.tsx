"use client";

import { Bug, CloudOff, Database, Droplets, Loader2, Moon, PartyPopper, Play, RotateCcw, Square, UserX, type LucideIcon } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Badge, Button, Card, Empty, PageHeader } from "@/components/guard/ui";
import { api, type Scenario, type SimRun } from "@/lib/api";
import { OUTCOME_META, timeAgo } from "@/lib/format";

const ABOUT: Record<string, { title: string; text: string; icon: LucideIcon; kind: "attack" | "control" | "ops" }> = {
  card_testing: { title: "Card-testing attack", text: "A bot farm hammers the cheapest item with a new card each time; about 70% are declined.", icon: Bug, kind: "attack" },
  account_testing: { title: "Stolen-account testing", text: "Stolen PayPal accounts (wallet), many unique payers, cheapest item.", icon: UserX, kind: "attack" },
  slow_drip: { title: "Slow drip", text: "One attempt every 12 s: an attacker trying to stay under the radar.", icon: Droplets, kind: "attack" },
  flash_sale: { title: "Flash sale", text: "Real customers after an announced sale: returning shoppers, varied baskets, few declines.", icon: PartyPopper, kind: "control" },
  paypal_outage: { title: "PayPal outage", text: "Ordinary customers while PayPal returns errors. Not an attack.", icon: CloudOff, kind: "ops" },
  normal_night: { title: "Normal night", text: "A few ordinary orders.", icon: Moon, kind: "control" },
};

const KIND = {
  attack: { badge: "bad", label: "Attack", tile: "bg-red-400/10 text-red-300" },
  control: { badge: "info", label: "Should not trigger", tile: "bg-sky-400/10 text-sky-300" },
  ops: { badge: "warn", label: "Not fraud", tile: "bg-amber-400/10 text-amber-300" },
} as const;

export default function LabPage() {
  const { mutate } = useSWRConfig();
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
      mutate((k) => typeof k === "string" && (k.startsWith("/dashboard") || k.startsWith("/demo")));
    }
  }

  return (
    <>
      <PageHeader
        title="Demo lab"
        subtitle={
          <>
            Run labelled simulations against the real pipeline. <b className="text-ink-1">Offline</b> synthesises PayPal&apos;s answer (rows tagged SIM, no PayPal call).{" "}
            <b className="text-ink-1">Live</b> sends real PayPal sandbox orders with sandbox test cards (needs card processing enabled on the sandbox app).
          </>
        }
      />

      <Card
        title="Scenarios"
        action={
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <div className="flex rounded-lg bg-white/[0.05] p-0.5 ring-1 ring-white/10" role="group" aria-label="PayPal mode">
              {(["offline", "live"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  aria-pressed={mode === m}
                  className={`rounded-md px-3 py-1 text-xs font-medium ${mode === m ? "bg-white/[0.12] text-ink-1" : "text-ink-2 hover:text-ink-1"}`}
                >
                  {m === "offline" ? "Offline" : "Live sandbox"}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-xs text-ink-2">
              Speed
              <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} className="rounded-md border border-white/10 bg-night-750 px-2 py-1 text-ink-1">
                {[1, 3, 5, 10].map((s) => (
                  <option key={s} value={s}>
                    {s}×
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
      >
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {scenarios?.map((s) => {
            const about = ABOUT[s.name] ?? { title: s.name.replace(/_/g, " "), text: "", icon: Play, kind: "control" as const };
            const kind = KIND[about.kind];
            const Icon = about.icon;
            const disabled = mode === "live" && !s.live_supported;
            return (
              <div key={s.name} className="flex flex-col rounded-xl border border-white/[0.07] bg-white/[0.02] p-4">
                <div className="flex items-start justify-between gap-2">
                  <span className={`flex size-9 items-center justify-center rounded-lg ${kind.tile}`}>
                    <Icon className="size-4" />
                  </span>
                  <Badge tone={kind.badge}>{kind.label}</Badge>
                </div>
                <p className="mt-3 font-medium text-ink-1">{about.title}</p>
                <p className="mt-1 flex-1 text-sm text-ink-2">{about.text}</p>
                <Button
                  className="mt-4 self-start"
                  size="sm"
                  variant={about.kind === "attack" ? "danger" : "secondary"}
                  disabled={disabled || !!busy}
                  title={disabled ? "Offline only" : undefined}
                  onClick={() => act(s.name, () => api(`/demo/run/${s.name}`, { method: "POST", json: { mode, speed } }), `Started ${about.title} (${mode}).`)}
                >
                  {busy === s.name ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />} {disabled ? "Offline only" : "Run"}
                </Button>
              </div>
            );
          })}
        </div>
        <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-4">
          <Button variant="secondary" size="sm" disabled={!!busy} onClick={() => act("seed", () => api("/demo/seed?days=28", { method: "POST" }), "Seeded 28 days of normal history.")}>
            <Database className="size-3.5" /> Seed 28 days of normal history
          </Button>
          <Button variant="secondary" size="sm" disabled={!!busy} onClick={() => act("reset", () => api("/demo/reset", { method: "POST" }), "Demo reset (history kept).")}>
            <RotateCcw className="size-3.5" /> Reset demo
          </Button>
          {message && <span className="text-xs text-ink-3" aria-live="polite">{message}</span>}
        </div>
      </Card>

      <Card title="Runs">
        {!runs?.length ? (
          <Empty>No runs yet.</Empty>
        ) : (
          <ul className="-my-2 divide-y divide-white/[0.05]">
            {runs.map((r) => {
              const pct = r.total ? (r.done / r.total) * 100 : 0;
              return (
                <li key={r.run_id} className="flex flex-wrap items-center gap-3 py-3 text-sm">
                  <span className="w-40 font-medium text-ink-1">{ABOUT[r.scenario]?.title ?? r.scenario.replace(/_/g, " ")}</span>
                  <Badge tone={r.mode === "live" ? "info" : "neutral"}>{r.mode}</Badge>
                  <div className="h-1.5 w-32 overflow-hidden rounded-full bg-white/[0.08]">
                    <div className="h-full rounded-full bg-violet-400" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="text-ink-2 tabular-nums">
                    {r.done}/{r.total}
                  </span>
                  <span className="flex flex-wrap gap-3 text-xs text-ink-2">
                    {Object.entries(r.outcomes).map(([o, n]) => (
                      <span key={o} className="inline-flex items-center gap-1.5">
                        <span className="size-2 rounded-full" style={{ background: OUTCOME_META[o as keyof typeof OUTCOME_META]?.color }} />
                        {OUTCOME_META[o as keyof typeof OUTCOME_META]?.label ?? o} {n}
                      </span>
                    ))}
                  </span>
                  <span className="ml-auto text-xs text-ink-3">{r.error ? <span className="text-red-300">{r.error}</span> : r.finished_at ? `done ${timeAgo(r.finished_at)}` : "running"}</span>
                  {!r.finished_at && (
                    <Button variant="ghost" size="sm" aria-label={`Stop ${r.scenario}`} onClick={() => act(`stop-${r.run_id}`, () => api(`/demo/runs/${r.run_id}/stop`, { method: "POST" }), "Stopped.")}>
                      <Square className="size-3.5" />
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}
