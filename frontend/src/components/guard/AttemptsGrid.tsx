"use client";

import { AllCommunityModule, ModuleRegistry, type ColDef, type GridApi, type ICellRendererParams, type IRowNode } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { Download, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Attempt, Outcome } from "@/lib/api";
import { DECISION_META, GUARD_REASON_LABEL, OUTCOME_META, SKU_NAME, clock } from "@/lib/format";
import { gridTheme } from "./gridTheme";
import { Badge } from "./ui";

ModuleRegistry.registerModules([AllCommunityModule]);

const STOPPED: Outcome[] = ["blocked", "challenged", "sca_required", "sca_failed"];

const FILTERS: { key: string; label: string; test: (a: Attempt) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "stopped", label: "Stopped", test: (a) => STOPPED.includes(a.outcome) },
  { key: "paid", label: "Paid or held", test: (a) => a.outcome === "captured" || a.outcome === "authorized" },
  { key: "declined", label: "Declined", test: (a) => a.outcome === "declined" },
  { key: "refunded", label: "Refunded or voided", test: (a) => a.outcome === "refunded" || a.outcome === "voided" },
];

const OUTCOME_HINT: Partial<Record<Outcome, string>> = {
  authorized: "Approved by the bank and held: charged after the review window unless an attack matches it",
  voided: "The hold was released: never charged, no fee, nothing to dispute",
  sca_required: "Stopped at the bank's 3-D Secure check",
};

function OutcomeCell({ value }: ICellRendererParams<Attempt, Outcome>) {
  if (!value) return null;
  const meta = OUTCOME_META[value];
  return (
    <span title={OUTCOME_HINT[value]}>
      <Badge tone={meta.tone}>{meta.label}</Badge>
    </span>
  );
}

/** "Card · VISA 1368" when PayPal told us the brand and last 4 (never the number). */
function methodText(a: Attempt | undefined): string {
  if (!a) return "";
  if (a.method === "wallet") return "PayPal";
  if (a.method === "unknown") return "Not known yet"; // observer mode: PayPal sent only the capture so far
  return [a.card_brand, a.card_last4].some(Boolean) ? `Card · ${[a.card_brand, a.card_last4].filter(Boolean).join(" ")}` : "Card";
}

function sourceText(a: Attempt | undefined): string {
  if (!a) return "";
  if (a.source === "web") return "Shopper";
  if (a.source === "api") return "Guard API";
  if (a.source === "observed") return a.paypal_live ? "Webhook" : "Webhook · SIM";
  return a.paypal_live ? "SIM · live PayPal" : "SIM";
}

function SourceCell({ data }: ICellRendererParams<Attempt>) {
  if (!data) return null;
  if (data.source === "web" || data.source === "api") return <span className="text-ink-2">{sourceText(data)}</span>;
  const title =
    data.source === "observed"
      ? "Seen through PayPal webhooks only: no guard check ran"
      : data.paypal_live
        ? "Simulated attacker, real PayPal sandbox call"
        : "Simulated, no PayPal call";
  return (
    <span title={title}>
      <Badge>{sourceText(data)}</Badge>
    </span>
  );
}

/** The guard's reason for its decision, and PayPal's decline code when it got that far. */
function reasonText(a: Attempt): string {
  const guard = a.guard_reason ? (GUARD_REASON_LABEL[a.guard_reason] ?? a.guard_reason) : "";
  if (a.outcome === "sca_failed") {
    const bank = `Bank check failed${a.sca_liability_shift ? ` (liability shift ${a.sca_liability_shift})` : ""}`;
    return a.guard_reason && a.guard_reason !== "normal" ? `${guard} · ${bank}` : bank;
  }
  if (a.decline_code) return a.guard_reason && a.guard_reason !== "normal" ? `${guard} · PayPal: ${a.decline_code}` : `PayPal: ${a.decline_code}`;
  return guard;
}

