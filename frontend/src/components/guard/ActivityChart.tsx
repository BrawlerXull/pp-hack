"use client";

import { AllCommunityModule, ModuleRegistry, type AgCartesianChartOptions, type AgCartesianCrossLineOptions } from "ag-charts-community";
import { AgCharts } from "ag-charts-react";
import { useEffect, useMemo, useState } from "react";
import type { ActionRow, IncidentDetail, TimeseriesPoint } from "@/lib/api";
import { SERIES, minuteLabel } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

const MINUTE = 60_000;
const currentMinute = () => Math.floor(Date.now() / MINUTE) * MINUTE;

const INK_2 = "#a9b3c7";
const INK_3 = "#7f8aa3";
const GRID = "rgba(255,255,255,0.06)";
const SURFACE = "#0e1628";

const MARKER_LABEL: Partial<Record<ActionRow["type"], (a: ActionRow[]) => string>> = {
  SCA_ON: () => "3DS on",
  BLOCK_FP: (a) => `${a.reduce((n, x) => n + ((x.params.fingerprints as string[] | undefined)?.length ?? 0), 0)} blocked`,
  HOLD_SKU: () => "item held",
  REFUND: (a) => `${a.length} refunded`,
  RELAX: () => "stood down",
  DEESCALATE: () => "stood down",
};

/**
 * Per-minute checkout outcomes, stacked, with the incident shaded and each defence marked where it
 * happened, so the attack, the response and the drop-off read as one story.
 */
export default function ActivityChart({
  points,
  minutes,
  incident,
  height = 300,
}: {
  points: TimeseriesPoint[];
  minutes: number;
  incident?: IncidentDetail | null;
  height?: number;
}) {
  const [end, setEnd] = useState(currentMinute);
  useEffect(() => {
    const id = window.setInterval(() => setEnd(currentMinute()), 10_000);
    return () => window.clearInterval(id);
  }, []);

  const start = end - (minutes - 1) * MINUTE;

  const data = useMemo(() => {
    // Fill empty minutes so quiet periods show as gaps rather than collapsing the axis.
    const byMinute = new Map(points.map((p) => [new Date(p.minute).getTime(), p]));
    return Array.from({ length: minutes }, (_, i) => {
      const t = start + i * MINUTE;
      const p = byMinute.get(t);
      const row: Record<string, string | number> = { minute: minuteLabel(t) };
      for (const s of SERIES) row[s.key] = s.outcomes.reduce((n, o) => n + (p?.[o] ?? 0), 0);
      return row;
    });
  }, [points, minutes, start]);

  const empty = data.every((row) => SERIES.every((s) => !row[s.key]));

  const crossLines = useMemo(() => {
    if (!incident) return [];
    const inWindow = (iso: string) => {
      const t = Math.floor(new Date(iso).getTime() / MINUTE) * MINUTE;
      return t >= start && t <= end ? t : null;
    };
    const lines: AgCartesianCrossLineOptions[] = [];

    const opened = new Date(incident.opened_at).getTime();
    const closed = incident.closed_at ? new Date(incident.closed_at).getTime() : end;
    if (closed >= start) {
      const from = Math.max(Math.floor(opened / MINUTE) * MINUTE, start);
      const to = Math.min(Math.floor(closed / MINUTE) * MINUTE, end);
      lines.push({
        type: "range",
        range: [minuteLabel(from), minuteLabel(to)],
        fill: incident.classification === "card_testing" || incident.classification === "account_testing" ? "#ef4444" : "#4c8dff",
        fillOpacity: 0.08,
        strokeWidth: 0,
      });
    }

    // One marker per minute: merge actions that fired together ("3-D Secure on · 4 devices blocked").
    const byMinute = new Map<number, Map<ActionRow["type"], ActionRow[]>>();
    for (const a of incident.actions) {
      if (a.status !== "executed" || !MARKER_LABEL[a.type]) continue;
      const t = inWindow(a.created_at);
      if (t === null) continue;
      const types = byMinute.get(t) ?? new Map();
      types.set(a.type, [...(types.get(a.type) ?? []), a]);
      byMinute.set(t, types);
    }
    let i = 0;
    for (const [t, types] of [...byMinute.entries()].sort((a, b) => a[0] - b[0])) {
      const text = [...types.entries()].map(([type, acts]) => MARKER_LABEL[type]!(acts)).join(" · ");
      // Labels sit on whichever side of the marker has room, alternating top/bottom so neighbours don't collide.
      const side = (t - start) / (end - start) > 0.5 ? "left" : "right";
      lines.push({
        type: "line",
        value: minuteLabel(t),
        stroke: "#e7ecf6",
        strokeOpacity: 0.6,
        strokeWidth: 1,
        label: { text, position: `inside-${i++ % 2 ? "bottom" : "top"}-${side}`, color: "#e7ecf6", fontSize: 11, fontWeight: 600 },
      });
    }
    return lines;
  }, [incident, start, end]);

  const options = useMemo<AgCartesianChartOptions>(
    () => ({
      data,
      theme: "ag-default-dark",
      background: { visible: false },
      padding: { top: 12, right: 8, bottom: 4, left: 0 },
      series: SERIES.map((s) => ({
        type: "bar" as const,
        xKey: "minute",
        yKey: s.key,
        yName: s.label,
        stacked: true,
        fill: s.color,
        stroke: SURFACE,
        strokeWidth: 1,
        cornerRadius: 3,
      })),
      axes: {
        x: {
          type: "category",
          position: "bottom",
          paddingInner: 0.35,
          paddingOuter: 0.1,
          line: { stroke: "rgba(255,255,255,0.12)" },
          label: { fontSize: 11, color: INK_3, autoRotate: false },
          gridLine: { enabled: false },
          crossLines,
        },
        y: {
          type: "number",
          position: "left",
          title: { text: "Attempts per minute", fontSize: 11, color: INK_3 },
          min: 0,
          ...(empty ? { max: 10 } : {}),
          label: { fontSize: 11, color: INK_3 },
          gridLine: { style: [{ stroke: GRID, lineDash: [] }] },
          line: { enabled: false },
        },
      },
      legend: { position: "bottom", spacing: 16, item: { label: { fontSize: 12, color: INK_2 }, marker: { size: 9 }, padding: 12 } },
    }),
    [data, crossLines, empty],
  );

  return <AgCharts options={options} style={{ height }} />;
}
