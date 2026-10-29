"use client";

import {
  ArrowLeft,
  Copy,
  Download,
  Eye,
  FileText,
  Fingerprint,
  Gavel,
  ListChecks,
  Loader2,
  RefreshCw,
  Scale,
  Settings2,
  ShieldOff,
  Smartphone,
  Telescope,
  Sparkles,
  User,
  Waypoints,
  Zap,
  type LucideIcon,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { DecisionStrip } from "@/components/guard/DecisionStrip";
import { DisputesCard } from "@/components/guard/Disputes";
import { AlertPhone, DecisionTrace, Markdown, VerdictCard } from "@/components/guard/incident";
import { defencesSummary } from "@/components/guard/StatusHero";
import { Badge, Button, Card, Empty, Skeleton } from "@/components/guard/ui";
import { api, ApiError, type AlertRow, type IncidentDetail, type TimeseriesPoint } from "@/lib/api";
import { CLASSIFICATION_LABEL, SKU_NAME, classificationTone, clock, duration } from "@/lib/format";
import { useNow } from "@/lib/now";

const ActivityChart = dynamic(() => import("@/components/guard/ActivityChart"), { ssr: false, loading: () => <Skeleton className="h-[260px]" /> });

const TABS = [
  { key: "report", label: "Morning report" },
  { key: "evidence", label: "Evidence for PayPal" },
  { key: "timeline", label: "Audit timeline" },
] as const;
type Tab = (typeof TABS)[number]["key"];

export default function IncidentPage() {
  const { id } = useParams<{ id: string }>();
  const key = `/dashboard/incidents/${id}`;
  const { data: inc, error: loadError, mutate } = useSWR<IncidentDetail>(key);
  const { data: alerts } = useSWR<AlertRow[]>(`/dashboard/alerts?limit=10&incident_id=${encodeURIComponent(id)}`);
  const [tab, setTab] = useState<Tab>("report");
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const now = useNow();

  const evidence = inc?.report?.evidence_md ?? "";
  const downloadUrl = useMemo(() => (evidence ? URL.createObjectURL(new Blob([evidence], { type: "text/markdown" })) : null), [evidence]);
  useEffect(() => () => void (downloadUrl && URL.revokeObjectURL(downloadUrl)), [downloadUrl]);

  // Show the traffic chart while the incident is recent enough for the per-minute series to cover it.
  const sinceOpen = inc ? (now - new Date(inc.opened_at).getTime()) / 60_000 : Infinity;
  const chartMinutes = Math.min(120, Math.max(20, Math.ceil(sinceOpen) + 6));
  const { data: series } = useSWR<TimeseriesPoint[]>(inc && sinceOpen < 115 ? `/dashboard/timeseries?minutes=${chartMinutes}` : null);

  if (loadError) {
    return (
      <Card>
        <Empty icon={FileText}>
          {loadError instanceof ApiError && loadError.status === 404 ? "This incident doesn't exist (it may have been cleared by a demo reset)." : "Couldn't load this incident."}
          <Link href="/guard/incidents" className="text-sm font-medium text-guard hover:underline">
            Back to incidents
          </Link>
        </Empty>
      </Card>
    );
  }
  if (!inc) return <Skeleton className="h-64" />;

  async function act(label: string, path: string) {
    setBusy(label);
    setError(null);
    try {
      await api(path, { method: "POST" });
      await mutate();
    } catch (e) {
      // e.g. 429: the report was regenerated moments ago (each regenerate is a Gemini call).
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(null);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(evidence);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Couldn't copy: your browser blocked clipboard access.");
    }
  }

  const tone = classificationTone(inc.classification);
  const end = inc.closed_at ? new Date(inc.closed_at).getTime() : now;
  const parts = defencesSummary(inc);
  const sig = inc.signature ?? {};

  return (
    <>
      <div className="flex flex-wrap items-start gap-4">
        <Link href="/guard/incidents" className="mt-1 rounded-lg p-1.5 text-ink-3 hover:bg-gray-100 hover:text-ink-1" aria-label="Back to incidents">
          <ArrowLeft className="size-5" />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-2xl font-semibold tracking-tight text-ink-1">{inc.classification ? CLASSIFICATION_LABEL[inc.classification] : "Incident"}</h1>
            <Badge tone={inc.status === "open" ? tone : "neutral"}>{inc.status === "open" ? "Active" : "Closed"}</Badge>
          </div>
          <p className="mt-1 text-sm text-ink-2">
            {new Date(inc.opened_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
            {inc.closed_at && ` → ${clock(inc.closed_at)}`} · {duration(end - new Date(inc.opened_at).getTime())}
            {parts.length > 0 && <span className="text-ink-3"> · {parts.join(" · ")}</span>}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link
            href={`/guard/investigate?incident=${encodeURIComponent(inc.id)}`}
            className="inline-flex items-center gap-1.5 rounded-lg bg-guard px-3.5 py-2 text-sm font-medium text-white shadow-sm hover:bg-guard-dark"
          >
            <Telescope className="size-4" /> Investigate in Studio
          </Link>
          <Button variant="secondary" disabled={!!busy} onClick={() => act("regen", `${key}/report/regenerate`)}>
            {busy === "regen" ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />} Refresh report
          </Button>
          {inc.status === "open" && (
            <Button variant="secondary" disabled={!!busy} onClick={() => act("close", `${key}/close`)}>
              {busy === "close" ? <Loader2 className="size-4 animate-spin" /> : <ShieldOff className="size-4" />} Stand down
            </Button>
          )}
        </div>
      </div>
      {error && <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-700">{error}</p>}

      <Card title="How Checkout Guard decided" subtitle="Detect, judge, decide: every step from our records, none of it the AI's own claim" icon={Scale}>
        <DecisionStrip incident={inc} />
      </Card>

      {series && (
        <Card title="Traffic around this incident" icon={Waypoints} bodyClassName="px-3 pb-3">
          <ActivityChart points={series} minutes={chartMinutes} incident={inc} height={260} />
        </Card>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Verdict" icon={Sparkles} className="xl:col-span-2">
          <VerdictCard incident={inc} />
        </Card>
        <Card title="Owner alerts" icon={Smartphone}>
          <AlertPhone text={inc.report?.alert_text} at={inc.report?.generated_at} history={alerts} />
        </Card>
      </div>

      <DisputesCard incidentId={inc.id} />

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="From verdict to PayPal" subtitle="Each action and the fixed rule that allowed it" icon={ListChecks} className="xl:col-span-2">
          <DecisionTrace incident={inc} onChange={() => mutate()} />
        </Card>
        <Card title="Attack signature" subtitle="Computed from our data. Only payments matching it can be refunded." icon={Fingerprint}>
          <dl className="space-y-4 text-sm">
            <div>
              <dt className="text-xs text-ink-3">Targeted item</dt>
              <dd className="mt-0.5 font-medium text-ink-1">{sig.top_sku ? SKU_NAME[sig.top_sku] ?? sig.top_sku : "None"}</dd>
            </div>
            <div>
              <dt className="text-xs text-ink-3">Suspicious devices ({sig.fingerprints?.length ?? 0})</dt>
              <dd className="mt-1.5 flex flex-wrap gap-1.5">
                {(sig.fingerprints ?? []).slice(0, 12).map((f) => (
                  <code key={f} className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-ink-2">
                    {f.slice(0, 8)}
                  </code>
                ))}
                {(sig.fingerprints?.length ?? 0) > 12 && <span className="text-xs text-ink-3">+{sig.fingerprints!.length - 12} more</span>}
                {!sig.fingerprints?.length && <span className="text-ink-3">None</span>}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-ink-3">Never-seen email domains ({sig.new_email_domains?.length ?? 0})</dt>
              <dd className="mt-1.5 flex flex-wrap gap-1.5">
                {(sig.new_email_domains ?? []).slice(0, 10).map((d) => (
                  <span key={d} className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-ink-2">
                    {d}
                  </span>
                ))}
                {!sig.new_email_domains?.length && <span className="text-ink-3">None</span>}
              </dd>
            </div>
          </dl>
        </Card>
      </div>

      <section className="panel">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 pt-3">
          <div role="tablist" aria-label="Incident documents" className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                role="tab"
                id={`tab-${t.key}`}
                aria-selected={tab === t.key}
                aria-controls={`panel-${t.key}`}
                onClick={() => setTab(t.key)}
                className={`-mb-px border-b-2 px-3 py-2.5 text-sm font-medium transition ${
                  tab === t.key ? "border-guard text-ink-1" : "border-transparent text-ink-3 hover:text-ink-1"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {tab === "evidence" && evidence && downloadUrl && (
            <div className="flex gap-1 pb-2">
              <Button variant="ghost" size="sm" onClick={copy}>
                <Copy className="size-3.5" /> {copied ? "Copied" : "Copy"}
              </Button>
              <a
                href={downloadUrl}
                download={`checkout-guard-incident-${inc.id}.md`}
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-ink-2 hover:bg-gray-100 hover:text-ink-1"
              >
                <Download className="size-3.5" /> Download
              </a>
            </div>
          )}
        </div>
        <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} className="p-6">
          {tab === "report" && (inc.report?.report_md ? <Markdown>{inc.report.report_md}</Markdown> : <p className="text-sm text-ink-3">The report is written when the incident closes.</p>)}
          {tab === "evidence" && (evidence ? <Markdown>{evidence}</Markdown> : <p className="text-sm text-ink-3">No evidence pack yet.</p>)}
          {tab === "timeline" && <Timeline items={inc.timeline} />}
          {inc.report?.model_id && tab !== "timeline" && (
            <p className="mt-5 border-t border-line pt-3 text-xs text-ink-3">
              Worded by {inc.report.model_id === "template" ? "a fixed template (AI unavailable)" : inc.report.model_id}. Every number comes from Checkout Guard&apos;s
              records, not the AI.
            </p>
          )}
        </div>
      </section>
    </>
  );
}

const ACTOR: Record<string, { icon: LucideIcon; color: string; label: string }> = {
  detector: { icon: Eye, color: "text-sky-700 bg-sky-50", label: "Detector" },
  ai: { icon: Sparkles, color: "text-violet-700 bg-violet-50", label: "AI" },
  policy: { icon: Gavel, color: "text-amber-700 bg-amber-50", label: "Policy" },
  executor: { icon: Zap, color: "text-emerald-700 bg-emerald-50", label: "Executor" },
  owner: { icon: User, color: "text-pink-700 bg-pink-50", label: "Owner" },
  system: { icon: Settings2, color: "text-ink-2 bg-gray-100", label: "System" },
};

const EVENT_LABEL: Record<string, string> = {
  "incident.opened": "Incident opened",
  "incident.closed": "Incident closed",
  "incident.closed_by_owner": "Stood down by the owner",
  verdict: "AI verdict",
  rules_first: "Fixed rules acted before the AI answered",
  decided: "Policy decided",
  enforced: "Policy re-applied to new traffic",
  "action.executed": "Action executed",
  "action.awaiting_approval": "Action waiting for approval",
  "action.failed": "Action failed",
  "action.rejected": "Action rejected",
  "ai.usage": "AI usage recorded",
  "ai.refresh_failed": "AI re-check failed, verdict kept",
  "ai.kept_verdict_dropped": "Kept verdict dropped: new signals contradict it",
  "refund.executed": "Refund approved by the owner",
  "refund.rejected": "Refund kept by the owner",
};

function summarize(data: Record<string, unknown>): string {
  return Object.entries(data)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length > 6))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" ")
    .slice(0, 180);
}

function Timeline({ items }: { items: IncidentDetail["timeline"] }) {
  if (!items.length) return <p className="text-sm text-ink-3">No events yet.</p>;
  return (
    <ol className="relative space-y-3 before:absolute before:top-2 before:bottom-2 before:left-[0.95rem] before:w-px before:bg-gray-100">
      {items.map((t, i) => {
        const a = ACTOR[t.actor] ?? ACTOR.system;
        const Icon = a.icon;
        return (
          <li key={i} className="relative flex gap-3">
            <span className={`relative flex size-8 shrink-0 items-center justify-center rounded-full ${a.color}`} title={a.label}>
              <Icon className="size-4" />
            </span>
            <div className="min-w-0 pt-1">
              <p className="text-sm text-ink-1">
                <span className="mr-2 font-mono text-xs text-ink-3 tabular-nums">{clock(t.ts)}</span>
                {EVENT_LABEL[t.event] ?? t.event}
                <span className="ml-2 text-xs text-ink-3">{a.label}</span>
              </p>
              <p className="truncate font-mono text-[11px] text-ink-3">{summarize(t.data)}</p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
