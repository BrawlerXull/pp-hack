"use client";

import { AllCommunityModule, ModuleRegistry, themeQuartz, type ColDef, type ICellRendererParams } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useMemo } from "react";
import type { Attempt } from "@/lib/api";
import { OUTCOME_META, SKU_NAME, clock } from "@/lib/format";

ModuleRegistry.registerModules([AllCommunityModule]);

const theme = themeQuartz.withParams({
  accentColor: "#1d4ed8",
  borderColor: "#e2e8f0",
  headerBackgroundColor: "#f8fafc",
  headerTextColor: "#475569",
  fontSize: 13,
  rowHeight: 36,
  headerHeight: 36,
  wrapperBorderRadius: 10,
});

function OutcomeCell({ value }: ICellRendererParams<Attempt, Attempt["outcome"]>) {
  if (!value) return null;
  const meta = OUTCOME_META[value];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="size-2 rounded-full" style={{ background: meta.color }} />
      {meta.label}
    </span>
  );
}

function SourceCell({ data }: ICellRendererParams<Attempt>) {
  if (!data) return null;
  if (data.source === "web") return <span className="text-slate-700">Shopper</span>;
  return (
    <span className="rounded bg-violet-50 px-1.5 py-0.5 text-[11px] font-medium text-violet-700" title={data.paypal_live ? "Simulated attacker, real PayPal sandbox call" : "Simulated, no PayPal call"}>
      SIM{data.paypal_live ? " · live PayPal" : ""}
    </span>
  );
}

export default function AttemptsGrid({ rows, height = 420 }: { rows: Attempt[]; height?: number }) {
  const columns = useMemo<ColDef<Attempt>[]>(
    () => [
      { field: "ts", headerName: "Time", width: 105, valueFormatter: (p) => (p.value ? clock(p.value) : ""), sort: "desc" },
      { field: "outcome", headerName: "Outcome", width: 135, cellRenderer: OutcomeCell, filter: true },
      { field: "sku", headerName: "Item", width: 140, valueFormatter: (p) => SKU_NAME[p.value] ?? p.value, filter: true },
      { field: "amount", headerName: "Amount", width: 90, valueFormatter: (p) => `$${p.value}`, type: "rightAligned" },
      { field: "method", headerName: "Method", width: 90, valueFormatter: (p) => (p.value === "wallet" ? "PayPal" : "Card") },
      { field: "email_domain", headerName: "Email domain", flex: 1, minWidth: 150, filter: true },
      { field: "fingerprint", headerName: "Device", width: 100, cellClass: "font-mono text-xs" },
      { field: "decline_code", headerName: "Reason", width: 170, valueFormatter: (p) => p.value ?? "" },
      { field: "source", headerName: "Source", width: 140, cellRenderer: SourceCell },
    ],
    [],
  );

  return (
    <div style={{ height }}>
      <AgGridReact<Attempt>
        theme={theme}
        rowData={rows}
        columnDefs={columns}
        getRowId={(p) => p.data.id}
        defaultColDef={{ sortable: true, resizable: true }}
        animateRows
        pagination
        paginationAutoPageSize
        overlayNoRowsTemplate="No checkout attempts yet"
      />
    </div>
  );
}
