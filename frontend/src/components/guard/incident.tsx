"use client";

import {
  Ban,
  BellRing,
  Bot,
  Check,
  CircleDollarSign,
  Loader2,
  PauseCircle,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  X,
  type LucideIcon,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useState } from "react";
import { api, type ActionRow, type IncidentDetail } from "@/lib/api";
import {
  ACTION_LABEL,
  ACTION_MECHANISM,
  CLASSIFICATION_LABEL,
  FEATURE_LABEL,
  RULES_HINT_LABEL,
  classificationTone,
  clock,
  featureValue,
  money,
  ruleLabel,
  timeAgo,
} from "@/lib/format";
import { Badge, Button } from "./ui";

const RING_COLOR = { bad: "#f87171", info: "#60a5fa", warn: "#fbbf24", good: "#34d399", neutral: "#7f8aa3" };

export function ConfidenceRing({ value, tone, size = 72 }: { value: number; tone: keyof typeof RING_COLOR; size?: number }) {
  const r = size / 2 - 5;
  const c = 2 * Math.PI * r;
  const pct = Math.round(value * 100);
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={`${pct}% confident`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={5} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={RING_COLOR[tone]}
          strokeWidth={5}
          strokeLinecap="round"
          strokeDasharray={`${(c * pct) / 100} ${c}`}
          style={{ transition: "stroke-dasharray 0.8s ease" }}
        />
      </svg>
      <span className="absolute inset-0 flex flex-col items-center justify-center leading-none">
        <span className="text-lg font-semibold text-ink-1">{pct}%</span>
        <span className="mt-0.5 text-[9px] font-medium tracking-wider text-ink-3 uppercase">sure</span>
      </span>
    </div>
  );
}

