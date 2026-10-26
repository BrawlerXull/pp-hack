"use client";

import { Ban, Eye, FileText, Gavel, Loader2, PauseCircle, ShieldAlert, ShieldCheck, Sparkles, TriangleAlert, Users, Zap, type LucideIcon } from "lucide-react";
import type { IncidentDetail, Summary } from "@/lib/api";
import { CLASSIFICATION_LABEL, STATUS_COPY, TRIGGER_LABEL, duration, money, timeAgo, type Tone } from "@/lib/format";

// Most telling first, so the Watch step names the strongest signals.
const TRIGGER_ORDER = ["fraud_declines", "cheap_sku_card_cycling", "decline_spike", "processor_errors", "velocity_spike", "sku_concentration", "blocked_activity"];
const rank = (t: string) => (TRIGGER_ORDER.includes(t) ? TRIGGER_ORDER.indexOf(t) : TRIGGER_ORDER.length);

function triggerSummary(triggers: string[]): string {
  if (!triggers.length) return "unusual traffic";
  const sorted = [...triggers].sort((a, b) => rank(a) - rank(b));
  const shown = sorted.slice(0, 2).map((t) => TRIGGER_LABEL[t] ?? t.replace(/_/g, " "));
  return sorted.length > 2 ? `${shown.join(", ")} +${sorted.length - 2} more` : shown.join(", ");
}
import { LiveDot } from "./ui";

const HERO: Record<Tone, { glow: string; tile: string; icon: LucideIcon; eyebrow: string }> = {
  good: { glow: "hero-glow-good", tile: "bg-emerald-400/12 text-emerald-300 ring-emerald-400/30", icon: ShieldCheck, eyebrow: "text-emerald-300" },
  bad: { glow: "hero-glow-critical", tile: "bg-red-500/15 text-red-300 ring-red-400/40 pulse-danger", icon: ShieldAlert, eyebrow: "text-red-300" },
  warn: { glow: "hero-glow-warn", tile: "bg-amber-400/12 text-amber-300 ring-amber-400/30", icon: TriangleAlert, eyebrow: "text-amber-300" },
  info: { glow: "hero-glow-info", tile: "bg-sky-400/12 text-sky-300 ring-sky-400/30", icon: Users, eyebrow: "text-sky-300" },
  neutral: { glow: "", tile: "bg-white/5 text-ink-2 ring-white/10", icon: ShieldCheck, eyebrow: "text-ink-2" },
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
    detail: inc ? `Detector opened an incident ${timeAgo(inc.opened_at)}` : "Every checkout passes the guard before PayPal",
  };
  if (!inc) {
    return [
      { ...watch, state: "active" },
      { name: "Judge", icon: Sparkles, state: "idle", title: "AI analyst on standby", detail: "Called only when something looks wrong" },
      { name: "Act", icon: Gavel, state: "idle", title: "Defences ready", detail: "3-D Secure, blocks, refunds within your limits" },
      { name: "Explain", icon: FileText, state: "idle", title: "Nothing to report", detail: "Plain-English alert and morning report" },
    ];
  }

  const v = inc.verdict;
  const judge: Step = !v
    ? { name: "Judge", icon: Sparkles, state: "active", title: "Gemini is analysing…", detail: "Attack, real rush or PayPal outage?" }
    : inc.ai_available
      ? { name: "Judge", icon: Sparkles, state: "done", title: `${CLASSIFICATION_LABEL[v.classification]} · ${Math.round(v.confidence * 100)}%`, detail: `Verdict from ${inc.model_id ?? "Gemini"}` }
      : { name: "Judge", icon: Sparkles, state: "done", title: "Fixed rules (AI unavailable)", detail: "Safe defaults applied" };

  const parts = defencesSummary(inc);
  const pending = inc.actions.filter((a) => a.type === "REFUND" && a.status === "awaiting_approval").length;
  const harmless = v && (v.classification === "genuine_rush" || v.classification === "paypal_outage");
  const act: Step = parts.length
    ? { name: "Act", icon: Gavel, state: "done", title: parts.slice(0, 2).join(" · "), detail: parts.slice(2).join(" · ") || "Within your safety limits" }
    : pending
      ? { name: "Act", icon: Gavel, state: "active", title: `${pending} refund${pending === 1 ? "" : "s"} await you`, detail: "Above your auto-refund limit" }
      : harmless
        ? { name: "Act", icon: Gavel, state: "done", title: "No defences needed", detail: "Real customers keep paying normally" }
        : { name: "Act", icon: Gavel, state: v ? "active" : "idle", title: v ? "Applying policy…" : "Waiting for the verdict", detail: "Fixed rules decide, never the AI" };
  if (parts.length && pending) act.detail = `${pending} refund${pending === 1 ? "" : "s"} await your approval`;

  const explain: Step = inc.report?.alert_text
    ? { name: "Explain", icon: FileText, state: "done", title: inc.status === "closed" ? "Morning report ready" : "Owner alerted", detail: "One plain-English message, no jargon" }
    : { name: "Explain", icon: FileText, state: v ? "active" : "idle", title: "Writing the alert…", detail: "Numbers come from our records, not the AI" };

  return [watch, judge, act, explain];
}

