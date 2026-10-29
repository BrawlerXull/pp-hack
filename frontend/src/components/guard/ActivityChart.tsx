"use client";

import { AllCommunityModule, ModuleRegistry, type AgCartesianChartOptions, type AgCartesianCrossLineOptions } from "ag-charts-community";
import { AgCharts } from "ag-charts-react";
import { useEffect, useMemo, useState } from "react";
import type { ActionRow, IncidentDetail, TimeseriesPoint } from "@/lib/api";
import { SERIES } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

// Light theme, same tokens as globals.css. Text stays in ink colours; only the bars carry series colour.
const INK_1 = "#101828";
const INK_2 = "#344054";
const INK_3 = "#5d6b82";
const GRID = "#eef0f3";
const SURFACE = "#ffffff";
const FONT = "Geist, 'Geist Fallback', ui-sans-serif, system-ui, sans-serif";

const currentBucket = (bucketMs: number) => Math.floor(Date.now() / bucketMs) * bucketMs;

const MARKER_LABEL: Partial<Record<ActionRow["type"], (a: ActionRow[]) => string>> = {
  SCA_ON: () => "3DS on",
  BLOCK_FP: (a) => `${a.reduce((n, x) => n + ((x.params.fingerprints as string[] | undefined)?.length ?? 0), 0)} blocked`,
  HOLD_SKU: () => "item held",
  REFUND: (a) => `${a.length} refunded`,
  VOID: (a) => `${a.length} voided`,
  RELAX: () => "stood down",
  DEESCALATE: () => "stood down",
};

/**
 * Checkout outcomes per bucket (10 s on the War Room, so an attack's first seconds aren't averaged into a minute),
 * stacked, with the incident shaded and a marker where it was detected and where each defence fired: the attack,
 * the response and the drop-off read as one story.
 */
