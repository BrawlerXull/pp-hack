"use client";

import { Activity, ArrowRight, CircleDollarSign, ListChecks, ShieldCheck, Smartphone, Sparkles, Table2, Timer, Waypoints } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import useSWR, { useSWRConfig } from "swr";
import { AlertPhone, DecisionTrace, VerdictCard } from "@/components/guard/incident";
import { StatusHero } from "@/components/guard/StatusHero";
import { Card, Empty, Skeleton, Stat } from "@/components/guard/ui";
import type { Attempt, IncidentDetail, IncidentSummary, Summary, TimeseriesPoint } from "@/lib/api";
import { duration, money } from "@/lib/format";

// AG Grid / AG Charts render in the browser only.
const AttemptsGrid = dynamic(() => import("@/components/guard/AttemptsGrid"), { ssr: false, loading: () => <Skeleton className="h-[500px]" /> });
const ActivityChart = dynamic(() => import("@/components/guard/ActivityChart"), { ssr: false, loading: () => <Skeleton className="h-[300px]" /> });

const CHART_MINUTES = 20;

export default function WarRoom() {
  const { mutate } = useSWRConfig();
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const { data: attempts } = useSWR<Attempt[]>("/dashboard/attempts?minutes=120&limit=1000", { refreshInterval: 15_000 });
  const { data: series } = useSWR<TimeseriesPoint[]>(`/dashboard/timeseries?minutes=${CHART_MINUTES}`, { refreshInterval: 15_000 });
  const openId = summary?.open_incident?.id;
  const { data: incident } = useSWR<IncidentDetail>(openId ? `/dashboard/incidents/${openId}` : null, { refreshInterval: 8_000 });
  const { data: recent } = useSWR<IncidentSummary[]>("/dashboard/incidents?limit=1");
  const lastClosedId = !openId && recent?.[0]?.status === "closed" ? recent[0].id : null;
  const { data: lastClosed } = useSWR<IncidentDetail>(lastClosedId ? `/dashboard/incidents/${lastClosedId}` : null);
  const shown = incident ?? lastClosed;

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
  const firstDefence = shown?.actions.find((a) => a.status === "executed" && a.type !== "NOTIFY");
  const contained = shown && firstDefence ? new Date(firstDefence.created_at).getTime() - new Date(shown.opened_at).getTime() : null;

  return (
    <>
      <StatusHero summary={summary} incident={shown} />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Checkout attempts · 24 h" icon={Activity} value={s.attempts.toLocaleString()} hint={`${s.captured} paid · ${s.declined} declined by banks`} />
        <Stat
          label="Stopped by Checkout Guard"
          icon={ShieldCheck}
          value={s.stopped_by_guard.toLocaleString()}
          hint="Blocked, human check or 3-D Secure"
          tone={s.stopped_by_guard ? "good" : undefined}
        />
        <Stat
          label="Refunded before dispute"
          icon={CircleDollarSign}
          value={money(s.refunded_amount)}
          hint={`${s.refunded} test payment${s.refunded === 1 ? "" : "s"} returned to the cardholder`}
          tone={s.refunded ? "warn" : undefined}
        />
        <Stat
          label="Time to first defence"
          icon={Timer}
          value={contained !== null ? duration(contained) : "–"}
          hint={contained !== null ? (shown?.status === "open" ? "This incident" : "Last incident") : "No incidents yet"}
          tone={contained !== null ? "info" : undefined}
        />
      </div>

      <div className="grid gap-6 xl:grid-cols-3">
        <Card
          title="Checkout traffic"
          subtitle={`Every attempt in the last ${CHART_MINUTES} minutes, by what happened to it. Markers show when each defence fired.`}
          icon={Waypoints}
          className="xl:col-span-2"
          bodyClassName="px-3 pb-3"
        >
          <ActivityChart points={series ?? []} minutes={CHART_MINUTES} incident={shown} />
        </Card>
        <Card
          title="AI analyst"
          subtitle="Attack, real rush or PayPal outage?"
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
          <AlertPhone text={shown?.report?.alert_text} at={shown?.report?.generated_at} />
        </Card>
      </div>

      <Card title="Live checkout attempts" subtitle="Every attempt the guard checked, newest first" icon={Table2}>
        <AttemptsGrid rows={attempts ?? []} />
      </Card>
    </>
  );
}
