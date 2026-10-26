// The Investigate page's starting report, as AG Studio report state. Field ids ("attempts.minute") refer to the
// sources in ./data.ts; "guard-verdict" and "defence-timeline" are our own widgets (./widgets.tsx).

import type { AgPageState, AgReportState, AgWidgetLayoutState } from "ag-studio";
import type { GuardRegistry } from "./widgets";

// Position on Studio's canvas grid: 24 columns wide, rows of studioCanvasRowHeight (18px).
const at = (xTrack: number, yTrack: number, xSpan: number, ySpan: number): AgWidgetLayoutState => ({ xTrack, yTrack, xSpan, ySpan });
const title = (text: string, subtitle?: string) => ({
  title: { enabled: true, text },
  ...(subtitle && { subtitle: { enabled: true, text: subtitle } }),
});
const sum = (id: string) => ({ id, aggregation: "sum" as const });
const countd = (id: string) => ({ id, aggregation: "countd" as const });
const legend = { style: { theme: { common: { legend: { enabled: true } } } } };

const kpi = (text: string, value: { id: string; aggregation?: "sum" | "countd" }, sparklineX?: string) => ({
  type: "value" as const,
  dataMapping: { value: [value], ...(sparklineX && { sparklineX: [{ id: sparklineX }] }) },
  format: {
    title: { enabled: true, text, typography: { fontSize: 13, fontWeight: "bold" as const, fontStyle: "normal" as const } },
    style: { typography: { fontSize: 28, fontWeight: "bold" as const, fontStyle: "normal" as const } },
  },
});

const attack: AgPageState<GuardRegistry> = {
  id: "attack",
  widgets: {
    "kpi-attempts": kpi("Attempts", sum("attempts.attempts"), "attempts.minute"),
    "kpi-decline": kpi("Decline rate", { id: "declineRate" }),
    "kpi-stopped": kpi("Stopped by guard", sum("attempts.stopped"), "attempts.minute"),
    "kpi-refunded": kpi("Refunded (USD)", sum("incidents.refundedTotal")),
    verdict: {
      type: "guard-verdict",
      dataMapping: {
        verdict: [{ id: "incidents.verdict" }],
        confidence: [{ id: "incidents.confidence", aggregation: "max" }],
        time: [{ id: "incidents.opened" }],
      },
      format: title("Latest AI verdict"),
    },
    traffic: {
      type: "column-chart-stacked",
      dataMapping: {
        categoryKey: [{ id: "attempts.minute" }],
        valueKey: [sum("attempts.attempts")],
        legendKey: [{ id: "attempts.result" }],
      },
      format: { ...title("Attempts per minute, by result", "Click a bar or legend item to cross-filter the page"), ...legend },
    },
    decisions: {
      type: "donut-chart",
      dataMapping: { categoryKey: [{ id: "attempts.decision" }], valueKey: [sum("attempts.attempts")] },
      format: title("Guard decisions", "Before any PayPal order is created"),
    },
    "domain-kind": {
      type: "bar-chart-stacked",
      dataMapping: {
        categoryKey: [{ id: "attempts.domainKind" }],
        valueKey: [sum("attempts.attempts")],
        legendKey: [{ id: "attempts.result" }],
      },
      format: { ...title("Email domains", "Bots use domains real customers don't"), ...legend },
    },
    devices: {
      type: "grid",
      dataMapping: {
        cols: [
          { id: "attempts.device" },
          sum("attempts.attempts"),
          sum("attempts.declined"),
          sum("attempts.stopped"),
          countd("attempts.emailDomain"),
          { id: "declineRate" },
        ],
      },
      sort: [{ field: sum("attempts.attempts"), direction: "desc" }],
      format: title("Devices", "A card-testing bot reuses a few devices with many cards and email domains"),
    },
  },
  widgetLayout: {
    "kpi-attempts": at(0, 0, 4, 7),
    "kpi-decline": at(4, 0, 4, 7),
    "kpi-stopped": at(8, 0, 4, 7),
    "kpi-refunded": at(12, 0, 4, 7),
    verdict: at(16, 0, 8, 7),
    traffic: at(0, 7, 16, 18),
    decisions: at(16, 7, 8, 18),
    "domain-kind": at(0, 25, 10, 16),
    devices: at(10, 25, 14, 16),
  },
};

