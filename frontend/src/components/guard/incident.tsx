"use client";

import { Ban, BellRing, Bot, Check, CircleDollarSign, Clock, Info, Loader2, PauseCircle, RotateCcw, ShieldCheck, Sparkles, TriangleAlert, Undo2, X, type LucideIcon } from "lucide-react";
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

const RING_COLOR = { bad: "#d92d20", info: "#1570ef", warn: "#dc6803", good: "#079455", neutral: "#98a2b3" };

export function ConfidenceRing({ value, tone, size = 72 }: { value: number; tone: keyof typeof RING_COLOR; size?: number }) {
  const r = size / 2 - 5;
  const c = 2 * Math.PI * r;
  const pct = Math.round(value * 100);
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={`${pct}% confident`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#eef0f3" strokeWidth={5} />
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
      <span className="absolute inset-0 flex items-center justify-center text-lg leading-none font-semibold text-ink-1 tabular-nums">{pct}%</span>
    </div>
  );
}

const CLASS_ORDER: Classification[] = ["card_testing", "account_testing", "genuine_rush", "paypal_outage"];
const BAR_COLOR: Record<string, string> = {
  card_testing: "bg-red-600",
  account_testing: "bg-orange-500",
  genuine_rush: "bg-blue-600",
  paypal_outage: "bg-amber-500",
};

