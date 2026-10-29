"use client";

import {
  AllCommunityModule,
  ModuleRegistry,
  colorSchemeDarkBlue,
  themeQuartz,
  type ColDef,
  type GridApi,
  type ICellRendererParams,
  type IRowNode,
  type RowClassRules,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { CreditCard, Search, Wallet } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Attempt, Outcome } from "@/lib/api";
import { DECISION_META, GUARD_REASON_LABEL, OUTCOME_META, SKU_NAME, clock } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

const theme = themeQuartz.withPart(colorSchemeDarkBlue).withParams({
  backgroundColor: "#0e1628",
  foregroundColor: "#e7ecf6",
  chromeBackgroundColor: "#111a2f",
  headerBackgroundColor: "#111a2f",
  headerTextColor: "#a9b3c7",
  headerFontSize: 12,
  headerFontWeight: 500,
  borderColor: "rgba(255,255,255,0.07)",
  rowHoverColor: "rgba(76,141,255,0.08)",
  oddRowBackgroundColor: "rgba(255,255,255,0.012)",
  accentColor: "#4c8dff",
  fontFamily: "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif",
  fontSize: 13,
  rowHeight: 40,
  headerHeight: 38,
  spacing: 7,
  wrapperBorderRadius: 12,
  browserColorScheme: "dark",
});

const STOPPED: Outcome[] = ["blocked", "challenged", "sca_required", "sca_failed"];

const FILTERS: { key: string; label: string; test: (a: Attempt) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "stopped", label: "Stopped by guard", test: (a) => STOPPED.includes(a.outcome) },
  { key: "paid", label: "Paid or held", test: (a) => a.outcome === "captured" || a.outcome === "authorized" },
  { key: "declined", label: "Declined by bank", test: (a) => a.outcome === "declined" },
  { key: "refunded", label: "Refunded or voided", test: (a) => a.outcome === "refunded" || a.outcome === "voided" },
];

function OutcomeCell({ value }: ICellRendererParams<Attempt, Attempt["outcome"]>) {
  if (!value) return null;
  const meta = OUTCOME_META[value];
  return (
    <span className="inline-flex items-center gap-2 text-ink-1">
      <span className="size-2 rounded-full" style={{ background: meta.color }} aria-hidden />
      {meta.label}
    </span>
  );
}

const DECISION_STYLE = {
  neutral: "text-ink-3",
  info: "bg-sky-400/10 text-sky-300 ring-1 ring-inset ring-sky-400/20",
  warn: "bg-amber-400/10 text-amber-200 ring-1 ring-inset ring-amber-400/20",
  bad: "bg-red-400/10 text-red-300 ring-1 ring-inset ring-red-400/20",
  good: "",
} as const;

