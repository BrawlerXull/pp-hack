"use client";

import { Bot, Check, ShieldAlert, ShieldCheck, Sparkles, TriangleAlert, Users, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState } from "react";
import { api, type ActionRow, type IncidentDetail, type Summary } from "@/lib/api";
import { ACTION_LABEL, CLASSIFICATION_LABEL, FEATURE_LABEL, STATUS_COPY, featureValue, money, timeAgo } from "@/lib/format";
import { Badge, Button } from "./ui";

const BANNER = {
  good: { box: "from-emerald-50 to-white border-emerald-200", icon: ShieldCheck, iconColor: "text-emerald-600" },
  bad: { box: "from-red-50 to-white border-red-200", icon: ShieldAlert, iconColor: "text-red-600" },
  warn: { box: "from-amber-50 to-white border-amber-200", icon: TriangleAlert, iconColor: "text-amber-600" },
  info: { box: "from-blue-50 to-white border-blue-200", icon: Users, iconColor: "text-blue-600" },
  neutral: { box: "from-slate-50 to-white border-slate-200", icon: ShieldCheck, iconColor: "text-slate-500" },
};

export function StatusBanner({ summary }: { summary: Summary }) {
  const copy = STATUS_COPY[summary.status];
  const style = BANNER[copy.tone];
  const Icon = style.icon;
  const inc = summary.open_incident;
  const gs = summary.guard_state;
  return (
    <div className={`flex flex-col gap-4 rounded-2xl border bg-gradient-to-r p-6 sm:flex-row sm:items-center ${style.box}`}>
      <div className={`flex size-14 shrink-0 items-center justify-center rounded-2xl bg-white shadow-sm ${copy.tone === "bad" ? "pulse-danger" : ""}`}>
        <Icon className={`size-7 ${style.iconColor}`} />
      </div>
      <div className="min-w-0 flex-1">
        <h1 className="text-xl font-semibold text-slate-900">{copy.title}</h1>
        <p className="mt-1 text-sm text-slate-600">
          {inc?.summary ?? (summary.status === "quiet" ? "Checkout Guard is watching every payment. Nothing unusual right now." : "Extra checks are still on while traffic settles.")}
        </p>
      </div>
      <div className="flex flex-wrap gap-2 sm:justify-end">
        <Badge tone={gs.mode === "sca_all" ? "info" : "neutral"}>3-D Secure: {gs.mode === "sca_all" ? "every card" : "when required"}</Badge>
        <Badge tone={gs.blocked_devices ? "bad" : "neutral"}>{gs.blocked_devices} devices blocked</Badge>
        {gs.held_skus.length > 0 && <Badge tone="warn">{gs.held_skus.length} item on hold</Badge>}
      </div>
    </div>
  );
}

