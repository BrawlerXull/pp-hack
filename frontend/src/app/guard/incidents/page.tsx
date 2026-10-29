"use client";

import { Bot, ChevronRight, Siren, Sparkles } from "lucide-react";
import Link from "next/link";
import useSWR from "swr";
import { Badge, Card, Empty, PageHeader, Skeleton } from "@/components/guard/ui";
import type { IncidentSummary } from "@/lib/api";
import { CLASSIFICATION_LABEL, TRIGGER_LABEL, classificationTone, duration, timeAgo } from "@/lib/format";
import { useNow } from "@/lib/now";

const ACCENT = { bad: "bg-red-400", info: "bg-sky-400", warn: "bg-amber-400", good: "bg-emerald-400", neutral: "bg-ink-3" };

export default function IncidentsPage() {
  const { data } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=100");
  const now = useNow();
  return (
    <>
      <PageHeader title="Incidents" subtitle="Every time the detector saw something unusual: what the AI concluded and what Checkout Guard did about it." />
      <Card bodyClassName="p-2">
        {!data ? (
          <Skeleton className="h-40" />
        ) : data.length === 0 ? (
          <Empty icon={Siren}>No incidents yet. Run a simulated attack from the demo bar to see one.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {data.map((i) => {
              const tone = classificationTone(i.classification);
              const end = i.closed_at ? new Date(i.closed_at).getTime() : now;
              return (
                <li key={i.id}>
                  <Link href={`/guard/incidents/${i.id}`} className="group flex items-center gap-4 rounded-xl px-3 py-3.5 transition hover:bg-gray-50">
                    <span className={`h-10 w-1 shrink-0 rounded-full ${ACCENT[tone]} ${i.status === "open" ? "live-dot" : "opacity-60"}`} aria-hidden />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-ink-1">{i.classification ? CLASSIFICATION_LABEL[i.classification] : "Analysing…"}</span>
                        {i.status === "open" ? <Badge tone={tone}>Active</Badge> : <Badge>Closed</Badge>}
                        {i.confidence !== null && i.ai_available && (
                          <span className="flex items-center gap-1 text-xs text-ink-3">
                            <Sparkles className="size-3 text-violet-700" /> {Math.round(i.confidence * 100)}% sure
                          </span>
                        )}
                        {!i.ai_available && i.classification && (
                          <span className="flex items-center gap-1 text-xs text-ink-3">
                            <Bot className="size-3" /> fixed rules
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 truncate text-sm text-ink-2">{i.summary ?? "Waiting for the AI analyst…"}</p>
                      <p className="mt-0.5 text-xs text-ink-3">
                        {timeAgo(i.opened_at)} · lasted {duration(end - new Date(i.opened_at).getTime())}
                        {i.triggers.length > 0 && ` · ${i.triggers.map((t) => TRIGGER_LABEL[t] ?? t).join(", ")}`}
                      </p>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-ink-3 transition group-hover:translate-x-0.5 group-hover:text-ink-1" />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}
