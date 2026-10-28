"use client";

import {
  Ban,
  BellRing,
  Bot,
  Check,
  CircleDollarSign,
  Clock,
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
import { api, ApiError, type ActionRow, type AlertRow, type Classification, type IncidentDetail, type IncidentSummary } from "@/lib/api";
import {
  ACTION_LABEL,
  ACTION_MECHANISM,
  CLASSIFICATION_LABEL,
  DECIDED_BY_LABEL,
  FEATURE_LABEL,
  RULES_HINT_LABEL,
  classificationTone,
  clock,
  featureValue,
  hhmm,
  money,
  ruleLabel,
  timeAgo,
  times,
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

const CLASS_ORDER: Classification[] = ["card_testing", "account_testing", "genuine_rush", "paypal_outage"];
const BAR_COLOR: Record<string, string> = {
  card_testing: "bg-red-400",
  account_testing: "bg-red-300",
  genuine_rush: "bg-sky-400",
  paypal_outage: "bg-amber-400",
};

/** The scorecard's per-class probabilities: what decided the classification (not the AI). */
export function ClassBars({ probs }: { probs: Record<string, number> }) {
  return (
    <ul className="space-y-1">
      {CLASS_ORDER.filter((c) => c in probs).map((c) => {
        const pct = Math.round((probs[c] ?? 0) * 100);
        return (
          <li key={c} className="flex items-center gap-2 text-xs text-ink-2">
            <span className="w-32 shrink-0 truncate">{CLASSIFICATION_LABEL[c]}</span>
            <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-white/[0.06]">
              <div className={`h-full ${BAR_COLOR[c]}`} style={{ width: `${pct}%` }} />
            </div>
            <span className="w-9 shrink-0 text-right tabular-nums">{pct}%</span>
          </li>
        );
      })}
    </ul>
  );
}

/** Who decided (the scorecard, with the tie-break outcome) and who wrote the words (Gemini or template). */
export function DecisionSource({ incident }: { incident: IncidentSummary }) {
  const tb = incident.scorecard?.tiebreak;
  const explainer = incident.model_id && incident.model_id !== "template" && incident.model_id !== "rules-fallback" ? incident.model_id : null;
  return (
    <div className="space-y-0.5 text-xs text-ink-2">
      <p>
        <span className="text-ink-3">Decided by:</span>{" "}
        <span className="font-medium text-ink-1">{incident.classifier_version ?? "AI analyst (before the scorecard)"}</span>
        {incident.decided_by && incident.decided_by !== "scorecard" && <span> · {DECIDED_BY_LABEL[incident.decided_by]}</span>}
      </p>
      {tb && (
        <p>
          <span className="text-ink-3">AI second opinion:</span>{" "}
          {tb.outcome === "agree"
            ? `agreed${tb.model ? ` (${tb.model})` : ""}: confidence capped, never raised`
            : tb.outcome === "disagree"
              ? `read it as ${tb.gemini ? CLASSIFICATION_LABEL[tb.gemini] : "something else"}, so it stays unclear (soft defences only)`
              : tb.outcome === "unverified"
                ? "its numbers did not match ours: the scorecard's call stands"
                : `unavailable${incident.ai_fallback_reason ? ` (${incident.ai_fallback_reason})` : ""}: the scorecard's call stands`}
        </p>
      )}
      <p className="flex items-center gap-1">
        {explainer ? <Sparkles className="size-3 text-violet-300" /> : <Bot className="size-3" />}
        <span className="text-ink-3">Explained by:</span> {explainer ?? "Checkout Guard's own template"}
        {incident.ai_fallback_reason && !tb && <span className="text-amber-300"> (AI: {incident.ai_fallback_reason})</span>}
      </p>
    </div>
  );
}

/** Shown when the AI cited numbers that didn't match ours: those claims never reach the screen or the policy. */
export function DroppedBadge({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <span title="The AI cited numbers that did not match what Checkout Guard computed. Those claims were removed before anything was shown or decided.">
      <Badge tone="warn">
        {count} AI claim{count === 1 ? "" : "s"} dropped: didn&apos;t match our numbers
      </Badge>
    </span>
  );
}

export function VerdictCard({ incident, compact = false }: { incident: IncidentDetail; compact?: boolean }) {
  const v = incident.verdict;
  if (!v) {
    return (
      <div className="scan relative flex items-center gap-4 overflow-hidden rounded-xl border border-violet-400/20 bg-violet-500/[0.06] p-4">
        <Loader2 className="size-5 animate-spin text-violet-300" />
        <div>
          <p className="text-sm font-medium text-ink-1">Scoring the traffic…</p>
          <p className="text-xs text-ink-3">Fixed rules already switched on 3-D Secure if fraud declines were seen.</p>
        </div>
      </div>
    );
  }
  // The scorecard decides the class (P9); ai_available only says whether Gemini wrote the explanation.
  const tone = classificationTone(v.classification);
  const shown = compact ? v.evidence.slice(0, 3) : v.evidence;
  const dropped = incident.dropped_evidence ?? 0;
  const leaning = v.classification === "unclear" && incident.scorecard ? CLASSIFICATION_LABEL[incident.scorecard.top_class] : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <ConfidenceRing value={v.confidence} tone={tone} />
        <div className="min-w-0">
          <p className="text-xl font-semibold tracking-tight text-ink-1">{CLASSIFICATION_LABEL[v.classification]}</p>
          {leaning && <p className="text-xs text-ink-3">leaning {leaning.toLowerCase()}</p>}
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Badge tone="neutral">
              <ShieldCheck className="size-3 text-sky-300" /> {incident.classifier_version ?? "scorecard"}
            </Badge>
            {incident.ai_available ? (
              <Badge tone="neutral">
                <Sparkles className="size-3 text-violet-300" /> explained by {incident.model_id}
              </Badge>
            ) : (
              <Badge tone="warn">AI unavailable{incident.ai_fallback_reason ? `: ${incident.ai_fallback_reason}` : ""}: template text</Badge>
            )}
            {v.trend !== "stable" && <Badge tone={v.trend === "escalating" ? "bad" : "good"}>{v.trend}</Badge>}
            {incident.verdict_at && <span className="text-xs text-ink-3">updated {timeAgo(incident.verdict_at)}</span>}
          </div>
        </div>
      </div>

      {!compact && (
        <div className="grid gap-4 sm:grid-cols-2">
          <DecisionSource incident={incident} />
          {incident.class_probs && <ClassBars probs={incident.class_probs} />}
        </div>
      )}

      {incident.ai_refresh_failed_at && (
        <p className="rounded-lg bg-amber-400/10 px-3 py-2 text-xs text-amber-200">
          AI re-check failed {timeAgo(incident.ai_refresh_failed_at)} ({incident.ai_refresh_failures}×)
          {incident.ai_fallback_reason ? `: ${incident.ai_fallback_reason}` : ""}. Keeping the earlier verdict; new refunds wait for your approval if this persists.
        </p>
      )}
      {v.classification === "unclear" && incident.rules_hint && (
        <p className="text-xs text-ink-2">Fixed rules saw {RULES_HINT_LABEL[incident.rules_hint]}. This is a hint, not a classification.</p>
      )}

      <p className="text-[15px] leading-relaxed text-ink-1/90">{v.summary}</p>

      {dropped > 0 && <DroppedBadge count={dropped} />}
      {shown.length > 0 && (
        <ul className="space-y-1.5">
          {shown.map((e, i) => {
            // Our own number and the ×N from our stored baseline; the text is only the "why".
            const ours = incident.evidence_vs_normal?.[i];
            const observed = ours?.feature === e.feature && ours.observed !== null ? ours.observed : e.observed;
            const vsNormal = ours?.feature === e.feature && ours.multiplier != null && ours.normal != null ? ours : null;
            return (
              <li key={i} className="rise flex items-start gap-3 rounded-lg bg-white/[0.03] px-3 py-2.5" style={{ animationDelay: `${i * 80}ms` }}>
                <span className="w-16 shrink-0 text-right">
                  <span className="block text-base font-semibold text-ink-1 tabular-nums">{featureValue(e.feature, observed)}</span>
                  {vsNormal && (
                    <span className="block text-[11px] text-amber-300 tabular-nums" title={`Normal for this shop: ${featureValue(e.feature, vsNormal.normal)}`}>
                      {times(vsNormal.multiplier!)} normal
                    </span>
                  )}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-ink-1">
                    {FEATURE_LABEL[e.feature] ?? e.feature}
                    <span className="ml-1.5 text-xs font-normal text-ink-3">last {e.window_min} min</span>
                  </span>
                  <span className="block text-xs leading-relaxed text-ink-2">{e.why}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {compact && v.evidence.length > shown.length && <p className="text-xs text-ink-3">+{v.evidence.length - shown.length} more signals in the full report</p>}

      {v.uncertainty_notes && !/^\s*(none|n\/a)?\.?\s*$/i.test(v.uncertainty_notes) && <p className="text-xs text-ink-3">Note: {v.uncertainty_notes}</p>}
      <p className="flex items-center gap-1.5 border-t border-white/[0.06] pt-3 text-[11px] text-ink-3">
        <ShieldCheck className="size-3.5" /> Numbers checked against Checkout Guard&apos;s own records. The scorecard decides, fixed rules act, the AI only explains.
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

const STATUS_BADGE: Record<ActionRow["status"], { tone: "good" | "warn" | "neutral" | "bad" | "info"; label: string }> = {
  executed: { tone: "good", label: "Done" },
  awaiting_approval: { tone: "warn", label: "Needs you" },
  rejected: { tone: "neutral", label: "Kept" },
  failed: { tone: "bad", label: "Failed" },
  in_flight: { tone: "info", label: "Sent" },
};

/** What Checkout Guard did, in order, with the rule that allowed it and the PayPal mechanism that carried it out. */
export function DecisionTrace({ incident, onChange }: { incident: IncidentDetail; onChange: () => void }) {
  const refunds = incident.actions.filter((a) => a.type === "REFUND");
  const others = incident.actions.filter((a) => a.type !== "REFUND");
  const executed = refunds.filter((a) => a.status === "executed");
  const pending = refunds.filter((a) => a.status === "awaiting_approval");
  const inFlight = refunds.filter((a) => a.status === "in_flight");
  // Refused by PayPal, or no answer after every retry: money may already have moved, so each one is shown.
  // A failed try that a later one settled (retry, approval) is history, not something to check.
  const settled = new Set(refunds.filter((a) => a.status !== "failed").map((a) => String(a.params.attempt_id)));
  const failed = refunds.filter((a) => a.status === "failed" && !settled.has(String(a.params.attempt_id)));
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
  if (inFlight.length) {
    rows.push({
      key: "in-flight",
      at: inFlight[0].created_at,
      type: "REFUND",
      title: `${inFlight.length} refund${inFlight.length === 1 ? "" : "s"} sent to PayPal, waiting for its answer`,
      why: "Re-sent automatically with the same request id, so PayPal refunds each one once",
      status: "in_flight",
    });
  }
  rows.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());

  return (
    <div className="space-y-4">
      {rows.length === 0 && pending.length === 0 && failed.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-3">No defences needed.</p>
      ) : (
        <ol className="relative space-y-1">
          {rows.map((r, i) => {
            const Icon = r.status === "in_flight" ? Clock : ACTION_ICON[r.type];
            const badge = STATUS_BADGE[r.status];
            return (
              <li key={r.key} className="rise relative flex gap-3 rounded-lg px-2 py-2.5 hover:bg-white/[0.02]" style={{ animationDelay: `${i * 60}ms` }}>
                {i < rows.length - 1 && <span className="absolute top-11 bottom-0 left-[1.45rem] w-px bg-white/[0.08]" aria-hidden />}
                <span
                  className={`relative flex size-8 shrink-0 items-center justify-center rounded-full ring-1 ${
                    r.type === "REFUND"
                      ? "bg-amber-400/10 text-amber-300 ring-amber-400/25"
                      : r.type === "NOTIFY"
                        ? "bg-violet-400/10 text-violet-300 ring-violet-400/25"
                        : "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25"
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

      {failed.length > 0 && (
        <ul className="space-y-1.5">
          {failed.map((a) => (
            <li key={a.id} className="flex items-start gap-2.5 rounded-lg bg-red-500/10 px-3 py-2 text-xs ring-1 ring-red-400/20">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-red-300" />
              <span className="min-w-0">
                <span className="block text-red-100">
                  Refund of {money(String(a.params.amount ?? 0))} not confirmed · capture <code className="font-mono">{String(a.params.capture_id)}</code>
                </span>
                <span className="block text-red-200/80">{a.error ?? "PayPal did not confirm it"}. Check this capture in PayPal.</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {kept.length > 0 && <p className="px-1 text-xs text-ink-3">{kept.length} payment{kept.length === 1 ? "" : "s"} kept at your request (no refund).</p>}

      {pending.length > 0 && (
        <div className="rounded-xl border border-amber-400/25 bg-amber-400/[0.07] p-3">
          <p className="text-sm font-medium text-amber-100">
            {pending.length} refund{pending.length === 1 ? "" : "s"} need your approval
          </p>
          <ul className="mt-2 space-y-2">
            {pending.map((a) => (
              <PendingRefund key={a.id} action={a} onChange={onChange} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// Why a refund waits for the owner (policy rule) and why the payment matched the attack (params.matched_on).
const WAIT_REASON: Record<string, string> = {
  R5_weak_match_needs_owner: "weak match",
  R5_sca_passed_needs_owner: "buyer passed 3-D Secure, which bots rarely do",
  R5_over_cap_needs_owner: "above your auto-refund limit",
  R5_stale_verdict_needs_owner: "the AI-confirmed verdict is out of date",
};
const MATCH_REASON: Record<string, string> = {
  strong_fingerprint: "device used several cards or accounts",
  blocked_device: "device already blocked as a bot",
  flagged_device: "device flagged in this attack",
  single_decline_device: "device had one declined payment",
  suspect_domain: "email domain with many declines",
};

function refundReason(action: ActionRow): string {
  const on = ((action.params.matched_on as string[] | undefined) ?? []).map((m) => MATCH_REASON[m] ?? m);
  return [WAIT_REASON[action.rule] ?? "needs your approval", ...on].join(" · ");
}

function PendingRefund({ action, onChange }: { action: ActionRow; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function act(kind: "approve" | "reject") {
    setBusy(true);
    setError(null);
    const path = `/dashboard/incidents/${action.incident_id}/actions/${action.id}/${kind}`;
    try {
      try {
        await api(path, { method: "POST" });
      } catch (e) {
        // A disputed payment: refunding can pay the buyer twice. Only on an explicit second confirmation.
        if (!(e instanceof ApiError && e.code === "disputed")) throw e;
        if (!window.confirm(`${e.message}\n\nRefund anyway?`)) return;
        await api(path, { method: "POST", json: { override_dispute: true } });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
      onChange();
    }
  }
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 text-sm">
      <span className="min-w-0 text-amber-50">
        {money(String(action.params.amount ?? 0))} · <code className="font-mono text-xs text-amber-200/80">{String(action.params.capture_id)}</code>
        <span className="block text-xs text-amber-200/70">{refundReason(action)}</span>
        {error && <span className="block text-xs text-red-300">{error}</span>}
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

/**
 * The owner's phone: the alert history as a notification stack, newest on top (GET /dashboard/alerts).
 * Falls back to the report's current alert text when nothing was sent yet (e.g. only regenerated).
 */
export function AlertPhone({
  text,
  at,
  history,
  caption = "Meera's phone",
}: {
  text: string | null | undefined;
  at?: string;
  history?: AlertRow[];
  caption?: string;
}) {
  const alerts = history?.length ? history.map((a) => ({ key: String(a.id), text: a.text, ts: a.ts })) : text ? [{ key: "current", text, ts: at }] : [];
  const [latest, ...older] = alerts;
  const time = latest?.ts ? new Date(latest.ts) : null;
  return (
    <figure className="flex flex-col items-center">
      <div className="relative w-full max-w-[250px] rounded-[2.4rem] bg-gradient-to-b from-slate-600 to-slate-800 p-[3px] shadow-[0_30px_60px_-20px_rgb(0_0_0/0.9)]">
        <div className="relative overflow-hidden rounded-[2.25rem] bg-[radial-gradient(120%_80%_at_50%_0%,#2a3f7a_0%,#121a38_45%,#070b18_100%)] px-3.5 pt-3 pb-5">
          <div className="mx-auto h-5 w-20 rounded-full bg-black" aria-hidden />
          <p className="mt-4 text-center text-xs font-medium text-white/60">
            {time ? time.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }) : "Tonight"}
          </p>
          <p className="text-center text-5xl font-extralight tracking-tight text-white/95 tabular-nums">{time ? hhmm(time) : "--:--"}</p>
          <div className={`mt-6 rounded-2xl bg-white/[0.14] p-3 backdrop-blur-md ${latest ? "rise" : ""}`}>
            <p className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-white/70 uppercase">
              <span className="flex size-4 items-center justify-center rounded-[5px] bg-guard">
                <ShieldCheck className="size-2.5 text-white" />
              </span>
              Checkout Guard
              <span className="ml-auto font-normal normal-case">{latest?.ts ? timeAgo(latest.ts) : ""}</span>
            </p>
            <p className="mt-1.5 text-[13px] leading-snug text-white">{latest?.text ?? "No alerts. Sleep well."}</p>
          </div>
          {older.length > 0 && (
            <ul className="mt-2 max-h-40 space-y-1.5 overflow-y-auto">
              {older.map((a) => (
                <li key={a.key} className="rounded-xl bg-white/[0.07] px-3 py-2">
                  <p className="text-[10px] font-semibold tracking-wide text-white/50 uppercase">{a.ts ? `${hhmm(a.ts)} · ${timeAgo(a.ts)}` : "earlier"}</p>
                  <p className="line-clamp-2 text-[12px] leading-snug text-white/80">{a.text}</p>
                </li>
              ))}
            </ul>
          )}
          <div className="mx-auto mt-8 h-1 w-24 rounded-full bg-white/50" aria-hidden />
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
