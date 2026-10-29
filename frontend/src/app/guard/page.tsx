"use client";

import { ArrowRight, ListChecks, Sparkles } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import useSWR, { useSWRConfig } from "swr";
import { DevicesList, useDeviceCounts } from "@/components/guard/DevicesCard";
import { GuardSwitches } from "@/components/guard/GuardSwitches";
import { DisputesCard } from "@/components/guard/Disputes";
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
        <Skeleton className="h-[240px]" />
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
          label="Attempts, last 5 min"
          value={live ? live["5m"].attempts : "–"}
          hint={live ? vsNormal(live["5m"].attempts_vs_normal, `${s.attempts} in 24 h`) : waiting}
          tone={live && (live["5m"].attempts_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat
          label="Decline rate, last 15 min"
          value={live ? `${Math.round(live["15m"].decline_rate * 100)}%` : "–"}
          hint={live ? vsNormal(live["15m"].decline_rate_vs_normal, `${s.declined} declined in 24 h`) : waiting}
          tone={live && (live["15m"].decline_rate_vs_normal ?? 0) >= 3 ? "bad" : undefined}
        />
        <Stat label="Stopped by the guard, 15 min" value={live ? live["15m"].stopped : "–"} hint={`${s.stopped_by_guard} in 24 h`} />
        <Stat
          label="Refunded, 24 h"
          value={money(s.refunded_amount)}
          hint={`${s.refunded} payment${s.refunded === 1 ? "" : "s"} returned before a dispute`}
        />
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-3">
        <Card title="Checkout traffic" subtitle={`Last ${CHART_MINUTES} minutes, ${BUCKET_S}-second bars`} className="xl:col-span-2" bodyClassName="px-3 pt-3 pb-2">
          <ActivityChart points={series ?? []} minutes={CHART_MINUTES} bucketS={BUCKET_S} incident={shown} />
        </Card>
        <Card
          title="Verdict"
          subtitle="The scorecard decides; the AI explains"
          action={
            shown && (
              <Link href={`/guard/incidents/${shown.id}`} className="inline-flex items-center gap-1 text-[13px] font-medium whitespace-nowrap text-guard hover:underline">
                Full report <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            )
          }
        >
          {shown ? (
            <VerdictCard incident={shown} compact />
          ) : (
            <Empty icon={Sparkles}>
              Nothing unusual yet.
              <span className="text-[13px]">Run a card-testing attack from the sidebar to see a verdict.</span>
            </Empty>
          )}
        </Card>
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-3">
        <Card title="What Checkout Guard did" subtitle="Each action, the rule that allowed it, and the PayPal call" className="xl:col-span-2">
          {shown ? (
            <DecisionTrace incident={shown} onChange={() => mutate(`/dashboard/incidents/${shown.id}`)} />
          ) : (
            <Empty icon={ListChecks}>No incidents yet. Every defence will be listed here with its reason.</Empty>
          )}
        </Card>
        <Card title="Owner alert" subtitle="What the shop owner is sent">
          <AlertPhone text={shown?.report?.alert_text} at={shown?.report?.generated_at} history={alerts} />
        </Card>
      </div>

      <Card title="Checkout attempts" subtitle="Every attempt the guard checked, newest first" bodyClassName="p-0">
        <AttemptsGrid rows={attempts ?? []} />
      </Card>

      <DisputesCard />

      <div className="grid gap-6 xl:grid-cols-3">
        <Card title="Guard switches">
          <GuardSwitches switches={summary.switches} />
        </Card>
        <Card
          title={`Blocked devices${devices ? ` (${devices.blocked})` : ""}`}
          subtitle="Blocks expire after 24 h. Unblocking also trusts the device."
          className="xl:col-span-2"
          action={devices && devices.trusted > 0 ? <span className="text-[13px] whitespace-nowrap text-ink-3">{devices.trusted} trusted</span> : undefined}
        >
          <DevicesList />
        </Card>
      </div>
    </>
  );
}

function vsNormal(multiplier: number | null, fallback: string): string {
  return multiplier ? `${times(multiplier)} normal · ${fallback}` : fallback;
}