export default function AttemptsGrid({ rows, pageSize = 25 }: { rows: Attempt[]; pageSize?: number }) {
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
      { field: "ts", headerName: "Time", pinned: "left", width: 96, sort: "desc", valueFormatter: (p) => (p.value ? clock(p.value) : ""), cellClass: "tabular-nums" },
      {
        field: "outcome",
        headerName: "Outcome",
        width: 150,
        cellRenderer: OutcomeCell,
        filter: "agTextColumnFilter",
        filterValueGetter: (p) => (p.data ? OUTCOME_META[p.data.outcome].label : ""),
      },
      { field: "amount", headerName: "Amount", width: 96, type: "rightAligned", valueFormatter: (p) => `$${Number(p.value).toFixed(2)}`, cellClass: "tabular-nums" },
      { field: "sku", headerName: "Item", minWidth: 140, flex: 1, valueFormatter: (p) => SKU_NAME[p.value] ?? p.value, filter: "agTextColumnFilter" },
      { colId: "method", headerName: "Payment", width: 136, valueGetter: (p) => methodText(p.data) },
      { field: "email_domain", headerName: "Email domain", minWidth: 136, flex: 1, filter: "agTextColumnFilter", valueFormatter: (p) => p.value ?? "–" },
      { field: "fingerprint", headerName: "Device", width: 104, cellClass: "font-mono text-[12px] text-ink-2" },
      { field: "decision", headerName: "Guard decision", width: 140, hide: true, valueFormatter: (p) => (p.value ? DECISION_META[p.value as Attempt["decision"]].label : "") },
      {
        colId: "reason",
        headerName: "Reason",
        minWidth: 200,
        flex: 2,
        valueGetter: (p) => (p.data ? reasonText(p.data) : ""),
        tooltipValueGetter: (p) =>
          p.data ? [reasonText(p.data), p.data.decline_category && `PayPal decline category: ${p.data.decline_category}`].filter(Boolean).join(" · ") : undefined,
        filter: "agTextColumnFilter",
        cellClass: "text-ink-2",
      },
      { colId: "source", headerName: "Source", width: 116, valueGetter: (p) => sourceText(p.data), cellRenderer: SourceCell },
    ],
    [],
  );

  const isExternalFilterPresent = useCallback(() => filterRef.current.key !== "all", []);
  const doesExternalFilterPass = useCallback((node: IRowNode<Attempt>) => !!node.data && filterRef.current.test(node.data), []);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
        <label className="relative">
          <span className="sr-only">Search attempts</span>
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-ink-3" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search domain, device, code"
            className="h-9 w-64 rounded-lg border border-line-strong bg-white pr-3 pl-8 text-sm text-ink-1 placeholder:text-ink-3 focus:border-guard focus:outline-none"
          />
        </label>
        <div className="flex flex-wrap rounded-lg border border-line-strong bg-white p-0.5" role="group" aria-label="Filter by outcome">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              aria-pressed={filter === f.key}
              className={`rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors ${filter === f.key ? "bg-gray-100 text-ink-1" : "text-ink-2 hover:text-ink-1"}`}
            >
              {f.label}
              <span className="ml-1.5 tabular-nums text-ink-3">{counts[f.key]}</span>
            </button>
          ))}
        </div>
        <span className="ml-auto text-[13px] text-ink-3 tabular-nums" aria-live="polite">
          {shown ?? rows.length} attempts
        </span>
        <button
          type="button"
          onClick={() => apiRef.current?.exportDataAsCsv({ fileName: "checkout-attempts.csv" })}
          className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-line-strong bg-white px-3 text-[13px] font-medium text-ink-1 hover:bg-gray-50"
        >
          <Download className="size-4" aria-hidden /> Export CSV
        </button>
      </div>
      <div className="h-[530px]">
        <AgGridReact<Attempt>
          theme={gridTheme}
          rowData={rows}
          columnDefs={columns}
          getRowId={(p) => p.data.id}
          defaultColDef={{ sortable: true, resizable: true, suppressHeaderFilterButton: true }}
          quickFilterText={query}
          isExternalFilterPresent={isExternalFilterPresent}
          doesExternalFilterPass={doesExternalFilterPass}
          onGridReady={(e) => (apiRef.current = e.api)}
          onModelUpdated={(e) => setShown(e.api.getDisplayedRowCount())}
          pagination
          paginationPageSize={pageSize}
          paginationPageSizeSelector={[10, 25, 50, 100]}
          tooltipShowDelay={300}
          ensureDomOrder
          enableCellTextSelection
          animateRows
          overlayNoRowsTemplate="No checkout attempts yet. Run a simulation from the sidebar."
        />
      </div>
    </div>
  );
}