export function VerdictCard({ incident, compact = false }: { incident: IncidentDetail; compact?: boolean }) {
  const v = incident.verdict;
  if (!v) {
    return (
      <div className="scan relative flex items-center gap-4 overflow-hidden rounded-xl border border-violet-400/20 bg-violet-500/[0.06] p-4">
        <Loader2 className="size-5 animate-spin text-violet-300" />
        <div>
          <p className="text-sm font-medium text-ink-1">The AI analyst is reading the traffic…</p>
          <p className="text-xs text-ink-3">Fixed rules already switched on 3-D Secure if fraud declines were seen.</p>
        </div>
      </div>
    );
  }
  const tone = incident.ai_available ? classificationTone(v.classification) : "neutral";
  const evidence = compact ? v.evidence.slice(0, 3) : v.evidence;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        {incident.ai_available ? (
          <ConfidenceRing value={v.confidence} tone={tone} />
        ) : (
          <span className="flex size-[72px] shrink-0 items-center justify-center rounded-full bg-white/[0.04] ring-1 ring-white/10">
            <Bot className="size-7 text-ink-3" />
          </span>
        )}
        <div className="min-w-0">
          <p className="text-xl font-semibold tracking-tight text-ink-1">
            {incident.ai_available ? CLASSIFICATION_LABEL[v.classification] : "Fixed rules in charge"}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {incident.ai_available ? (
              <Badge tone="neutral">
                <Sparkles className="size-3 text-violet-300" /> {incident.model_id}
              </Badge>
            ) : (
              <Badge tone="warn">AI unavailable</Badge>
            )}
            {incident.ai_available && v.trend !== "stable" && (
              <Badge tone={v.trend === "escalating" ? "bad" : "good"}>{v.trend}</Badge>
            )}
            {incident.verdict_at && <span className="text-xs text-ink-3">updated {timeAgo(incident.verdict_at)}</span>}
          </div>
        </div>
      </div>

      {incident.ai_available && incident.ai_refresh_failed_at && (
        <p className="rounded-lg bg-amber-400/10 px-3 py-2 text-xs text-amber-200">
          AI re-check failed {timeAgo(incident.ai_refresh_failed_at)} ({incident.ai_refresh_failures}×). Keeping the earlier verdict; new refunds wait for your approval if
          this persists.
        </p>
      )}
      {!incident.ai_available && incident.rules_hint && (
        <p className="text-xs text-ink-2">Fixed rules saw {RULES_HINT_LABEL[incident.rules_hint]}. This is not an AI verdict.</p>
      )}

      <p className="text-[15px] leading-relaxed text-ink-1/90">{v.summary}</p>

      {evidence.length > 0 && (
        <ul className="space-y-1.5">
          {evidence.map((e, i) => (
            <li key={i} className="rise flex items-start gap-3 rounded-lg bg-white/[0.03] px-3 py-2.5" style={{ animationDelay: `${i * 80}ms` }}>
              <span className="w-14 shrink-0 text-right text-base font-semibold text-ink-1 tabular-nums">{featureValue(e.feature, e.observed)}</span>
              <span className="min-w-0">
                <span className="block text-sm font-medium text-ink-1">
                  {FEATURE_LABEL[e.feature] ?? e.feature}
                  <span className="ml-1.5 text-xs font-normal text-ink-3">last {e.window_min} min</span>
                </span>
                <span className="block text-xs leading-relaxed text-ink-2">{e.why}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {compact && v.evidence.length > evidence.length && <p className="text-xs text-ink-3">+{v.evidence.length - evidence.length} more signals in the full report</p>}

      {v.uncertainty_notes && !/^\s*(none|n\/a)?\.?\s*$/i.test(v.uncertainty_notes) && <p className="text-xs text-ink-3">Note: {v.uncertainty_notes}</p>}
      <p className="flex items-center gap-1.5 border-t border-white/[0.06] pt-3 text-[11px] text-ink-3">
        <ShieldCheck className="size-3.5" /> Numbers checked against Checkout Guard&apos;s own records. The AI advises; fixed rules decide.
      </p>
    </div>
  );
}

const ACTION_ICON: Record<ActionRow["type"], LucideIcon> = {
  SCA_ON: ShieldCheck,
  BLOCK_FP: Ban,
  HOLD_SKU: PauseCircle,
  REFUND: CircleDollarSign,
  NOTIFY: BellRing,
  DEESCALATE: RotateCcw,
  RELAX: RotateCcw,
};

const STATUS_BADGE: Record<ActionRow["status"], { tone: "good" | "warn" | "neutral" | "bad"; label: string }> = {
  executed: { tone: "good", label: "Done" },
  awaiting_approval: { tone: "warn", label: "Needs you" },
  rejected: { tone: "neutral", label: "Kept" },
  failed: { tone: "bad", label: "Failed" },
};

/** What Checkout Guard did, in order, with the rule that allowed it and the PayPal mechanism that carried it out. */
export function DecisionTrace({ incident, onChange }: { incident: IncidentDetail; onChange: () => void }) {
  const refunds = incident.actions.filter((a) => a.type === "REFUND");
  const others = incident.actions.filter((a) => a.type !== "REFUND");
  const executed = refunds.filter((a) => a.status === "executed");
  const pending = refunds.filter((a) => a.status === "awaiting_approval");
  const failed = refunds.filter((a) => a.status === "failed");
  const kept = refunds.filter((a) => a.status === "rejected");
  const total = executed.reduce((s, a) => s + Number(a.params.amount ?? 0), 0);
  const simulated = executed.some((a) => a.params.simulated);
  const refs = executed.map((a) => a.paypal_ref).filter(Boolean) as string[];

  type Row = { key: string; at: string; type: ActionRow["type"]; title: string; why: string; status: ActionRow["status"]; extra?: React.ReactNode };
  const rows: Row[] = others.map((a) => ({
    key: a.id,
    at: a.created_at,
    type: a.type,
    title: `${ACTION_LABEL[a.type] ?? a.type}${a.type === "BLOCK_FP" ? ` (${(a.params.fingerprints as string[] | undefined)?.length ?? 0})` : ""}`,
    why: ruleLabel(a.rule),
    status: a.status,
  }));
  if (executed.length) {
    rows.push({
      key: "refunds",
      at: executed[0].created_at,
      type: "REFUND",
      title: `Refunded ${executed.length} test payment${executed.length === 1 ? "" : "s"} (${money(total)}) before they could become disputes`,
      why: ruleLabel(executed[0].rule),
      status: "executed",
      extra: (
        <p className="mt-1 truncate font-mono text-[11px] text-ink-3">
          {simulated ? "Simulated captures: no PayPal call · " : ""}
          {refs.slice(0, 3).join(" · ")}
          {refs.length > 3 && ` · +${refs.length - 3} more`}
        </p>
      ),
    });
  }
  rows.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());

  return (
    <div className="space-y-4">
      {rows.length === 0 && pending.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-3">No defences needed.</p>
      ) : (
        <ol className="relative space-y-1">
          {rows.map((r, i) => {
            const Icon = ACTION_ICON[r.type];
            const badge = STATUS_BADGE[r.status];
            return (
              <li key={r.key} className="rise relative flex gap-3 rounded-lg px-2 py-2.5 hover:bg-white/[0.02]" style={{ animationDelay: `${i * 60}ms` }}>
                {i < rows.length - 1 && <span className="absolute top-11 bottom-0 left-[1.45rem] w-px bg-white/[0.08]" aria-hidden />}
                <span
                  className={`relative flex size-8 shrink-0 items-center justify-center rounded-full ring-1 ${
                    r.type === "REFUND" ? "bg-amber-400/10 text-amber-300 ring-amber-400/25" : r.type === "NOTIFY" ? "bg-violet-400/10 text-violet-300 ring-violet-400/25" : "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25"
                  }`}
                >
                  <Icon className="size-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-3">
                    <p className="text-sm font-medium text-ink-1">{r.title}</p>
                    <Badge tone={badge.tone}>{badge.label}</Badge>
                  </div>
                  <p className="mt-0.5 text-xs text-ink-2">{r.why}</p>
                  <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-3">
                    <span className="tabular-nums">{clock(r.at)}</span>
                    <span className="rounded bg-[#0070e0]/15 px-1.5 py-px font-medium text-[#7fb6ff]">PayPal</span>
                    {ACTION_MECHANISM[r.type]}
                  </p>
                  {r.extra}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {(failed.length > 0 || kept.length > 0) && (
        <p className="flex items-center gap-2 rounded-lg bg-white/[0.03] px-3 py-2 text-xs text-ink-2">
          <TriangleAlert className="size-3.5 text-amber-300" />
          {failed.length > 0 && `${failed.length} refund${failed.length === 1 ? "" : "s"} failed at PayPal. `}
          {kept.length > 0 && `${kept.length} kept at your request.`}
        </p>
      )}

      {pending.length > 0 && (
        <div className="rounded-xl border border-amber-400/25 bg-amber-400/[0.07] p-3">
          <p className="text-sm font-medium text-amber-100">
            {pending.length} refund{pending.length === 1 ? "" : "s"} need your approval
          </p>
          <p className="text-xs text-amber-200/70">Above your auto-refund limit, or the AI verdict is out of date.</p>
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
  const [error, setError] = useState<string | null>(null);
  async function act(kind: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await api(`/dashboard/incidents/${action.incident_id}/actions/${action.id}/${kind}`, { method: "POST" });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
      onChange();
    }
  }
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span className="text-amber-50">
        {money(String(action.params.amount ?? 0))} · <code className="font-mono text-xs text-amber-200/80">{String(action.params.capture_id)}</code>
        {error && <span className="ml-2 text-xs text-red-300">{error}</span>}
      </span>
      <span className="flex gap-1.5">
        <Button variant="secondary" size="sm" disabled={busy} onClick={() => act("reject")}>
          <X className="size-3.5" /> Keep
        </Button>
        <Button size="sm" disabled={busy} onClick={() => act("approve")}>
          <Check className="size-3.5" /> Refund
        </Button>
      </span>
    </li>
  );
}

export function AlertPhone({ text, at, caption = "Meera's phone" }: { text: string | null | undefined; at?: string; caption?: string }) {
  const time = at ? new Date(at) : null;
  return (
    <figure className="flex flex-col items-center">
      <div className="relative w-full max-w-[250px] rounded-[2.4rem] bg-gradient-to-b from-slate-600 to-slate-800 p-[3px] shadow-[0_30px_60px_-20px_rgb(0_0_0/0.9)]">
        <div className="relative overflow-hidden rounded-[2.25rem] bg-[radial-gradient(120%_80%_at_50%_0%,#2a3f7a_0%,#121a38_45%,#070b18_100%)] px-3.5 pt-3 pb-5">
          <div className="mx-auto h-5 w-20 rounded-full bg-black" aria-hidden />
          <p className="mt-4 text-center text-xs font-medium text-white/60">
            {time ? time.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }) : "Tonight"}
          </p>
          <p className="text-center text-5xl font-extralight tracking-tight text-white/95 tabular-nums">
            {time ? time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "--:--"}
          </p>
          <div className={`mt-6 rounded-2xl bg-white/[0.14] p-3 backdrop-blur-md ${text ? "rise" : ""}`}>
            <p className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-white/70 uppercase">
              <span className="flex size-4 items-center justify-center rounded-[5px] bg-guard">
                <ShieldCheck className="size-2.5 text-white" />
              </span>
              Checkout Guard
              <span className="ml-auto font-normal normal-case">{at ? timeAgo(at) : ""}</span>
            </p>
            <p className="mt-1.5 text-[13px] leading-snug text-white">{text ?? "No alerts. Sleep well."}</p>
          </div>
          <div className="mx-auto mt-10 h-1 w-24 rounded-full bg-white/50" aria-hidden />
        </div>
      </div>
      <figcaption className="mt-3 text-xs text-ink-3">{caption}</figcaption>
    </figure>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-report text-sm text-ink-2">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
