"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useMemo } from "react";
import useSWR, { useSWRConfig } from "swr";
import type { ChartMarker } from "@/components/guard/ActivityChart";
import { DecisionStrip } from "@/components/guard/DecisionStrip";
import { DevicesList, useDeviceCounts } from "@/components/guard/DevicesCard";
import { GuardSwitches } from "@/components/guard/GuardSwitches";
import { ActionsList, AlertPhone, StatusBanner, VerdictCard } from "@/components/guard/incident";
import { QuickRun } from "@/components/guard/QuickRun";
import { Badge, Card, Empty, Stat } from "@/components/guard/ui";
import type { AlertRow, Attempt, IncidentDetail, IncidentSummary, Summary, TimeseriesPoint } from "@/lib/api";
import { money, times, timeAgo } from "@/lib/format";

// AG Grid / AG Charts render in the browser only.
const AttemptsGrid = dynamic(() => import("@/components/guard/AttemptsGrid"), { ssr: false, loading: () => <div className="h-[420px] animate-pulse rounded-xl bg-slate-100" /> });
const ActivityChart = dynamic(() => import("@/components/guard/ActivityChart"), { ssr: false, loading: () => <div className="h-[280px] animate-pulse rounded-xl bg-slate-100" /> });

const CHART_MINUTES = 10;
const BUCKET_S = 10; // an attack's first seconds are visible, not averaged into a minute

export default function WarRoom() {
  const { mutate } = useSWRConfig();
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const { data: attempts } = useSWR<Attempt[]>("/dashboard/attempts?minutes=120&limit=1000", { refreshInterval: 15_000 });
  const { data: series } = useSWR<TimeseriesPoint[]>(`/dashboard/timeseries?minutes=${CHART_MINUTES}&bucket_s=${BUCKET_S}`, { refreshInterval: 10_000 });
  const openId = summary?.open_incident?.id;
  const { data: incident } = useSWR<IncidentDetail>(openId ? `/dashboard/incidents/${openId}` : null);
  const { data: recent } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=1");
  const lastClosedId = !openId && recent?.[0]?.status === "closed" ? recent[0].id : null;
  const { data: lastClosed } = useSWR<IncidentDetail>(lastClosedId ? `/dashboard/incidents/${lastClosedId}` : null);
  const shown = incident ?? lastClosed;
  // The phone sits next to the shown incident: only that incident's alerts.
  const { data: alerts } = useSWR<AlertRow[]>(shown ? `/dashboard/alerts?limit=6&incident_id=${shown.id}` : null, { refreshInterval: 30_000 });
  const devices = useDeviceCounts();
  const markers = useMemo(() => (shown ? incidentMarkers(shown) : []), [shown]);

  if (!summary) return <div className="h-32 animate-pulse rounded-2xl bg-slate-100" />;
  const s = summary.last_24h;
  const live = summary.system.live;
  const offline = !summary.system.worker.alive;

  return (
    <>
      <StatusBanner summary={summary} />

      {shown && (
        <section className="space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            {shown.status === "open" ? "Current incident" : `Last incident · closed ${timeAgo(shown.closed_at!)}`}: how Checkout Guard got here
          </p>
          <DecisionStrip incident={shown} />
        </section>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Attempts · last 5 min"
          value={live ? live["5m"].attempts : "–"}
          hint={live ? vsNormal(live["5m"].attempts_vs_normal, `${s.attempts} in 24 h`) : offline ? "detector stopped" : "waiting for the detector"}
          tone={live && (live["5m"].attempts_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat
          label="Decline rate · last 15 min"
          value={live ? `${Math.round(live["15m"].decline_rate * 100)}%` : "–"}
          hint={live ? vsNormal(live["15m"].decline_rate_vs_normal, `${s.declined} declined in 24 h`) : undefined}
          tone={live && (live["15m"].decline_rate_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat
          label="Stopped by the guard · 15 min"
          value={live ? live["15m"].stopped : "–"}
          hint={`${s.stopped_by_guard} in 24 h · blocked, challenged or 3-D Secure`}
          tone={live?.["15m"].stopped ? "bad" : undefined}
        />
        <Stat label="Refunded before dispute · 24h" value={money(s.refunded_amount)} hint={`${s.refunded} test payments`} tone={s.refunded ? "info" : undefined} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Guard switches">
          <GuardSwitches switches={summary.switches} />
        </Card>
        <Card title="Demo controls" className="lg:col-span-2">
          <QuickRun />
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card
          title={`Checkout activity · last ${CHART_MINUTES} min, ${BUCKET_S}-second bars`}
          action={shown?.detected_in_s != null && shown.status === "open" ? <Badge tone="info">Detected in {shown.detected_in_s} s</Badge> : undefined}
          className="xl:col-span-2"
        >
          <ActivityChart points={series ?? []} minutes={CHART_MINUTES} bucketS={BUCKET_S} markers={markers} />
        </Card>
        <Card title="What Meera's phone says">
          <AlertPhone text={shown?.report?.alert_text} at={shown?.report?.generated_at} history={alerts} />
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

      <Card
        title={`Blocked devices${devices ? ` (${devices.blocked})` : ""}`}
        action={devices && devices.trusted > 0 ? <span className="text-xs text-slate-500">{devices.trusted} trusted by you</span> : undefined}
      >
        <DevicesList />
      </Card>

      <Card title="Live checkout attempts" action={<span className="text-xs text-slate-500">{attempts?.length ?? 0} in the last 2 h</span>}>
        <AttemptsGrid rows={attempts ?? []} />
      </Card>
    </>
  );
}

function vsNormal(multiplier: number | null, fallback: string): string {
  return multiplier !== null ? `${times(multiplier)} vs normal · ${fallback}` : fallback;
}

/** Vertical lines on the chart: when the incident opened and when the first defences went on. */
function incidentMarkers(inc: IncidentDetail): ChartMarker[] {
  const first = (type: string) => inc.actions.filter((a) => a.type === type && a.status === "executed").map((a) => a.created_at).sort()[0];
  const sca = first("SCA_ON");
  const blocked = first("BLOCK_FP");
  return [
    { at: inc.opened_at, label: "Incident opened", color: "#dc2626" },
    ...(sca ? [{ at: sca, label: "3-D Secure on", color: "#2563eb" }] : []),
    ...(blocked ? [{ at: blocked, label: "Devices blocked", color: "#7c3aed" }] : []),
  ];
}
