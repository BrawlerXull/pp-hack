"use client";

import { ArrowLeft, Copy, Download, Loader2, RefreshCw, ShieldOff } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import useSWR from "swr";
import { DecisionStrip } from "@/components/guard/DecisionStrip";
import { ActionsList, AlertPhone, Markdown, VerdictCard } from "@/components/guard/incident";
import { Badge, Button, Card } from "@/components/guard/ui";
import { api, type AlertRow, type IncidentDetail } from "@/lib/api";
import { CLASSIFICATION_LABEL, clock } from "@/lib/format";

export default function IncidentPage() {
  const { id } = useParams<{ id: string }>();
  const key = `/dashboard/incidents/${id}`;
  const { data: inc, mutate } = useSWR<IncidentDetail>(key);
  const { data: alerts } = useSWR<AlertRow[]>(`/dashboard/alerts?limit=10&incident_id=${encodeURIComponent(id)}`);
  const [tab, setTab] = useState<"report" | "evidence" | "timeline">("report");
  const [busy, setBusy] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!inc) return <div className="h-40 animate-pulse rounded-2xl bg-slate-100" />;

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

  const evidence = inc.report?.evidence_md ?? "";
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/guard/incidents" className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100" aria-label="Back">
          <ArrowLeft className="size-5" />
        </Link>
        <h1 className="text-xl font-semibold text-slate-900">
          {inc.classification ? CLASSIFICATION_LABEL[inc.classification] : "Incident"}
        </h1>
        <Badge tone={inc.status === "open" ? "bad" : "neutral"}>{inc.status === "open" ? "Active" : "Closed"}</Badge>
        <span className="text-sm text-slate-500">
          {new Date(inc.opened_at).toLocaleString()} {inc.closed_at && `→ ${clock(inc.closed_at)}`}
        </span>
        <div className="ml-auto flex gap-2">
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
      {error && <p className="text-sm text-amber-700">{error}</p>}

      <DecisionStrip incident={inc} />

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Verdict (scorecard decides, AI explains)" className="xl:col-span-2">
          <VerdictCard incident={inc} />
        </Card>
        <div className="space-y-6">
          <Card title="What Checkout Guard did">
            <ActionsList incident={inc} onChange={() => mutate()} />
          </Card>
          <Card title="Owner alerts">
            <AlertPhone text={inc.report?.alert_text} at={inc.report?.generated_at} history={alerts} />
          </Card>
        </div>
      </div>

      <Card
        title={
          <div className="flex gap-1">
            {(["report", "evidence", "timeline"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`rounded-md px-3 py-1 text-sm ${tab === t ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`}
              >
                {t === "report" ? "Morning report" : t === "evidence" ? "Evidence for PayPal" : "Audit timeline"}
              </button>
            ))}
          </div>
        }
        action={
          tab === "evidence" && evidence ? (
            <div className="flex gap-2">
              <Button
                variant="ghost"
                onClick={async () => {
                  await navigator.clipboard.writeText(evidence);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                }}
              >
                <Copy className="size-4" /> {copied ? "Copied" : "Copy"}
              </Button>
              <a
                href={URL.createObjectURL(new Blob([evidence], { type: "text/markdown" }))}
                download={`checkout-guard-incident-${inc.id}.md`}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100"
              >
                <Download className="size-4" /> Download
              </a>
            </div>
          ) : undefined
        }
      >
        {tab === "report" && (inc.report?.report_md ? <Markdown>{inc.report.report_md}</Markdown> : <p className="text-sm text-slate-500">No report yet.</p>)}
        {tab === "evidence" && (evidence ? <Markdown>{evidence}</Markdown> : <p className="text-sm text-slate-500">No evidence pack yet.</p>)}
        {tab === "timeline" && (
          <ol className="space-y-2 text-sm">
            {inc.timeline.map((t, i) => (
              <li key={i} className="flex gap-3">
                <span className="w-20 shrink-0 font-mono text-xs text-slate-500">{clock(t.ts)}</span>
                <Badge tone={t.actor === "ai" ? "info" : t.actor === "owner" ? "good" : "neutral"}>{t.actor}</Badge>
                <span className="text-slate-800">{t.event}</span>
                <span className="truncate font-mono text-xs text-slate-500">{summarize(t.data)}</span>
              </li>
            ))}
          </ol>
        )}
        {inc.report?.model_id && tab !== "timeline" && (
          <p className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-400">
            Written by {inc.report.model_id === "template" ? "fixed template (AI unavailable)" : inc.report.model_id}. Every number comes from Checkout Guard&apos;s records, not the AI.
          </p>
        )}
      </Card>
    </>
  );
}

function summarize(data: Record<string, unknown>): string {
  return Object.entries(data)
    .filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length > 6))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" ")
    .slice(0, 160);
}
