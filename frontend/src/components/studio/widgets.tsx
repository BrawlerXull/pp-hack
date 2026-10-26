"use client";

// Checkout Guard's own AG Studio widgets, registered next to Studio's built-in ones. Each reads Studio's data
// engine through widgetApi.getData, so page filters and cross-filters apply to it like any built-in widget.

import type { AgDefaultRegistry, AgDefaultWidgetDefinition, AgWidgetData, AgWidgetDataFormat, AgWidgetField, AgWidgetFieldReference, AgWidgetParams, AgWidgetsConfig } from "ag-studio";
import { createWidgets, type AgRegistry, type AgWidgetDefinition } from "ag-studio-react";
import { CircleCheck, CircleDashed, CircleX, Clock } from "lucide-react";
import { useEffect, useState } from "react";
import { ConfidenceRing } from "@/components/guard/incident";

type Format = AgWidgetDataFormat<Record<string, never>>;

type VerdictMapping = { verdict: AgWidgetFieldReference[]; confidence: AgWidgetFieldReference[]; time?: AgWidgetFieldReference[] };
interface VerdictData extends AgWidgetData<VerdictMapping, Format> {
  type: "guard-verdict";
}
export type VerdictDef = AgWidgetDefinition<"guard-verdict", VerdictData>;

type TimelineMapping = { time: AgWidgetFieldReference[]; action: AgWidgetFieldReference[]; status?: AgWidgetFieldReference[]; detail?: AgWidgetFieldReference[] };
interface TimelineData extends AgWidgetData<TimelineMapping, Format> {
  type: "defence-timeline";
}
export type TimelineDef = AgWidgetDefinition<"defence-timeline", TimelineData>;

export interface GuardRegistry extends AgRegistry {
  widgets: readonly (AgDefaultWidgetDefinition | VerdictDef | TimelineDef)[];
}

type Row = Record<string, unknown>;

/** A row's value for one requested field: rows are keyed by each field's `key` (aggregation included). */
function pick(row: Row, field: AgWidgetField, index: number): unknown {
  if (field.key in row) return row[field.key];
  const id = (field as { id?: string }).id;
  if (id && id in row) return row[id];
  return Object.values(row)[index];
}

const asText = (v: unknown) => (v === null || v === undefined || v === "" ? "" : String(v));
const asTime = (v: unknown) => (v instanceof Date ? v.getTime() : typeof v === "string" || typeof v === "number" ? new Date(v).getTime() : NaN);

