"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import useSWR from "swr";
import { Badge, Card, Empty } from "@/components/guard/ui";
import type { IncidentSummary } from "@/lib/api";
import { CLASSIFICATION_LABEL, timeAgo } from "@/lib/format";

export default function IncidentsPage() {
  const { data } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=100");
  return (
    <>
      <h1 className="text-xl font-semibold text-slate-900">Incidents</h1>
      <Card>
        {!data ? (
          <div className="h-24 animate-pulse rounded bg-slate-100" />
        ) : data.length === 0 ? (
          <Empty>No incidents yet.</Empty>
        ) : (
          <ul className="-my-2 divide-y divide-slate-100">
            {data.map((i) => {
              const attack = i.classification === "card_testing" || i.classification === "account_testing";
              return (
                <li key={i.id}>
                  <Link href={`/guard/incidents/${i.id}`} className="flex items-center gap-4 py-3 hover:bg-slate-50">
                    <Badge tone={attack ? "bad" : i.classification === "genuine_rush" ? "info" : "warn"}>
                      {i.classification ? CLASSIFICATION_LABEL[i.classification] : "Analysing"}
                    </Badge>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-slate-800">{i.summary ?? "Waiting for the AI analyst…"}</p>
                      <p className="text-xs text-slate-500">
                        Opened {timeAgo(i.opened_at)} · {i.status === "open" ? "active" : `closed ${timeAgo(i.closed_at!)}`}
                        {i.confidence !== null && ` · ${Math.round(i.confidence * 100)}% confident`}
                      </p>
                    </div>
                    {i.status === "open" && <Badge tone="bad">Live</Badge>}
                    <ChevronRight className="size-4 text-slate-400" />
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
