"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import useSWR, { useSWRConfig } from "swr";
import { ActionsList, AlertPhone, StatusBanner, VerdictCard } from "@/components/guard/incident";
import { QuickRun } from "@/components/guard/QuickRun";
import { Card, Empty, Stat } from "@/components/guard/ui";
import type { Attempt, IncidentDetail, IncidentSummary, Summary, TimeseriesPoint } from "@/lib/api";
import { money, timeAgo } from "@/lib/format";

// AG Grid / AG Charts render in the browser only.
const AttemptsGrid = dynamic(() => import("@/components/guard/AttemptsGrid"), { ssr: false, loading: () => <div className="h-[420px] animate-pulse rounded-xl bg-slate-100" /> });
const ActivityChart = dynamic(() => import("@/components/guard/ActivityChart"), { ssr: false, loading: () => <div className="h-[280px] animate-pulse rounded-xl bg-slate-100" /> });

const CHART_MINUTES = 30;

export default function WarRoom() {
  const { mutate } = useSWRConfig();
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const { data: attempts } = useSWR<Attempt[]>("/dashboard/attempts?minutes=120&limit=1000", { refreshInterval: 15_000 });
  const { data: series } = useSWR<TimeseriesPoint[]>(`/dashboard/timeseries?minutes=${CHART_MINUTES}`, { refreshInterval: 15_000 });
  const openId = summary?.open_incident?.id;
  const { data: incident } = useSWR<IncidentDetail>(openId ? `/dashboard/incidents/${openId}` : null);
  const { data: recent } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=1");
  const lastClosedId = !openId && recent?.[0]?.status === "closed" ? recent[0].id : null;
  const { data: lastClosed } = useSWR<IncidentDetail>(lastClosedId ? `/dashboard/incidents/${lastClosedId}` : null);
  const shown = incident ?? lastClosed;

  if (!summary) return <div className="h-32 animate-pulse rounded-2xl bg-slate-100" />;
  const s = summary.last_24h;

  return (
    <>
      <StatusBanner summary={summary} />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Checkout attempts · 24h" value={s.attempts} hint={`${s.captured} paid · ${s.declined} declined`} />
        <Stat label="Stopped by the guard" value={s.stopped_by_guard} hint="blocked, challenged or 3-D Secure" tone={s.stopped_by_guard ? "bad" : undefined} />
        <Stat label="Refunded before dispute" value={money(s.refunded_amount)} hint={`${s.refunded} test payments`} tone={s.refunded ? "info" : undefined} />
        <Stat label="Devices blocked" value={summary.guard_state.blocked_devices} hint={summary.guard_state.mode === "sca_all" ? "3-D Secure on every card" : "normal checks"} />
      </div>

      <Card title="Demo controls">
        <QuickRun />
      </Card>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title={`Checkout activity · last ${CHART_MINUTES} min`} className="xl:col-span-2">
          <ActivityChart points={series ?? []} minutes={CHART_MINUTES} />
        </Card>
        <Card title="What Meera's phone says">
          <AlertPhone text={shown?.report?.alert_text} at={shown?.report?.generated_at} />
        </Card>
      </div>

      {shown ? (
        <div className="grid gap-6 xl:grid-cols-3">
          <Card
            title={shown.status === "open" ? "AI analyst verdict" : `Last incident · closed ${timeAgo(shown.closed_at!)}`}
            action={<Link href={`/guard/incidents/${shown.id}`} className="text-xs font-medium text-guard hover:underline">Full report →</Link>}
            className="xl:col-span-2"
          >
            <VerdictCard incident={shown} />
          </Card>
          <Card title="What Checkout Guard did">
            <ActionsList incident={shown} onChange={() => mutate(`/dashboard/incidents/${shown.id}`)} />
          </Card>
        </div>
      ) : (
        <Card title="Incidents">
          <Empty>No incidents yet. Try “Simulate card-testing attack” above.</Empty>
        </Card>
      )}

      <Card title="Live checkout attempts" action={<span className="text-xs text-slate-500">{attempts?.length ?? 0} in the last 2 h</span>}>
        <AttemptsGrid rows={attempts ?? []} />
      </Card>
    </>
  );
}