function DecisionCell({ value }: ICellRendererParams<Attempt, Attempt["decision"]>) {
  if (!value) return null;
  const meta = DECISION_META[value];
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${DECISION_STYLE[meta.tone]}`}>{meta.label}</span>;
}

/** A tiny 2×2 identicon from the device hash, so repeat devices are recognisable at a glance. */
function DeviceCell({ value }: ICellRendererParams<Attempt, string>) {
  if (!value) return null;
  const hues = [0, 2, 4, 6].map((i) => (parseInt(value.slice(i, i + 2), 16) || 0) * 1.41);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="grid size-4 grid-cols-2 gap-px overflow-hidden rounded-[3px]" aria-hidden>
        {hues.map((h, i) => (
          <span key={i} style={{ background: `oklch(0.68 0.13 ${h})` }} />
        ))}
      </span>
      <span className="font-mono text-xs text-ink-2">{value}</span>
    </span>
  );
}

/** "Card · VISA 1368" when PayPal told us the brand and last 4 (never the number). */
function methodText(a: Attempt | undefined): string {
  if (!a) return "";
  if (a.method === "wallet") return "PayPal";
  return [a.card_brand, a.card_last4].some(Boolean) ? `Card · ${[a.card_brand, a.card_last4].filter(Boolean).join(" ")}` : "Card";
}

function MethodCell({ data }: ICellRendererParams<Attempt>) {
  if (!data) return null;
  const Icon = data.method === "wallet" ? Wallet : CreditCard;
  return (
    <span className="inline-flex items-center gap-1.5 text-ink-2">
      <Icon className="size-3.5" aria-hidden /> {methodText(data)}
    </span>
  );
}

function SourceCell({ data }: ICellRendererParams<Attempt>) {
  if (!data) return null;
  if (data.source === "web") return <span className="text-ink-1">Shopper</span>;
  return (
    <span
      className="rounded bg-violet-400/15 px-1.5 py-0.5 text-[11px] font-semibold tracking-wide text-violet-200"
      title={data.paypal_live ? "Simulated attacker, real PayPal sandbox call" : "Simulated, no PayPal call"}
    >
      SIM{data.paypal_live ? " · live PayPal" : ""}
    </span>
  );
}

/** The guard's reason for its decision, and PayPal's decline code when it got that far. */
function whyText(a: Attempt): string {
  const guard = a.guard_reason ? (GUARD_REASON_LABEL[a.guard_reason] ?? a.guard_reason) : "";
  if (a.outcome === "sca_failed") {
    const bank = `Bank check failed${a.sca_liability_shift ? ` (liability shift ${a.sca_liability_shift})` : ""}`;
    return a.guard_reason && a.guard_reason !== "normal" ? `${guard} · ${bank}` : bank;
  }
  if (a.decline_code) return a.guard_reason && a.guard_reason !== "normal" ? `${guard} · PayPal: ${a.decline_code}` : `PayPal: ${a.decline_code}`;
  return guard;
}

const rowClassRules: RowClassRules<Attempt> = {
  "cg-row-stopped": (p) => !!p.data && STOPPED.includes(p.data.outcome),
  "cg-row-refunded": (p) => p.data?.outcome === "refunded",
  "cg-row-new": (p) => !!p.data && Date.now() - new Date(p.data.ts).getTime() < 5000,
};

export default function AttemptsGrid({ rows, height = 460 }: { rows: Attempt[]; height?: number }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [shown, setShown] = useState<number | null>(null);
  const apiRef = useRef<GridApi<Attempt> | null>(null);
  const filterRef = useRef(FILTERS[0]);

  useEffect(() => {
    filterRef.current = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
    apiRef.current?.onFilterChanged();
  }, [filter]);

  const counts = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f.key, rows.filter(f.test).length])), [rows]);

  const columns = useMemo<ColDef<Attempt>[]>(
    () => [
      { field: "ts", headerName: "Time", flex: 0.8, minWidth: 86, valueFormatter: (p) => (p.value ? clock(p.value) : ""), sort: "desc", cellClass: "tabular-nums text-ink-2" },
      { field: "outcome", headerName: "Outcome", flex: 1.2, minWidth: 128, cellRenderer: OutcomeCell, filter: true },
      { field: "decision", headerName: "Guard", flex: 0.9, minWidth: 98, cellRenderer: DecisionCell },
      { field: "sku", headerName: "Item", flex: 1, minWidth: 108, valueFormatter: (p) => SKU_NAME[p.value] ?? p.value, filter: true },
      { field: "amount", headerName: "Amount", flex: 0.7, minWidth: 74, valueFormatter: (p) => `$${p.value}`, type: "rightAligned", cellClass: "tabular-nums" },
      { field: "method", headerName: "Paid with", flex: 0.8, minWidth: 88, cellRenderer: MethodCell },
      { field: "email_domain", headerName: "Email domain", flex: 1.3, minWidth: 118, filter: true },
      { field: "fingerprint", headerName: "Device", flex: 1, minWidth: 108, cellRenderer: DeviceCell },
      {
        colId: "why",
        headerName: "Why",
        flex: 1.4,
        minWidth: 128,
        valueGetter: (p) => (p.data ? whyText(p.data) : ""),
        tooltipValueGetter: (p) =>
          p.data ? [whyText(p.data), p.data.decline_category && `PayPal decline category: ${p.data.decline_category}`].filter(Boolean).join(" · ") : undefined,
        filter: true,
        cellClass: "text-xs text-ink-2",
      },
      { field: "source", headerName: "Source", flex: 0.7, minWidth: 76, cellRenderer: SourceCell },
    ],
    [],
  );

  const isExternalFilterPresent = useCallback(() => filterRef.current.key !== "all", []);
  const doesExternalFilterPass = useCallback((node: IRowNode<Attempt>) => !!node.data && filterRef.current.test(node.data), []);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative">
          <span className="sr-only">Search attempts</span>
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-3" aria-hidden />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search domain, device, code…"
            className="w-60 rounded-lg border border-white/10 bg-white/[0.04] py-1.5 pr-3 pl-8 text-sm text-ink-1 placeholder:text-ink-3 focus:border-guard/60 focus:ring-2 focus:ring-guard/20 focus:outline-none"
          />
        </label>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by outcome">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              aria-pressed={filter === f.key}
              className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                filter === f.key ? "bg-white/[0.12] text-ink-1 ring-1 ring-white/15" : "text-ink-2 hover:bg-white/[0.05]"
              }`}
            >
              {f.label}
              <span className="ml-1.5 tabular-nums text-ink-3">{counts[f.key]}</span>
            </button>
          ))}
        </div>
        <span className="ml-auto text-xs text-ink-3 tabular-nums">{shown ?? rows.length} shown</span>
      </div>
      <div style={{ height }}>
        <AgGridReact<Attempt>
          theme={theme}
          rowData={rows}
          columnDefs={columns}
          getRowId={(p) => p.data.id}
          defaultColDef={{ sortable: true, resizable: true }}
          rowClassRules={rowClassRules}
          quickFilterText={query}
          isExternalFilterPresent={isExternalFilterPresent}
          doesExternalFilterPass={doesExternalFilterPass}
          onGridReady={(e) => (apiRef.current = e.api)}
          onModelUpdated={(e) => setShown(e.api.getDisplayedRowCount())}
          tooltipShowDelay={300}
          animateRows
          overlayNoRowsTemplate="No checkout attempts yet. Start a simulation from the demo bar below."
        />
      </div>
    </div>
  );
}
