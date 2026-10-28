"use client";

import { Activity, ArrowRight, Ban, CircleDollarSign, ListChecks, Percent, ShieldCheck, Smartphone, Sparkles, Table2, ToggleRight, Waypoints } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import useSWR, { useSWRConfig } from "swr";
import { DevicesList, useDeviceCounts } from "@/components/guard/DevicesCard";
import { GuardSwitches } from "@/components/guard/GuardSwitches";
import { AlertPhone, DecisionTrace, VerdictCard } from "@/components/guard/incident";
import { StatusHero } from "@/components/guard/StatusHero";
import { Card, Empty, Skeleton, Stat } from "@/components/guard/ui";
import type { AlertRow, Attempt, IncidentDetail, IncidentSummary, Summary, TimeseriesPoint } from "@/lib/api";
import { money, times } from "@/lib/format";

// AG Grid / AG Charts render in the browser only.
const AttemptsGrid = dynamic(() => import("@/components/guard/AttemptsGrid"), { ssr: false, loading: () => <Skeleton className="h-[500px]" /> });
const ActivityChart = dynamic(() => import("@/components/guard/ActivityChart"), { ssr: false, loading: () => <Skeleton className="h-[300px]" /> });

const CHART_MINUTES = 10;
const BUCKET_S = 10; // an attack's first seconds are visible, not averaged into a minute

export default function WarRoom() {
  const { mutate } = useSWRConfig();
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const { data: attempts } = useSWR<Attempt[]>("/dashboard/attempts?minutes=120&limit=1000", { refreshInterval: 15_000 });
  const { data: series } = useSWR<TimeseriesPoint[]>(`/dashboard/timeseries?minutes=${CHART_MINUTES}&bucket_s=${BUCKET_S}`, { refreshInterval: 10_000 });
  const openId = summary?.open_incident?.id;
  const { data: incident } = useSWR<IncidentDetail>(openId ? `/dashboard/incidents/${openId}` : null, { refreshInterval: 8_000 });
  const { data: recent } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=1");
  const lastClosedId = !openId && recent?.[0]?.status === "closed" ? recent[0].id : null;
  const { data: lastClosed } = useSWR<IncidentDetail>(lastClosedId ? `/dashboard/incidents/${lastClosedId}` : null);
  const shown = incident ?? lastClosed;
  // The phone sits next to the shown incident: only that incident's alerts.
  const { data: alerts } = useSWR<AlertRow[]>(shown ? `/dashboard/alerts?limit=6&incident_id=${shown.id}` : null, { refreshInterval: 30_000 });
  const devices = useDeviceCounts();

  if (!summary) {
    return (
      <>
        <Skeleton className="h-[290px]" />
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      </>
    );
  }
  const s = summary.last_24h;
  const live = summary.system.live;
  const offline = !summary.system.worker.alive;
  const waiting = offline ? "detector stopped" : "waiting for the detector";

  return (
    <>
      <StatusHero summary={summary} incident={shown} />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Attempts · last 5 min"
          icon={Activity}
          value={live ? live["5m"].attempts : "–"}
          hint={live ? vsNormal(live["5m"].attempts_vs_normal, `${s.attempts} in 24 h`) : waiting}
          tone={live && (live["5m"].attempts_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat
          label="Decline rate · last 15 min"
          icon={Percent}
          value={live ? `${Math.round(live["15m"].decline_rate * 100)}%` : "–"}
          hint={live ? vsNormal(live["15m"].decline_rate_vs_normal, `${s.declined} declined by banks in 24 h`) : waiting}
          tone={live && (live["15m"].decline_rate_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat
          label="Stopped by guard · 15 min"
          icon={ShieldCheck}
          value={live ? live["15m"].stopped : "–"}
          hint={`${s.stopped_by_guard} in 24 h · blocked, human check or 3-D Secure`}
          tone={live?.["15m"].stopped ? "good" : undefined}
        />
        <Stat
          label="Refunded before dispute · 24 h"
          icon={CircleDollarSign}
          value={money(s.refunded_amount)}
          hint={`${s.refunded} test payment${s.refunded === 1 ? "" : "s"} returned to the cardholder`}
          tone={s.refunded ? "warn" : undefined}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card
          title="Checkout traffic"
          subtitle={`Last ${CHART_MINUTES} minutes in ${BUCKET_S}-second bars. Markers show when the attack was detected and when each defence fired.`}
          icon={Waypoints}
          className="xl:col-span-2"
          bodyClassName="px-3 pb-3"
        >
          <ActivityChart points={series ?? []} minutes={CHART_MINUTES} bucketS={BUCKET_S} incident={shown} />
        </Card>
        <Card
          title="Verdict"
          subtitle="Attack, real rush or PayPal outage? The scorecard decides, the AI explains"
          icon={Sparkles}
          action={
            shown && (
              <Link href={`/guard/incidents/${shown.id}`} className="flex shrink-0 items-center gap-1 text-xs font-medium whitespace-nowrap text-guard hover:underline">
                Full report <ArrowRight className="size-3.5" />
              </Link>
            )
          }
        >
          {shown ? (
            <VerdictCard incident={shown} compact />
          ) : (
            <Empty icon={Sparkles}>
              The analyst is only called when the detector sees something unusual.
              <span className="text-xs">Start a simulation from the demo bar to watch it work.</span>
            </Empty>
          )}
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card
          title="From verdict to PayPal"
          subtitle="Each action, the fixed rule that allowed it, and how PayPal carried it out"
          icon={ListChecks}
          className="xl:col-span-2"
        >
          {shown ? (
            <DecisionTrace incident={shown} onChange={() => mutate(`/dashboard/incidents/${shown.id}`)} />
          ) : (
            <Empty icon={ListChecks}>No incidents yet. Every defence will be listed here with its reason.</Empty>
          )}
        </Card>
        <Card title="What the owner sees" subtitle="One calm message instead of a 3 a.m. scramble" icon={Smartphone}>
          <AlertPhone text={shown?.report?.alert_text} at={shown?.report?.generated_at} history={alerts} />
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Guard switches" subtitle="The two an owner reaches for mid-incident" icon={ToggleRight}>
          <GuardSwitches switches={summary.switches} />
        </Card>
        <Card
          title={`Blocked devices${devices ? ` (${devices.blocked})` : ""}`}
          subtitle="Blocks expire after 24 h. Unblocking also trusts the device, so it isn't blocked or refunded again."
          icon={Ban}
          className="xl:col-span-2"
          action={devices && devices.trusted > 0 ? <span className="text-xs whitespace-nowrap text-ink-3">{devices.trusted} trusted by you</span> : undefined}
        >
          <DevicesList />
        </Card>
      </div>

      <Card title="Live checkout attempts" subtitle="Every attempt the guard checked, newest first" icon={Table2}>
        <AttemptsGrid rows={attempts ?? []} />
      </Card>
    </>
  );
}

function vsNormal(multiplier: number | null, fallback: string): string {
  return multiplier !== null ? `${times(multiplier)} vs normal · ${fallback}` : fallback;
}