/** Runs a flat query for the mapped fields whenever Studio re-renders the widget (filters, cross-filters, data). */
function useRows(params: AgWidgetParams<VerdictData> | AgWidgetParams<TimelineData>, fields: (AgWidgetField | undefined)[], ready: boolean) {
  const { widgetApi } = params;
  const [rows, setRows] = useState<unknown[][] | null>(null);
  const present = fields.filter((f): f is AgWidgetField => !!f);
  const key = JSON.stringify(present.map((f) => (f as { id?: string }).id));
  useEffect(() => {
    if (!ready) {
      widgetApi.setDisplayState("incompleteDataMapping");
      return;
    }
    let cancelled = false;
    widgetApi.setDisplayState("loading");
    widgetApi
      .getData({ kind: "flat", fields: present })
      .then((res) => {
        if (cancelled) return;
        const out = res.results.rows.map((row) => fields.map((f) => (f ? pick(row, f, present.indexOf(f)) : undefined)));
        setRows(out);
        widgetApi.setDisplayState(out.length ? "displayed" : "noData");
      })
      .catch(() => !cancelled && widgetApi.setDisplayState("noData"));
    return () => {
      cancelled = true;
    };
    // `params` changes identity whenever Studio wants the widget to refresh its data.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, key, ready]);
  return rows;
}

const VERDICT_TONE: Record<string, "bad" | "info" | "warn" | "neutral"> = {
  "Card-testing attack": "bad",
  "Stolen-account testing": "bad",
  "Genuine rush": "info",
  "PayPal processing issue": "warn",
  "Unclear activity": "warn",
};

function VerdictWidget(params: AgWidgetParams<VerdictData>) {
  const { verdict, confidence, time } = params.dataMapping;
  const rows = useRows(params, [verdict?.[0], confidence?.[0], time?.[0]], !!verdict?.length && !!confidence?.length);
  if (!rows?.length) return null;
  // The newest incident when a time field is mapped, otherwise the first group.
  const latest = time?.length ? [...rows].sort((a, b) => asTime(b[2]) - asTime(a[2]))[0] : rows[0];
  const label = asText(latest[0]) || "Unclassified";
  const conf = Math.max(0, Math.min(1, Number(latest[1]) || 0));
  return (
    <div className="flex h-full items-center gap-4 px-1 text-ink-1">
      <ConfidenceRing value={conf} tone={VERDICT_TONE[label] ?? "neutral"} size={68} />
      <div className="min-w-0">
        <p className="text-lg leading-tight font-semibold">{label}</p>
        <p className="mt-1 text-xs text-ink-3">
          {time?.length && rows.length > 1 ? `Latest of ${rows.length} incidents` : rows.length > 1 ? `${rows.length} verdicts` : "Checked against our own numbers"}
        </p>
      </div>
    </div>
  );
}

function StatusIcon({ status }: { status: string }) {
  if (status === "Done") return <CircleCheck className="size-4 text-emerald-300" />;
  if (status === "Failed") return <CircleX className="size-4 text-red-300" />;
  if (status === "Sent to PayPal") return <Clock className="size-4 text-sky-300" />;
  return <CircleDashed className="size-4 text-amber-300" />;
}

function TimelineWidget(params: AgWidgetParams<TimelineData>) {
  const { time, action, status, detail } = params.dataMapping;
  const rows = useRows(params, [time?.[0], action?.[0], status?.[0], detail?.[0]], !!time?.length && !!action?.length);
  if (!rows?.length) return null;
  const sorted = [...rows].sort((a, b) => asTime(a[0]) - asTime(b[0])).slice(-40);
  return (
    <ol className="relative h-full space-y-2 overflow-y-auto pr-1 text-ink-1">
      {sorted.map((r, i) => {
        const t = asTime(r[0]);
        const s = asText(r[2]);
        return (
          <li key={i} className="flex gap-3">
            <span className="mt-0.5 shrink-0">{s ? <StatusIcon status={s} /> : <CircleCheck className="size-4 text-ink-3" />}</span>
            <span className="min-w-0">
              <span className="block text-sm leading-snug">
                <span className="mr-2 font-mono text-[11px] text-ink-3 tabular-nums">
                  {Number.isNaN(t) ? "" : new Date(t).toLocaleTimeString([], { hourCycle: "h23" })}
                </span>
                {asText(r[1])}
                {s && s !== "Done" && <span className="ml-2 text-xs text-amber-200">{s}</span>}
              </span>
              {asText(r[3]) && <span className="block truncate text-xs text-ink-3">{asText(r[3])}</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

// Palette icons, drawn in the console's accent so they read as ours next to Studio's built-in widgets.
const svg = (body: string) => ({
  url: `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#4c8dff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`)}`,
});
const VERDICT_ICON = svg('<circle cx="12" cy="12" r="9" stroke="#33415c"/><path d="M12 3a9 9 0 0 1 8.5 12"/><path d="m9 12 2 2 4-4"/>');
const TIMELINE_ICON = svg('<path d="M6 4v16"/><circle cx="6" cy="7" r="2" fill="#4c8dff"/><circle cx="6" cy="17" r="2" fill="#4c8dff"/><path d="M11 7h8M11 12h6M11 17h8"/>');

/** Studio's built-in widgets plus ours, in their own "Checkout Guard" menu group. */
export const guardWidgets = (defaults: AgWidgetsConfig<AgDefaultRegistry>): AgWidgetsConfig<GuardRegistry> =>
  createWidgets<GuardRegistry>({
  additionalTypes: [
    {
      id: "guard-verdict",
      label: "AI verdict",
      icon: VERDICT_ICON,
      dataMapping: {
        verdict: { type: "field", supportedRoles: ["category"], requires: { cardinality: "many" }, required: true, aiDescription: "The incident verdict, e.g. incidents.verdict." },
        confidence: {
          type: "field",
          supportedRoles: ["numeric"],
          requires: { per: "dataMapping.verdict", cardinality: "one" },
          required: true,
          aiDescription: "The verdict's confidence (0-1), e.g. incidents.confidence.",
        },
        time: {
          type: "field",
          supportedRoles: ["temporal"],
          requires: { cardinality: "many" },
          required: false,
          aiDescription: "When the incident opened, to show the newest verdict, e.g. incidents.opened.",
        },
      },
      form: (p) =>
        p.createDefaults({
          dataMappingItems: [
            { key: "verdict", label: "Verdict" },
            { key: "confidence", label: "Confidence" },
            { key: "time", label: "Opened (newest wins)" },
          ],
        }),
      comp: VerdictWidget,
      defaultSize: { width: 360, height: 160 },
      minSize: { width: 260, height: 120 },
      ai: { description: "Checkout Guard's AI verdict for the newest incident: classification and a confidence ring." },
    },
    {
      id: "defence-timeline",
      label: "Defence timeline",
      icon: TIMELINE_ICON,
      dataMapping: {
        time: { type: "field", supportedRoles: ["temporal"], requires: { cardinality: "many" }, required: true, aiDescription: "When each action happened, e.g. actions.time." },
        action: { type: "field", supportedRoles: ["category"], requires: { cardinality: "many" }, required: true, aiDescription: "What was done, e.g. actions.action." },
        status: { type: "field", supportedRoles: ["category"], requires: { cardinality: "many" }, required: false, aiDescription: "Done, Needs owner, Failed…, e.g. actions.status." },
        detail: { type: "field", supportedRoles: ["category"], requires: { cardinality: "many" }, required: false, aiDescription: "Why it was allowed, e.g. actions.why." },
      },
      form: (p) =>
        p.createDefaults({
          dataMappingItems: [
            { key: "time", label: "Time" },
            { key: "action", label: "Action" },
            { key: "status", label: "Status" },
            { key: "detail", label: "Detail" },
          ],
        }),
      comp: TimelineWidget,
      defaultSize: { width: 420, height: 360 },
      minSize: { width: 280, height: 200 },
      ai: { description: "Every Checkout Guard defence in time order with its status and the rule that allowed it." },
    },
  ],
  menu: [{ label: "Checkout Guard", widgetIds: ["guard-verdict", "defence-timeline"] }, ...defaults.menu],
  });