export default function ActivityChart({
  points,
  minutes,
  bucketS = 60,
  incident,
  height = 300,
}: {
  points: TimeseriesPoint[];
  minutes: number;
  bucketS?: number;
  incident?: IncidentDetail | null;
  height?: number;
}) {
  const bucketMs = bucketS * 1000;
  const buckets = Math.round((minutes * 60) / bucketS);
  const [end, setEnd] = useState(() => currentBucket(bucketMs));
  useEffect(() => {
    const id = window.setInterval(() => setEnd(currentBucket(bucketMs)), Math.min(bucketMs, 15_000));
    return () => window.clearInterval(id);
  }, [bucketMs]);
  const start = end - (buckets - 1) * bucketMs;

  const data = useMemo(() => {
    // Fill empty buckets so quiet periods show as gaps rather than collapsing the axis. The backend's buckets
    // start on multiples of bucket_s since a UTC origin, the same grid as this one.
    const byBucket = new Map(points.map((p) => [new Date(p.t ?? p.minute).getTime(), p]));
    return Array.from({ length: buckets }, (_, i) => {
      const t = start + i * bucketMs;
      const p = byBucket.get(t);
      const row: Record<string, Date | number> = { time: new Date(t) };
      for (const s of SERIES) row[s.key] = s.outcomes.reduce((n, o) => n + (p?.[o] ?? 0), 0);
      return row;
    });
  }, [points, buckets, bucketMs, start]);

  const empty = data.every((row) => SERIES.every((s) => !row[s.key]));

  const crossLines = useMemo(() => {
    if (!incident) return [];
    const lines: AgCartesianCrossLineOptions[] = [];
    const inWindow = (t: number) => t >= start && t <= end + bucketMs;
    const side = (t: number) => ((t - start) / (end - start || 1) > 0.5 ? "left" : "right");

    const opened = new Date(incident.opened_at).getTime();
    const closed = incident.closed_at ? new Date(incident.closed_at).getTime() : end + bucketMs;
    if (closed >= start) {
      lines.push({
        type: "range",
        range: [new Date(Math.max(opened, start)), new Date(Math.min(closed, end + bucketMs))],
        fill: incident.classification === "card_testing" || incident.classification === "account_testing" ? "#d92d20" : "#0070e0",
        fillOpacity: 0.06,
        strokeWidth: 0,
      });
    }

    // One marker per bucket: actions that fired together share a label ("3DS on · 4 blocked").
    const groups = new Map<number, { detected?: boolean; types: Map<ActionRow["type"], ActionRow[]> }>();
    // Markers closer than MERGE_BUCKETS bars share one line and label, so labels never overlap.
    const MERGE_BUCKETS = 2;
    const bucketOf = (iso: string) => {
      const t = Math.floor(new Date(iso).getTime() / bucketMs) * bucketMs;
      const near = [...groups.keys()].find((k) => Math.abs(k - t) <= MERGE_BUCKETS * bucketMs);
      return near ?? t;
    };
    if (inWindow(opened)) groups.set(bucketOf(incident.opened_at), { detected: true, types: new Map() });
    for (const a of incident.actions) {
      if (a.status !== "executed" || !MARKER_LABEL[a.type]) continue;
      const t = bucketOf(a.created_at);
      if (!inWindow(t)) continue;
      const g = groups.get(t) ?? { types: new Map() };
      g.types.set(a.type, [...(g.types.get(a.type) ?? []), a]);
      groups.set(t, g);
    }
    let i = 0;
    for (const [t, g] of [...groups.entries()].sort((a, b) => a[0] - b[0])) {
      const parts = [...(g.detected ? ["detected"] : []), ...[...g.types.entries()].map(([type, acts]) => MARKER_LABEL[type]!(acts))];
      const text = parts.join(" · ");
      lines.push({
        type: "line",
        value: new Date(t + bucketMs / 2),
        stroke: g.detected && g.types.size === 0 ? "#b42318" : INK_2,
        strokeOpacity: 0.7,
        strokeWidth: 1,
        label: {
          text: text.charAt(0).toUpperCase() + text.slice(1),
          position: `inside-${i++ % 2 ? "bottom" : "top"}-${side(t)}`,
          color: INK_1,
          fontSize: 12,
          fontWeight: 600,
          fontFamily: FONT,
        },
      });
    }
    return lines;
  }, [incident, start, end, bucketMs]);

  const options = useMemo<AgCartesianChartOptions>(
    () => ({
      data,
      theme: "ag-default",
      background: { visible: false },
      padding: { top: 12, right: 8, bottom: 4, left: 0 },
      series: SERIES.map((s) => ({
        type: "bar" as const,
        xKey: "time",
        yKey: s.key,
        yName: s.label,
        stacked: true,
        fill: s.color,
        stroke: SURFACE,
        strokeWidth: 2, // 2px surface gap between stacked segments
        cornerRadius: 2,
      })),
      axes: {
        x: {
          type: "time",
          position: "bottom",
          line: { stroke: "#d0d5dd" },
          label: { format: "%H:%M", fontSize: 12, color: INK_3, fontFamily: FONT, autoRotate: false },
          gridLine: { enabled: false },
          crossLines,
        },
        y: {
          type: "number",
          position: "left",
          title: { text: bucketS < 60 ? `Attempts per ${bucketS} s` : "Attempts per minute", fontSize: 12, color: INK_3, fontFamily: FONT },
          min: 0,
          ...(empty ? { max: 10 } : {}),
          label: { fontSize: 12, color: INK_3, fontFamily: FONT },
          gridLine: { style: [{ stroke: GRID, lineDash: [] }] },
          line: { enabled: false },
        },
      },
      legend: { position: "bottom", spacing: 14, item: { label: { fontSize: 13, color: INK_2, fontFamily: FONT }, marker: { size: 10, shape: "square" } } },
    }),
    [data, crossLines, empty, bucketS],
  );

  return <AgCharts options={options} style={{ height }} />;
}