const incidents: AgPageState<GuardRegistry> = {
  id: "incidents",
  widgets: {
    list: {
      type: "grid",
      dataMapping: {
        cols: [
          { id: "incidents.name" },
          { id: "incidents.status" },
          { id: "incidents.judgedBy" },
          { id: "incidents.confidence", aggregation: "max" },
          sum("incidents.refunds"),
          sum("incidents.refundedTotal"),
          sum("incidents.droppedClaims"),
          { id: "incidents.triggers" },
        ],
      },
      format: title("Incidents", "Select one to cross-filter the timeline and actions"),
    },
    timeline: {
      type: "defence-timeline",
      dataMapping: {
        time: [{ id: "actions.time" }],
        action: [{ id: "actions.action" }],
        status: [{ id: "actions.status" }],
        detail: [{ id: "actions.why" }],
      },
      format: title("Defence timeline", "What Checkout Guard did, in order, and the fixed rule that allowed it"),
    },
    actions: {
      type: "grid",
      dataMapping: {
        cols: [
          { id: "actions.incident" },
          { id: "actions.action" },
          { id: "actions.status" },
          { id: "actions.rule" },
          sum("actions.amount"),
          { id: "actions.paypalRef" },
        ],
      },
      format: title("Actions and PayPal references"),
    },
  },
  widgetLayout: {
    list: at(0, 0, 15, 16),
    timeline: at(15, 0, 9, 32),
    actions: at(0, 16, 15, 16),
  },
};

const compare: AgPageState<GuardRegistry> = {
  id: "compare",
  widgets: {
    rates: {
      type: "bar-chart-grouped",
      dataMapping: { categoryKey: [{ id: "attempts.traffic" }], valueKey: [{ id: "declineRate" }, { id: "stoppedShare" }] },
      format: { ...title("Declines and guard stops by traffic", "An attack is declined and stopped; a real rush is not"), ...legend },
    },
    domains: {
      type: "column-chart-stacked",
      dataMapping: {
        categoryKey: [{ id: "attempts.traffic" }],
        valueKey: [sum("attempts.attempts")],
        legendKey: [{ id: "attempts.domainKind" }],
      },
      format: { ...title("Email domain kind by traffic"), ...legend },
    },
    items: {
      type: "column-chart-stacked",
      dataMapping: {
        categoryKey: [{ id: "attempts.traffic" }],
        valueKey: [sum("attempts.attempts")],
        legendKey: [{ id: "attempts.item" }],
      },
      format: { ...title("Items bought by traffic", "Bots buy the cheapest item; shoppers vary"), ...legend },
    },
    summary: {
      type: "grid",
      dataMapping: {
        cols: [
          { id: "attempts.traffic" },
          sum("attempts.attempts"),
          { id: "declineRate" },
          { id: "stoppedShare" },
          countd("attempts.device"),
          countd("attempts.emailDomain"),
        ],
      },
      format: title("Side by side"),
    },
  },
  widgetLayout: {
    rates: at(0, 0, 12, 16),
    domains: at(12, 0, 12, 16),
    items: at(0, 16, 12, 16),
    summary: at(12, 16, 12, 16),
  },
};

export const PAGES = [
  { id: attack.id, title: "Live attack" },
  { id: incidents.id, title: "Incidents" },
  { id: compare.id, title: "Attack vs flash sale" },
] as const;

export const initialReport: AgReportState<GuardRegistry> = {
  selectedPageId: attack.id,
  pages: [attack, incidents, compare],
  panels: { filters: { collapsed: true }, data: { collapsed: true } },
};