export function StatusHero({ summary, incident }: { summary: Summary; incident?: IncidentDetail | null }) {
  const copy = STATUS_COPY[summary.status];
  const style = HERO[copy.tone];
  const Icon = style.icon;
  const gs = summary.guard_state;
  const open = summary.open_incident;
  const pipeline = steps(summary, incident);
  const firstDefence = incident?.actions.find((a) => a.status === "executed" && a.type !== "NOTIFY");
  const contained = incident && firstDefence ? new Date(firstDefence.created_at).getTime() - new Date(incident.opened_at).getTime() : null;

  return (
    <section className={`panel relative overflow-hidden p-6 transition-shadow duration-700 ${style.glow}`} aria-live="polite">
      <div className="flex flex-col gap-6 xl:flex-row xl:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-4 sm:gap-5">
          <span className={`flex size-12 shrink-0 items-center justify-center rounded-2xl ring-1 sm:size-16 ${style.tile}`}>
            <Icon className="size-6 sm:size-8" />
          </span>
          <div className="min-w-0">
            <p className={`flex items-center gap-2 text-xs font-semibold tracking-wider uppercase ${style.eyebrow}`}>
              <LiveDot tone={copy.tone === "bad" ? "bad" : copy.tone === "good" ? "good" : "warn"} /> Wick &amp; Wax checkout
              {open && <span className="font-normal tracking-normal text-ink-3 normal-case">· incident open {timeAgo(open.opened_at).replace(" ago", "")}</span>}
            </p>
            <h1 className="mt-1 text-3xl font-semibold tracking-tight text-ink-1 sm:text-[2.1rem]">{copy.title}</h1>
            <p className="mt-1 max-w-2xl text-[15px] text-ink-2">{open?.summary ?? copy.line}</p>
          </div>
        </div>
        <dl className="grid shrink-0 grid-cols-3 gap-2 text-center xl:w-[400px]">
          <Defence icon={ShieldCheck} label="3-D Secure" value={gs.mode === "sca_all" ? "Every card" : "Normal"} on={gs.mode === "sca_all"} />
          <Defence icon={Ban} label="Devices blocked" value={String(gs.blocked_devices)} on={gs.blocked_devices > 0} />
          <Defence icon={PauseCircle} label="Items on hold" value={String(gs.held_skus.length)} on={gs.held_skus.length > 0} />
        </dl>
      </div>

      <div className="mt-6 border-t border-white/[0.06] pt-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-ink-3">
          <span className="font-medium tracking-wider uppercase">{incident ? (incident.status === "open" ? "This incident" : `Last incident · closed ${timeAgo(incident.closed_at!)}`) : "How Checkout Guard responds"}</span>
          {contained !== null && (
            <span className="flex items-center gap-1.5 rounded-full bg-emerald-400/10 px-2.5 py-1 font-medium text-emerald-300">
              <Zap className="size-3.5" /> First defence {duration(contained)} after detection
            </span>
          )}
        </div>
        <ol className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {pipeline.map((s, i) => (
            <PipelineStep key={s.name} step={s} index={i} />
          ))}
        </ol>
      </div>
    </section>
  );
}

function Defence({ icon: Icon, label, value, on }: { icon: LucideIcon; label: string; value: string; on: boolean }) {
  return (
    <div className={`rounded-xl px-2 py-3 ring-1 ${on ? "bg-emerald-400/[0.08] ring-emerald-400/25" : "bg-white/[0.03] ring-white/[0.07]"}`}>
      <dt className="flex items-center justify-center gap-1 text-[11px] text-ink-3">
        <Icon className={`size-3.5 ${on ? "text-emerald-300" : ""}`} /> {label}
      </dt>
      <dd className={`mt-1 text-lg font-semibold ${on ? "text-emerald-200" : "text-ink-1"}`}>{value}</dd>
    </div>
  );
}

function PipelineStep({ step, index }: { step: Step; index: number }) {
  const Icon = step.icon;
  const ring =
    step.state === "done"
      ? "bg-emerald-400/12 text-emerald-300 ring-emerald-400/30"
      : step.state === "active"
        ? "bg-guard/15 text-sky-200 ring-guard/50"
        : "bg-white/[0.03] text-ink-3 ring-white/10";
  return (
    <li className={`relative flex gap-3 rounded-xl p-3 ring-1 transition ${step.state === "idle" ? "ring-white/[0.05]" : step.state === "active" ? "bg-guard/[0.05] ring-guard/25" : "bg-white/[0.02] ring-white/[0.07]"}`}>
      <span className={`flex size-9 shrink-0 items-center justify-center rounded-lg ring-1 ${ring}`}>
        {step.state === "active" && step.name !== "Watch" ? <Loader2 className="size-4 animate-spin" /> : <Icon className="size-4" />}
      </span>
      <span className="min-w-0">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold tracking-wider text-ink-3 uppercase">
          {index + 1}. {step.name}
          {step.state === "done" && <span className="size-1.5 rounded-full bg-emerald-400" aria-label="done" />}
        </span>
        <span className={`mt-0.5 block text-sm leading-snug font-medium ${step.state === "idle" ? "text-ink-2" : "text-ink-1"}`}>{step.title}</span>
        <span className="mt-0.5 block text-xs leading-snug text-ink-3">{step.detail}</span>
      </span>
    </li>
  );
}
