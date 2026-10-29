"use client";

import { ArrowRight, Check, Eye, FileText, Gavel, Loader2, PartyPopper, Power, Sparkles, type LucideIcon } from "lucide-react";
import Link from "next/link";
import type { IncidentDetail, Summary } from "@/lib/api";
import { CLASSIFICATION_LABEL, STATUS_COPY, TRIGGER_LABEL, duration, hhmm, money, timeAgo, type Tone } from "@/lib/format";

// Most telling first, so the Watch step names the strongest signals.
const TRIGGER_ORDER = ["fraud_declines", "cheap_sku_card_cycling", "decline_spike", "processor_errors", "velocity_spike", "sku_concentration", "blocked_activity"];
const rank = (t: string) => (TRIGGER_ORDER.includes(t) ? TRIGGER_ORDER.indexOf(t) : TRIGGER_ORDER.length);

function triggerSummary(triggers: string[]): string {
  if (!triggers.length) return "unusual traffic";
  const sorted = [...triggers].sort((a, b) => rank(a) - rank(b));
  const shown = sorted.slice(0, 2).map((t, i) => {
    const label = TRIGGER_LABEL[t] ?? t.replace(/_/g, " ");
    return i === 0 || label.startsWith("PayPal") ? label : label.charAt(0).toLowerCase() + label.slice(1);
  });
  return sorted.length > 2 ? `${shown.join(", ")} +${sorted.length - 2} more` : shown.join(", ");
}
import { Badge } from "./ui";

// One status colour per state, used only on the badge and the left rule: the rest of the banner stays neutral.
const RULE: Record<Tone, string> = {
  good: "border-l-emerald-500",
  bad: "border-l-red-600",
  warn: "border-l-amber-500",
  info: "border-l-blue-600",
  neutral: "border-l-gray-300",
};
const STATE_LABEL: Record<string, string> = {
  quiet: "All quiet",
  under_attack: "Under attack",
  busy_genuine_rush: "Busy: real customers",
  paypal_issue: "PayPal issue",
  investigating: "Investigating",
  recovering: "Recovering",
};

type StepState = "idle" | "active" | "done";
type Step = { name: string; icon: LucideIcon; state: StepState; title: string; detail: string };

export function defencesSummary(incident: IncidentDetail): string[] {
  const done = incident.actions.filter((a) => a.status === "executed");
  const parts: string[] = [];
  if (done.some((a) => a.type === "SCA_ON")) parts.push("3-D Secure on");
  const blocked = done.filter((a) => a.type === "BLOCK_FP").reduce((n, a) => n + ((a.params.fingerprints as string[] | undefined)?.length ?? 0), 0);
  if (blocked) parts.push(`${blocked} device${blocked === 1 ? "" : "s"} blocked`);
  if (done.some((a) => a.type === "HOLD_SKU")) parts.push("item on hold");
  const refunds = done.filter((a) => a.type === "REFUND");
  if (refunds.length) parts.push(`${refunds.length} refunded (${money(refunds.reduce((s, a) => s + Number(a.params.amount ?? 0), 0))})`);
  const voids = done.filter((a) => a.type === "VOID");
  if (voids.length) parts.push(`${voids.length} held payment${voids.length === 1 ? "" : "s"} voided (${money(voids.reduce((s, a) => s + Number(a.params.amount ?? 0), 0))})`);
  if (done.some((a) => a.type === "RELAX" || a.type === "DEESCALATE")) parts.push("stood down");
  return parts;
}

function steps(summary: Summary, inc: IncidentDetail | null | undefined): Step[] {
  const s24 = summary.last_24h;
  const watch: Step = {
    name: "Watch",
    icon: Eye,
    state: "done",
    title: inc ? `Flagged: ${triggerSummary(inc.triggers)}` : `${s24.attempts} checkouts in 24 h`,
    detail: inc
      ? inc.detected_in_s != null
        ? `Detected ${inc.detected_in_s} s after the burst began`
        : `Detector opened an incident ${timeAgo(inc.opened_at)}`
      : "Every checkout passes the guard before PayPal",
  };
  if (!inc) {
    return [
      { ...watch, state: "active" },
      { name: "Judge", icon: Sparkles, state: "idle", title: "Scorecard on standby", detail: "Runs when the detector flags something" },
      { name: "Act", icon: Gavel, state: "idle", title: "Defences ready", detail: "3-D Secure, blocks, refunds within your limits" },
      { name: "Explain", icon: FileText, state: "idle", title: "Nothing to report", detail: "Plain-English alert and morning report" },
    ];
  }

  const v = inc.verdict;
  const judge: Step = !v
    ? { name: "Judge", icon: Sparkles, state: "active", title: "Scoring the traffic…", detail: "Attack, real rush or PayPal outage?" }
    : {
        name: "Judge",
        icon: Sparkles,
        state: "done",
        title: `${CLASSIFICATION_LABEL[v.classification]} · ${Math.round(v.confidence * 100)}%`,
        detail: `Scorecard decided · ${inc.ai_available && inc.model_id ? `explained by ${inc.model_id}` : "explained by our template"}`,
      };

  const parts = defencesSummary(inc);
  const pending = inc.actions.filter((a) => (a.type === "REFUND" || a.type === "VOID") && a.status === "awaiting_approval").length;
  const harmless = v && (v.classification === "genuine_rush" || v.classification === "paypal_outage");
  const act: Step = parts.length
    ? { name: "Act", icon: Gavel, state: "done", title: parts.slice(0, 2).join(" · "), detail: parts.slice(2).join(" · ") || "Within your safety limits" }
    : pending
      ? { name: "Act", icon: Gavel, state: "active", title: `${pending} payment${pending === 1 ? "" : "s"} need your decision`, detail: "Refunds or voids above your limits" }
      : harmless
        ? { name: "Act", icon: Gavel, state: "done", title: "No defences needed", detail: "Real customers keep paying normally" }
        : { name: "Act", icon: Gavel, state: v ? "active" : "idle", title: v ? "Applying policy…" : "Waiting for the verdict", detail: "Fixed rules decide, never the AI" };
  if (parts.length && pending) act.detail = `${pending} payment${pending === 1 ? "" : "s"} need your decision`;

  const explain: Step = inc.report?.alert_text
    ? { name: "Explain", icon: FileText, state: "done", title: inc.status === "closed" ? "Morning report ready" : "Owner alerted", detail: "One plain-English message, no jargon" }
    : { name: "Explain", icon: FileText, state: v ? "active" : "idle", title: "Writing the alert…", detail: "Numbers come from our records, not the AI" };

  return [watch, judge, act, explain];
}