export function VerdictCard({ incident }: { incident: IncidentDetail }) {
  const v = incident.verdict;
  if (!v) return <p className="text-sm text-slate-500">Waiting for the AI analyst…</p>;
  const pct = Math.round(v.confidence * 100);
  const tone = v.classification === "card_testing" || v.classification === "account_testing" ? "bad" : v.classification === "genuine_rush" ? "info" : "warn";
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={tone}>{CLASSIFICATION_LABEL[v.classification]}</Badge>
        <div className="flex items-center gap-2 text-sm text-slate-600">
          <div className="h-1.5 w-28 overflow-hidden rounded-full bg-slate-100">
            <div className={`h-full ${tone === "bad" ? "bg-red-500" : tone === "info" ? "bg-blue-500" : "bg-amber-500"}`} style={{ width: `${pct}%` }} />
          </div>
          {pct}% confident
        </div>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-slate-500">
          {incident.ai_available ? <Sparkles className="size-3.5 text-violet-500" /> : <Bot className="size-3.5" />}
          {incident.ai_available ? incident.model_id : "AI unavailable: rule-based fallback"}
        </span>
      </div>
      <p className="text-[15px] leading-relaxed text-slate-800">{v.summary}</p>
      {v.evidence.length > 0 && (
        <div>
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Evidence (verified against our own data)</p>
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
            {v.evidence.map((e, i) => (
              <li key={i} className="flex items-start gap-4 px-4 py-2.5">
                <div className="w-44 shrink-0">
                  <p className="text-sm font-medium text-slate-800">{FEATURE_LABEL[e.feature] ?? e.feature}</p>
                  <p className="text-xs text-slate-500">last {e.window_min} min</p>
                </div>
                <p className="w-20 shrink-0 text-right text-sm font-semibold tabular-nums text-slate-900">{featureValue(e.feature, e.observed)}</p>
                <p className="text-sm text-slate-600">{e.why}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
      {v.uncertainty_notes && !/^\s*(none|n\/a)?\.?\s*$/i.test(v.uncertainty_notes) && (
        <p className="text-xs text-slate-500">Note: {v.uncertainty_notes}</p>
      )}
    </div>
  );
}

export function ActionsList({ incident, onChange }: { incident: IncidentDetail; onChange: () => void }) {
  const refunds = incident.actions.filter((a) => a.type === "REFUND");
  const others = incident.actions.filter((a) => a.type !== "REFUND");
  const executed = refunds.filter((a) => a.status === "executed");
  const pending = refunds.filter((a) => a.status === "awaiting_approval");
  const total = executed.reduce((s, a) => s + Number(a.params.amount ?? 0), 0);
  const simulated = executed.some((a) => a.params.simulated);

  return (
    <div className="space-y-4">
      <ul className="space-y-2">
        {others.map((a) => (
          <li key={a.id} className="flex items-start gap-3 text-sm">
            <Check className="mt-0.5 size-4 shrink-0 text-green-600" />
            <div>
              <p className="text-slate-800">
                {ACTION_LABEL[a.type] ?? a.type}
                {a.type === "BLOCK_FP" && ` (${(a.params.fingerprints as string[] | undefined)?.length ?? 0})`}
              </p>
              <p className="text-xs text-slate-500">
                {timeAgo(a.created_at)} · rule <code className="font-mono">{a.rule}</code>
              </p>
            </div>
          </li>
        ))}
        {executed.length > 0 && (
          <li className="flex items-start gap-3 text-sm">
            <Check className="mt-0.5 size-4 shrink-0 text-green-600" />
            <div>
              <p className="text-slate-800">
                Refunded {executed.length} test payment{executed.length === 1 ? "" : "s"} ({money(total)}) before they could become disputes
              </p>
              <p className="text-xs text-slate-500">
                {simulated ? "Includes simulated captures (no PayPal call)" : "Via PayPal Refunds API"} · rule <code className="font-mono">R5_refund_matched</code>
              </p>
            </div>
          </li>
        )}
        {others.length === 0 && executed.length === 0 && <li className="text-sm text-slate-500">No defences needed.</li>}
      </ul>
      {pending.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3">
          <p className="text-sm font-medium text-amber-900">{pending.length} refund{pending.length === 1 ? "" : "s"} need your approval (above your auto-refund limit)</p>
          <ul className="mt-2 space-y-1.5">
            {pending.map((a) => (
              <PendingRefund key={a.id} action={a} onChange={onChange} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function PendingRefund({ action, onChange }: { action: ActionRow; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  async function act(kind: "approve" | "reject") {
    setBusy(true);
    try {
      await api(`/dashboard/incidents/${action.incident_id}/actions/${action.id}/${kind}`, { method: "POST" });
      onChange();
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="flex items-center justify-between gap-3 text-sm">
      <span className="text-amber-900">
        {money(String(action.params.amount ?? 0))} · capture <code className="font-mono text-xs">{String(action.params.capture_id)}</code>
      </span>
      <span className="flex gap-1.5">
        <Button variant="secondary" className="px-2 py-1 text-xs" disabled={busy} onClick={() => act("reject")}>
          <X className="size-3.5" /> Keep
        </Button>
        <Button className="px-2 py-1 text-xs" disabled={busy} onClick={() => act("approve")}>
          <Check className="size-3.5" /> Refund
        </Button>
      </span>
    </li>
  );
}

export function AlertPhone({ text, at }: { text: string | null | undefined; at?: string }) {
  return (
    <div className="mx-auto w-full max-w-[260px] rounded-[2rem] border-[6px] border-slate-900 bg-gradient-to-b from-indigo-950 to-slate-900 p-3 shadow-xl">
      <p className="mt-1 text-center text-3xl font-light text-white/90 tabular-nums">
        {at ? new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "--:--"}
      </p>
      <div className="mt-4 rounded-2xl bg-white/90 p-3 backdrop-blur">
        <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
          <ShieldCheck className="size-3.5 text-guard" /> Checkout Guard · now
        </p>
        <p className="mt-1 text-[13px] leading-snug text-slate-900">{text ?? "No alerts. Sleep well."}</p>
      </div>
      <div className="mx-auto mt-6 h-1 w-20 rounded-full bg-white/40" />
    </div>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-report text-sm text-slate-800">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
