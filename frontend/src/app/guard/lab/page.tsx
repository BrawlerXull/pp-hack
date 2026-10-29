"use client";

import { Bug, CloudOff, Database, Droplets, EyeOff, Gavel, Loader2, Moon, PartyPopper, Play, RotateCcw, ShieldAlert, ShoppingBag, Square, UserX, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Badge, Button, Card, Empty, PageHeader } from "@/components/guard/ui";
import { api, type RunContext, type Scenario, type SimRun, type Summary } from "@/lib/api";
import { OUTCOME_META, SKU_NAME, timeAgo } from "@/lib/format";

const ABOUT: Record<string, { title: string; text: string; icon: LucideIcon; kind: "attack" | "control" | "ops" }> = {
  card_testing: { title: "Card-testing attack", text: "A bot farm hammers the cheapest item with a new card each time. Each run draws its own attacker: 2-7 devices, pace, decline mix.", icon: Bug, kind: "attack" },
  account_testing: { title: "Stolen-account testing", text: "Stolen PayPal logins (wallet): most orders never get approved, many payers per device, cheapest item.", icon: UserX, kind: "attack" },
  slow_drip: { title: "Slow drip", text: "One attempt every 7-20 s: an attacker trying to stay under the radar.", icon: Droplets, kind: "attack" },
  flash_sale: { title: "Flash sale", text: "Real customers after an announced sale: returning shoppers, varied baskets, few declines.", icon: PartyPopper, kind: "control" },
  paypal_outage: { title: "PayPal outage", text: "Ordinary customers while PayPal returns errors. Not an attack.", icon: CloudOff, kind: "ops" },
  normal_night: { title: "Normal night", text: "A few ordinary orders.", icon: Moon, kind: "control" },
  mixed_traffic: { title: "Busy evening", text: "A busy ordinary evening: returning and new shoppers, a few honest second-card retries.", icon: ShoppingBag, kind: "control" },
  stealth_bot: { title: "Stealth bot", text: "A careful card tester: new device every attempt, one try every 1-5 minutes (slow; caught by the slow-decline trigger).", icon: EyeOff, kind: "attack" },
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
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 5000 });
  const gs = summary?.guard_state;
  const defencesOn = Boolean(summary?.open_incident || gs?.mode === "sca_all" || gs?.held_skus.length || gs?.blocked_devices);
  const [mode, setMode] = useState<"offline" | "live" | "observer">("offline");
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

  const reset = () => act("reset", () => api("/demo/reset", { method: "POST" }), "Demo reset (history kept).");

  return (
    <>
      <PageHeader
        title="Demo lab"
        subtitle={
          <>
            Run labelled simulations against the real pipeline. <b className="text-ink-1">Offline</b> synthesises PayPal&apos;s answer (rows tagged SIM, no PayPal call).{" "}
            <b className="text-ink-1">Live</b> sends real PayPal sandbox orders with sandbox test cards (needs card processing enabled on the sandbox app).{" "}
            <b className="text-ink-1">Webhooks only</b> is observer mode: the attack arrives as the PayPal webhooks of a checkout that never calls the guard (a hosted
            button or plugin), so you can see what a shop gets with no code change: detection, alerts and refunds for approval, but no blocking or 3-D Secure.
          </>
        }
      />

      {defencesOn && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-amber-400/20 bg-amber-400/[0.07] px-4 py-3 text-sm text-amber-100">
          <ShieldAlert className="size-5 shrink-0 text-amber-300" />
          <p className="min-w-0 flex-1">
            <b>Defences from an earlier attack are still on</b>
            {summary?.open_incident && (
              <>
                {" "}(
                <Link href={`/guard/incidents/${summary.open_incident.id}`} className="underline">
                  open incident
                </Link>
                )
              </>
            )}
            : {describeDefences({ sca_all: gs?.mode === "sca_all", held_skus: gs?.held_skus, blocked_devices: gs?.blocked_devices })}. A new attack
            will be stopped before it reaches PayPal. Reset the demo to watch an attack from the start.
          </p>
          <Button variant="secondary" size="sm" disabled={!!busy} onClick={reset}>
            <RotateCcw className="size-3.5" /> Reset demo
          </Button>
        </div>
      )}

      <Card
        title="Scenarios"
        action={
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <div className="flex rounded-lg bg-white/[0.05] p-0.5 ring-1 ring-white/10" role="group" aria-label="PayPal mode">
              {(["offline", "live", "observer"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  aria-pressed={mode === m}
                  className={`rounded-md px-3 py-1 text-xs font-medium ${mode === m ? "bg-white/[0.12] text-ink-1" : "text-ink-2 hover:text-ink-1"}`}
                >
                  {m === "offline" ? "Offline" : m === "live" ? "Live sandbox" : "Webhooks only"}
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
          <Button variant="secondary" size="sm" disabled={!!busy} onClick={reset}>
            <RotateCcw className="size-3.5" /> Reset demo
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!!busy}
            title="A simulated chargeback on an attack payment: see the dispute assistant on the War Room"
            onClick={() => act("dispute", () => api("/demo/dispute", { method: "POST" }), "Chargeback simulated: the dispute assistant opens a case in a few seconds.")}
          >
            <Gavel className="size-3.5" /> Simulate a chargeback
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
                <li key={r.run_id} className="py-3 text-sm">
                  <div className="flex flex-wrap items-center gap-3">
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
                  </div>
                  <RunNote run={r} />
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}

function describeDefences(c: RunContext): string {
  const parts: string[] = [];
  if (c.sca_all) parts.push("3-D Secure on every card");
  if (c.held_skus?.length) parts.push(`${c.held_skus.map((s) => SKU_NAME[s] ?? s).join(", ")} on hold for new shoppers`);
  if (c.blocked_devices) parts.push(`${c.blocked_devices} device${c.blocked_devices === 1 ? "" : "s"} blocked`);
  return parts.length ? parts.join(", ") : "an incident is open";
}

/** One line under each run: its seed (replayable) and why its numbers look the way they do. */
function RunNote({ run: r }: { run: SimRun }) {
  const ctx = r.context ?? {};
  const allStopped = r.done > 0 && r.reached_paypal === 0;
  return (
    <p className="mt-1 text-xs text-ink-3">
      {r.seed !== null && <span className="font-mono">seed {r.seed}</span>}
      {r.done > 0 && (
        <>
          {" · "}
          {allStopped
            ? `all ${r.done} stopped by the guard before PayPal`
            : `${r.reached_paypal} reached ${r.mode === "live" ? "PayPal" : "the (simulated) PayPal step"}, ${r.stopped_by_guard} stopped by the guard`}
        </>
      )}
      {ctx.defences_active && (
        <span className="text-amber-300">
          {" · "}started with defences already on ({describeDefences(ctx)})
        </span>
      )}
      {r.mode === "live" && allStopped && <span>{" · "}no PayPal calls were made</span>}
    </p>
  );
}