export function StatusHero({ summary, incident }: { summary: Summary; incident?: IncidentDetail | null }) {
  const copy = STATUS_COPY[summary.status];
  const gs = summary.guard_state;
  const open = summary.open_incident;
  const pipeline = steps(summary, incident);
  const firstDefence = incident?.actions.find((a) => a.status === "executed" && a.type !== "NOTIFY");
  const contained = incident && firstDefence ? new Date(firstDefence.created_at).getTime() - new Date(incident.opened_at).getTime() : null;
  const defences = [
    { label: "3-D Secure", value: gs.mode === "sca_all" ? "First-time cards" : "Normal", on: gs.mode === "sca_all" },
    { label: "Devices blocked", value: String(gs.blocked_devices), on: gs.blocked_devices > 0 },
    { label: "Items on hold", value: String(gs.held_skus.length), on: gs.held_skus.length > 0 },
  ];

  return (
    <section className={`panel border-l-4 ${RULE[copy.tone]}`} aria-labelledby="status-title">
      <div className="flex flex-col gap-5 p-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={copy.tone}>{STATE_LABEL[summary.status] ?? summary.status}</Badge>
            {open && <span className="text-[13px] text-ink-3">Incident open {timeAgo(open.opened_at).replace(" ago", "")}</span>}
            {summary.switches.kill_switch && (
              <Badge tone="bad">
                <Power className="size-3" aria-hidden /> Kill switch on: guard paused
              </Badge>
            )}
            {summary.sale_window && (
              <Badge tone="info">
                <PartyPopper className="size-3" aria-hidden /> Sale until {hhmm(summary.sale_window.end)}
              </Badge>
            )}
          </div>
          <h1 id="status-title" className="mt-2 text-2xl font-semibold tracking-tight text-ink-1">
            {copy.title}
          </h1>
          <p className="mt-1 max-w-3xl text-[15px] leading-relaxed text-ink-2">{open?.summary ?? copy.line}</p>
        </div>
        <div className="flex shrink-0 flex-col items-start gap-3 lg:items-end">
          <dl className="flex divide-x divide-line rounded-lg border border-line">
            {defences.map((d) => (
              <div key={d.label} className="px-4 py-2">
                <dt className="text-xs text-ink-3">{d.label}</dt>
                <dd className={`mt-0.5 text-sm font-semibold ${d.on ? "text-emerald-700" : "text-ink-1"}`}>{d.value}</dd>
              </div>
            ))}
          </dl>
          {incident && (
            <Link href={`/guard/incidents/${incident.id}`} className="inline-flex items-center gap-1 text-[13px] font-medium text-guard hover:underline">
              Open incident report <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          )}
        </div>
      </div>

      <div className="border-t border-line px-5 py-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-[13px] font-medium text-ink-2">
            {incident ? (incident.status === "open" ? "How Checkout Guard is responding" : `Last incident · closed ${timeAgo(incident.closed_at!)}`) : "How Checkout Guard responds"}
          </h2>
          {contained !== null && <span className="text-[13px] text-ink-3">First defence {duration(contained)} after detection</span>}
        </div>
        <ol className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {pipeline.map((s, i) => (
            <PipelineStep key={s.name} step={s} index={i} />
          ))}
        </ol>
      </div>
    </section>
  );
}

function PipelineStep({ step, index }: { step: Step; index: number }) {
  const marker =
    step.state === "done"
      ? "bg-emerald-600 text-white"
      : step.state === "active"
        ? "bg-guard text-white"
        : "bg-white text-ink-3 ring-1 ring-line-strong";
  return (
    <li className="flex gap-3">
      <span className={`flex size-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${marker}`} aria-hidden>
        {step.state === "done" ? <Check className="size-4" /> : step.state === "active" && step.name !== "Watch" ? <Loader2 className="size-4 animate-spin" /> : index + 1}
      </span>
      <span className="min-w-0">
        <span className="block text-xs font-medium text-ink-3">
          {step.name}
          <span className="sr-only"> ({step.state === "done" ? "done" : step.state === "active" ? "in progress" : "waiting"})</span>
        </span>
        <span className={`block text-sm leading-snug font-medium ${step.state === "idle" ? "text-ink-2" : "text-ink-1"}`}>{step.title}</span>
        <span className="mt-0.5 block text-[13px] leading-snug text-ink-3">{step.detail}</span>
      </span>
    </li>
  );
}
