"use client";

import { AllCommunityModule, ModuleRegistry, type AgChartOptions } from "ag-charts-community";
import { AgCharts } from "ag-charts-react";
import { useEffect, useMemo, useState } from "react";
import type { Outcome, TimeseriesPoint } from "@/lib/api";
import { OUTCOME_META } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

const SERIES: Outcome[] = ["captured", "declined", "sca_required", "challenged", "blocked", "error", "refunded"];

const currentMinute = () => Math.floor(Date.now() / 60000) * 60000;

export default function ActivityChart({ points, minutes }: { points: TimeseriesPoint[]; minutes: number }) {
  const [end, setEnd] = useState(currentMinute);
  useEffect(() => {
    const id = window.setInterval(() => setEnd(currentMinute()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  const data = useMemo(() => {
    // Fill empty minutes so quiet periods show as gaps rather than collapsing the axis.
    const byMinute = new Map(points.map((p) => [new Date(p.minute).getTime(), p]));
    return Array.from({ length: minutes }, (_, i) => {
      const t = end - (minutes - 1 - i) * 60000;
      const p = byMinute.get(t);
      return { time: new Date(t), ...Object.fromEntries(SERIES.map((s) => [s, p?.[s] ?? 0])) };
    });
  }, [points, minutes, end]);

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
        x: { type: "time", position: "bottom", label: { format: "%H:%M", fontSize: 11, color: "#64748b" }, gridLine: { enabled: false } },
        y: { type: "number", position: "left", title: { text: "Attempts / min", fontSize: 11, color: "#64748b" }, label: { fontSize: 11, color: "#64748b" } },
      },
      legend: { position: "bottom", item: { label: { fontSize: 11, color: "#475569" }, marker: { size: 8 } } },
    }),
    [data],
  );

  return <AgCharts options={options} style={{ height: 280 }} />;
}