/** The scorecard's per-class probabilities: what decided the classification (not the AI). */
export function ClassBars({ probs }: { probs: Record<string, number> }) {
  return (
    <ul className="space-y-1">
      {CLASS_ORDER.filter((c) => c in probs).map((c) => {
        const pct = Math.round((probs[c] ?? 0) * 100);
        return (
          <li key={c} className="flex items-center gap-2 text-[13px] text-ink-2">
            <span className="w-32 shrink-0 truncate">{CLASSIFICATION_LABEL[c]}</span>
            <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-gray-100">
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
        {explainer ? <Sparkles className="size-3 text-violet-700" /> : <Bot className="size-3" />}
        <span className="text-ink-3">Explained by:</span> {explainer ?? "Checkout Guard's own template"}
        {incident.ai_fallback_reason && !tb && <span className="text-amber-700"> (AI: {incident.ai_fallback_reason})</span>}
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
      <div className="flex items-center gap-3 rounded-lg border border-line bg-gray-50 p-4" role="status">
        <Loader2 className="size-5 animate-spin text-guard" aria-hidden />
        <div>
          <p className="text-sm font-medium text-ink-1">Scoring the traffic…</p>
          <p className="text-[13px] text-ink-3">Fixed rules already switched on 3-D Secure if fraud declines were seen.</p>
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
        <div className="flex shrink-0 flex-col items-center gap-1">
          <ConfidenceRing value={v.confidence} tone={tone} />
          <span className="text-xs text-ink-3">confidence</span>
        </div>
        <div className="min-w-0">
          <p className="text-xl font-semibold tracking-tight text-ink-1">{CLASSIFICATION_LABEL[v.classification]}</p>
          {leaning && <p className="text-xs text-ink-3">leaning {leaning.toLowerCase()}</p>}
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Badge tone="neutral">Decided by {incident.classifier_version ?? "scorecard"}</Badge>
            {incident.ai_available ? (
              <Badge tone="neutral">Explained by {incident.model_id}</Badge>
            ) : (
              <Badge tone="warn">AI unavailable: template text</Badge>
            )}
            {v.trend !== "stable" && <Badge tone={v.trend === "escalating" ? "bad" : "good"}>{v.trend}</Badge>}
            {incident.verdict_at && <span className="text-[13px] text-ink-3">Updated {timeAgo(incident.verdict_at)}</span>}
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
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
          AI re-check failed {timeAgo(incident.ai_refresh_failed_at)} ({incident.ai_refresh_failures}×)
          {incident.ai_fallback_reason ? `: ${incident.ai_fallback_reason}` : ""}. Keeping the earlier verdict; new refunds wait for your approval if this persists.
        </p>
      )}
      {v.classification === "unclear" && incident.rules_hint && (
        <p className="text-xs text-ink-2">Fixed rules saw {RULES_HINT_LABEL[incident.rules_hint]}. This is a hint, not a classification.</p>
      )}

      <p className="text-[15px] leading-relaxed text-ink-1">{v.summary}</p>

      {dropped > 0 && <DroppedBadge count={dropped} />}
      {shown.length > 0 && (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {shown.map((e, i) => {
            // Our own number and the ×N from our stored baseline; the text is only the "why".
            const ours = incident.evidence_vs_normal?.[i];
            const observed = ours?.feature === e.feature && ours.observed !== null ? ours.observed : e.observed;
            const vsNormal = ours?.feature === e.feature && ours.multiplier != null && ours.normal != null ? ours : null;
            return (
              <li key={i} className="flex items-start gap-3 px-3 py-2.5">
                <span className="w-16 shrink-0 text-right">
                  <span className="block text-base font-semibold text-ink-1 tabular-nums">{featureValue(e.feature, observed)}</span>
                  {vsNormal && (
                    <span className="block text-xs text-amber-700 tabular-nums" title={`Normal for this shop: ${featureValue(e.feature, vsNormal.normal)}`}>
                      {times(vsNormal.multiplier!)} normal
                    </span>
                  )}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-ink-1">
                    {FEATURE_LABEL[e.feature] ?? e.feature}
                    <span className="ml-1.5 text-xs font-normal text-ink-3">Last {e.window_min} min</span>
                  </span>
                  <span className="block text-[13px] leading-relaxed text-ink-2">{e.why}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {compact && v.evidence.length > shown.length && <p className="text-[13px] text-ink-3">{v.evidence.length - shown.length} more signals in the full report</p>}

      {v.uncertainty_notes && !/^\s*(none|n\/a)?\.?\s*$/i.test(v.uncertainty_notes) && <p className="text-xs text-ink-3">Note: {v.uncertainty_notes}</p>}
      <p className="flex items-center gap-1.5 border-t border-line pt-3 text-xs text-ink-3">
        <ShieldCheck className="size-3.5 shrink-0" aria-hidden /> Every number is checked against our own records. The AI only explains.
      </p>
    </div>
  );
}

const ACTION_ICON: Record<ActionRow["type"], LucideIcon> = {
  SCA_ON: ShieldCheck,
  BLOCK_FP: Ban,
  HOLD_SKU: PauseCircle,
  REFUND: CircleDollarSign,
  VOID: Undo2,
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
  // Payment holds: the attack's held payments voided (never charged) instead of captured and refunded.
  const voids = incident.actions.filter((a) => a.type === "VOID");
  const voided = voids.filter((a) => a.status === "executed");
  const others = incident.actions.filter((a) => a.type !== "REFUND" && a.type !== "VOID");
  const executed = refunds.filter((a) => a.status === "executed");
  const pending = [...refunds, ...voids].filter((a) => a.status === "awaiting_approval");
  const inFlight = refunds.filter((a) => a.status === "in_flight");
  // Refused by PayPal, or no answer after every retry: money may already have moved, so each one is shown.
  // A failed try that a later one settled (retry, approval) is history, not something to check.
  const settled = new Set(refunds.filter((a) => a.status !== "failed").map((a) => String(a.params.attempt_id)));
  const failed = refunds.filter((a) => a.status === "failed" && !settled.has(String(a.params.attempt_id)));
  const kept = [...refunds, ...voids].filter((a) => a.status === "rejected");
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
  if (voided.length) {
    const voidedTotal = voided.reduce((s, a) => s + Number(a.params.amount ?? 0), 0);
    rows.push({
      key: "voids",
      at: voided[0].created_at,
      type: "VOID",
      title: `Voided ${voided.length} held payment${voided.length === 1 ? "" : "s"} (${money(voidedTotal)}): never charged, no fee, nothing to dispute`,
      why: ruleLabel(voided[0].rule),
      status: "executed",
      extra: voided.some((a) => a.params.simulated) ? <p className="mt-1 text-[11px] text-ink-3">Simulated holds: no PayPal call</p> : undefined,
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

  const seen = incident.seen_via;
  const webhooksOnly = !!seen && seen.observed > 0 && seen.guarded === 0;
  return (
    <div className="space-y-4">
      {seen && seen.observed > 0 && (
        <p className="flex items-start gap-2 rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-900 ring-1 ring-sky-200">
          <Info className="mt-0.5 size-3.5 shrink-0 text-sky-700" />
          <span>
            {webhooksOnly ? "Seen through PayPal webhooks only (observer mode): " : `${seen.observed} of these payments were seen through PayPal webhooks only: `}
            detection, the verdict, alerts and refunds work, but blocking devices, 3-D Secure, the item hold and payment holds act only on checkouts that
            call the guard (the shop, or <code className="font-mono">POST /v1/guard/check</code>).
          </span>
        </p>
      )}
      {rows.length === 0 && pending.length === 0 && failed.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink-3">No defences needed.</p>
      ) : (
        <ol className="relative space-y-1">
          {rows.map((r, i) => {
            const Icon = r.status === "in_flight" ? Clock : ACTION_ICON[r.type];
            const badge = STATUS_BADGE[r.status];
            return (
              <li key={r.key} className="rise relative flex gap-3 rounded-lg px-2 py-2.5 hover:bg-gray-50" style={{ animationDelay: `${i * 60}ms` }}>
                {i < rows.length - 1 && <span className="absolute top-11 bottom-0 left-[1.45rem] w-px bg-gray-100" aria-hidden />}
                <span
                  className={`relative flex size-8 shrink-0 items-center justify-center rounded-full ring-1 ${
                    r.type === "REFUND" || r.type === "VOID"
                      ? "bg-amber-50 text-amber-700 ring-amber-200"
                      : r.type === "NOTIFY"
                        ? "bg-violet-50 text-violet-700 ring-violet-200"
                        : "bg-emerald-50 text-emerald-700 ring-emerald-200"
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
                    <span className="rounded bg-blue-50 px-1.5 py-px font-medium text-blue-800 ring-1 ring-inset ring-blue-200">PayPal</span>
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
            <li key={a.id} className="flex items-start gap-2.5 rounded-lg bg-red-50 px-3 py-2 text-xs ring-1 ring-red-200">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-red-700" />
              <span className="min-w-0">
                <span className="block text-red-900">
                  Refund of {money(String(a.params.amount ?? 0))} not confirmed · capture <code className="font-mono">{String(a.params.capture_id)}</code>
                </span>
                <span className="block text-red-700">{a.error ?? "PayPal did not confirm it"}. Check this capture in PayPal.</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {kept.length > 0 && <p className="px-1 text-xs text-ink-3">{kept.length} payment{kept.length === 1 ? "" : "s"} kept at your request (charged as usual).</p>}

      {pending.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-medium text-amber-900">
              {pending.length} payment{pending.length === 1 ? "" : "s"} need your decision
            </p>
            {pending.length > 1 && <DecideAll actions={pending} onChange={onChange} />}
          </div>
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
  attack_pattern: "seen through PayPal webhooks only: first-time payer on the attacked item, during the attack",
};

function refundReason(action: ActionRow): string {
  const on = ((action.params.matched_on as string[] | undefined) ?? []).map((m) => MATCH_REASON[m] ?? m);
  return [WAIT_REASON[action.rule] ?? "needs your approval", ...on].join(" · ");
}

/** One tap for the whole list: each payment still goes through its own approve/keep (same checks, same
 * refund keys), one after another. A disputed payment is skipped, never refunded in bulk. */
function DecideAll({ actions, onChange }: { actions: ActionRow[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const total = actions.reduce((s, a) => s + Number(a.params.amount ?? 0), 0);
  async function decide(kind: "approve" | "reject") {
    if (kind === "approve" && !window.confirm(`Refund or void all ${actions.length} payments (${money(total)})?`)) return;
    setBusy(kind);
    let skipped = 0;
    try {
      for (const a of actions) {
        try {
          await api(`/dashboard/incidents/${a.incident_id}/actions/${a.id}/${kind}`, { method: "POST" });
        } catch {
          skipped += 1; // disputed, or decided meanwhile: left for a one-by-one look
        }
      }
    } finally {
      setBusy(null);
      onChange();
      if (skipped) window.alert(`${skipped} payment${skipped === 1 ? " was" : "s were"} skipped (disputed or already decided): check them one by one.`);
    }
  }
  return (
    <span className="flex gap-1.5">
      <Button variant="secondary" size="sm" disabled={!!busy} onClick={() => decide("reject")}>
        {busy === "reject" ? <Loader2 className="size-3.5 animate-spin" /> : <X className="size-3.5" />} Keep all
      </Button>
      <Button size="sm" disabled={!!busy} onClick={() => decide("approve")}>
        {busy === "approve" ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} Approve all ({money(total)})
      </Button>
    </span>
  );
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
      <span className="min-w-0 text-amber-900">
        {action.type === "VOID" ? "Held " : ""}
        {money(String(action.params.amount ?? 0))} ·{" "}
        <code className="font-mono text-xs text-amber-700">{String(action.type === "VOID" ? action.params.authorization_id : action.params.capture_id)}</code>
        <span className="block text-xs text-amber-700">{refundReason(action)}</span>
        {error && <span className="block text-xs text-red-700">{error}</span>}
      </span>
      <span className="flex gap-1.5">
        <Button variant="secondary" size="sm" disabled={busy} onClick={() => act("reject")}>
          <X className="size-3.5" /> Keep
        </Button>
        <Button size="sm" disabled={busy} onClick={() => act("approve")}>
          <Check className="size-3.5" /> {action.type === "VOID" ? "Void" : "Refund"}
        </Button>
      </span>
    </li>
  );
}

/**
 * What the shop owner is sent: the latest alert as a message, earlier ones underneath, newest first
 * (GET /dashboard/alerts). Falls back to the report's current alert text when nothing was sent yet.
 */
export function AlertPhone({
  text,
  at,
  history,
}: {
  text: string | null | undefined;
  at?: string;
  history?: AlertRow[];
}) {
  const alerts = history?.length ? history.map((a) => ({ key: String(a.id), text: a.text, ts: a.ts })) : text ? [{ key: "current", text, ts: at }] : [];
  const [latest, ...older] = alerts;
  if (!latest) return <p className="py-6 text-center text-sm text-ink-3">No alerts sent. Nothing needed the owner.</p>;
  return (
    <div className="space-y-3">
      <article className="rounded-lg border border-line bg-gray-50 p-3.5">
        <header className="flex items-center gap-2 text-[13px]">
          <span className="flex size-6 items-center justify-center rounded-md bg-brand" aria-hidden>
            <ShieldCheck className="size-3.5 text-white" />
          </span>
          <span className="font-semibold text-ink-1">Checkout Guard</span>
          <time className="ml-auto text-ink-3" dateTime={latest.ts}>
            {latest.ts ? `${hhmm(latest.ts)} · ${timeAgo(latest.ts)}` : ""}
          </time>
        </header>
        <p className="mt-2 text-sm leading-relaxed text-ink-1">{latest.text}</p>
      </article>
      {older.length > 0 && (
        <details className="text-[13px]">
          <summary className="cursor-pointer text-ink-2 hover:text-ink-1">
            {older.length} earlier alert{older.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 space-y-2">
            {older.map((a) => (
              <li key={a.key} className="border-l-2 border-line pl-3">
                <p className="text-xs text-ink-3">{a.ts ? hhmm(a.ts) : "Earlier"}</p>
                <p className="text-ink-2">{a.text}</p>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-report text-sm text-ink-2">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
