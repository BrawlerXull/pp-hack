"use client";

import { AllCommunityModule, ModuleRegistry, type AgChartOptions } from "ag-charts-community";
import { AgCharts } from "ag-charts-react";
import { useEffect, useMemo, useState } from "react";
import type { Outcome, TimeseriesPoint } from "@/lib/api";
import { OUTCOME_META } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

const SERIES: Outcome[] = ["captured", "declined", "sca_required", "sca_failed", "challenged", "blocked", "error", "refunded"];

// A moment worth a vertical line on the chart: incident opened, 3-D Secure on, devices blocked.
// Callers memoize the array: a new one each render redraws the chart.
export type ChartMarker = { at: string; label: string; color: string };

const NO_MARKERS: ChartMarker[] = [];

const currentBucket = (bucketMs: number) => Math.floor(Date.now() / bucketMs) * bucketMs;

export default function ActivityChart({
  points,
  minutes,
  bucketS = 60,
  markers = NO_MARKERS,
}: {
  points: TimeseriesPoint[];
  minutes: number;
  bucketS?: number;
  markers?: ChartMarker[];
}) {
  const bucketMs = bucketS * 1000;
  const buckets = Math.round((minutes * 60) / bucketS);
  const [end, setEnd] = useState(() => currentBucket(bucketMs));
  useEffect(() => {
    const id = window.setInterval(() => setEnd(currentBucket(bucketMs)), Math.min(bucketMs, 15_000));
    return () => window.clearInterval(id);
  }, [bucketMs]);

  const data = useMemo(() => {
    // Fill empty buckets so quiet periods show as gaps rather than collapsing the axis. The backend's buckets
    // start on multiples of bucket_s since a UTC origin, the same grid as this one.
    const byBucket = new Map(points.map((p) => [new Date(p.t ?? p.minute).getTime(), p]));
    return Array.from({ length: buckets }, (_, i) => {
      const t = end - (buckets - 1 - i) * bucketMs;
      const p = byBucket.get(t);
      return { time: new Date(t), ...Object.fromEntries(SERIES.map((s) => [s, p?.[s] ?? 0])) };
    });
  }, [points, buckets, bucketMs, end]);

  const visible = useMemo(() => {
    const start = end - (buckets - 1) * bucketMs;
    return markers.filter((m) => {
      const t = new Date(m.at).getTime();
      return t >= start && t <= end + bucketMs;
    });
  }, [markers, end, buckets, bucketMs]);

  const options = useMemo<AgChartOptions>(
    () => ({
      data,
      background: { visible: false },
      padding: { top: 8, right: 8, bottom: 0, left: 0 },
      series: SERIES.map((s) => ({
        type: "bar" as const,
        xKey: "time",
        yKey: s,
        yName: OUTCOME_META[s].label,
        stacked: true,
        fill: OUTCOME_META[s].color,
        cornerRadius: 2,
      })),
      axes: {
        x: {
          type: "time",
          position: "bottom",
          label: { format: bucketS < 60 ? "%H:%M:%S" : "%H:%M", fontSize: 11, color: "#64748b" },
          gridLine: { enabled: false },
          crossLines: visible.map((m) => ({
            type: "line" as const,
            value: new Date(m.at),
            stroke: m.color,
            strokeWidth: 1.5,
            lineDash: [4, 3],
            label: { text: m.label, fontSize: 10, color: m.color, position: "top" as const },
          })),
        },
        y: {
          type: "number",
          position: "left",
          title: { text: bucketS < 60 ? `Attempts / ${bucketS} s` : "Attempts / min", fontSize: 11, color: "#64748b" },
          label: { fontSize: 11, color: "#64748b" },
        },
      },
      legend: { position: "bottom", item: { label: { fontSize: 11, color: "#475569" }, marker: { size: 8 } } },
    }),
    [data, bucketS, visible],
  );

  return <AgCharts options={options} style={{ height: 280 }} />;
}
