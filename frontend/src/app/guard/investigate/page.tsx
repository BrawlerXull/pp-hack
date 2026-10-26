"use client";

import type { AgAiHarness, AgAiRunStatus, AgReportState, AgStudioApi, AgStudioMode } from "ag-studio";
import { Eye, Loader2, Pencil, RotateCcw, Sparkles, TriangleAlert } from "lucide-react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { saveReport } from "@/components/studio/GuardStudio";
import { investigate } from "@/components/studio/ai";
import type { StudioPayload } from "@/components/studio/data";
import { PAGES, initialReport } from "@/components/studio/report";
import type { GuardRegistry } from "@/components/studio/widgets";
import { Badge, Button, PageHeader, Skeleton } from "@/components/guard/ui";
import type { Summary } from "@/lib/api";

// AG Studio and its widgets run in the browser only.
const GuardStudio = dynamic(() => import("@/components/studio/GuardStudio"), { ssr: false, loading: () => <Skeleton className="h-full" /> });

const AUTO_KEY = "cg_auto_investigate";
const DONE_KEY = "cg_investigated";
// Only incidents this fresh are investigated automatically; older ones on request (the incident page's link).
const AUTO_WINDOW_MS = 30 * 60_000;

// Incidents already investigated survive reloads: each costs a run of AI calls and adds a report page.
function loadInvestigated(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(DONE_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function rememberInvestigated(ids: Set<string>) {
  try {
    localStorage.setItem(DONE_KEY, JSON.stringify([...ids].slice(-50)));
  } catch {
    /* a convenience only */
  }
}
const TITLES: Record<string, string> = Object.fromEntries(PAGES.map((p) => [p.id, p.title]));
const pageTitle = (id: string, i: number) => TITLES[id] ?? (id.replace(/[-_]+/g, " ").replace(/^\w/, (c) => c.toUpperCase()) || `Page ${i + 1}`);

type Run = { title: string; status: AgAiRunStatus };

export default function InvestigatePage() {
  const { data: payload, error } = useSWR<StudioPayload>("/studio/data?hours=48", { refreshInterval: 15_000 });
  const { data: summary } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const requested = useSearchParams().get("incident");

  const [mode, setMode] = useState<AgStudioMode>("view");
  const [report, setReport] = useState<AgReportState<GuardRegistry> | null>(null);
  const [harness, setHarness] = useState<AgAiHarness | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [auto, setAuto] = useState(() => {
    try {
      return localStorage.getItem(AUTO_KEY) !== "0";
    } catch {
      return true;
    }
  });
  const apiRef = useRef<AgStudioApi<GuardRegistry> | null>(null);
  const investigated = useRef<Set<string> | null>(null);

  const onApi = useCallback((api: AgStudioApi<GuardRegistry>) => (apiRef.current = api), []);
  const onHarness = useCallback((h: AgAiHarness) => setHarness(h), []);

  // The anomaly alert: a newly opened incident (or one linked from its incident page) gets an investigation
  // page built by the Guard analyst. Each incident at most once per visit.
  const open = summary?.open_incident;
  const fresh = open && Date.parse(open.opened_at) > Date.parse(summary?.system.live?.at ?? open.opened_at) - AUTO_WINDOW_MS;
  const target = requested ? payload?.incidents.find((i) => i.id === requested) : auto && fresh ? open : undefined;
  useEffect(() => {
    if (!harness || !target) return;
    investigated.current ??= loadInvestigated();
    if (investigated.current.has(target.id)) return;
    investigated.current.add(target.id);
    rememberInvestigated(investigated.current);
    const title = `Incident ${new Date(target.opened_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`;
    investigate(harness, target)
      .then((session) => {
        const update = () => setRun({ title, status: session.status });
        session.addEventListener("changed", update);
        update();
      })
      .catch(() => setRun({ title, status: "error" }));
  }, [harness, target]);

  function toggleAuto() {
    setAuto((a) => {
      try {
        localStorage.setItem(AUTO_KEY, a ? "0" : "1");
      } catch {
        /* ignore */
      }
      return !a;
    });
  }

  function selectPage(id: string) {
    const api = apiRef.current;
    if (api) api.setState({ ...api.getState(), selectedPageId: id });
  }

  // Studio shows its AI panel in edit mode only: switch to it and open the panel.
  function askAnalyst() {
    setMode("edit");
    window.setTimeout(() => {
      const api = apiRef.current;
      if (!api) return;
      const state = api.getState();
      api.setState({ ...state, panels: { ...state.panels, ai: { ...state.panels?.ai, collapsed: false } } });
    }, 50);
  }

  function resetLayout() {
    if (!window.confirm("Reset the report to Checkout Guard's default pages? Pages you or the analyst added are removed.")) return;
    saveReport(null);
    apiRef.current?.setState(initialReport);
  }

  const pages = report?.pages ?? initialReport.pages;
  const selected = report?.selectedPageId ?? initialReport.selectedPageId;

  return (
    <>
      <PageHeader
        title="Investigate"
        subtitle="Every checkout attempt, incident and defence from the last 48 hours in AG Studio. Filter, cross-filter and rearrange, or ask the Guard analyst to build the view you need."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {payload?.ai.available && (
              <Button size="sm" onClick={askAnalyst}>
                <Sparkles className="size-3.5" /> Ask the Guard analyst
              </Button>
            )}
            <div className="flex rounded-lg bg-white/[0.05] p-0.5 ring-1 ring-white/10" role="group" aria-label="Report mode">
              {(["view", "edit"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  aria-pressed={mode === m}
                  className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium ${mode === m ? "bg-white/[0.12] text-ink-1" : "text-ink-2 hover:text-ink-1"}`}
                >
                  {m === "view" ? <Eye className="size-3.5" /> : <Pencil className="size-3.5" />} {m === "view" ? "View" : "Edit layout"}
                </button>
              ))}
            </div>
            <Button variant="ghost" size="sm" onClick={resetLayout}>
              <RotateCcw className="size-3.5" /> Reset
            </Button>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <nav className="flex flex-wrap gap-1" aria-label="Report pages">
          {pages.map((p, i) => (
            <button
              key={p.id}
              onClick={() => selectPage(p.id)}
              aria-current={p.id === selected ? "page" : undefined}
              className={`rounded-full px-3 py-1.5 text-xs font-medium transition ${
                p.id === selected ? "bg-guard/20 text-sky-100 ring-1 ring-guard/40" : "text-ink-2 hover:bg-white/[0.05] hover:text-ink-1"
              }`}
            >
              {pageTitle(p.id, i)}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
          {payload && !payload.ai.available && (
            <Badge tone="warn">
              <TriangleAlert className="size-3" /> Guard analyst off: the server has no Gemini key
            </Badge>
          )}
          {payload?.ai.available && (
            <>
              {run && (
                <Badge tone={run.status === "error" ? "bad" : run.status === "running" ? "info" : "good"}>
                  {run.status === "running" ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                  {run.status === "running"
                    ? `Guard analyst investigating ${run.title}…`
                    : run.status === "error"
                      ? `Investigation of ${run.title} failed`
                      : `${run.title} investigated: see its page and the assistant`}
                </Badge>
              )}
              <button
                onClick={toggleAuto}
                role="switch"
                aria-checked={auto}
                className="flex items-center gap-2 rounded-full px-2.5 py-1 text-ink-2 ring-1 ring-white/10 hover:bg-white/[0.05]"
                title="When an incident opens, the Guard analyst builds an investigation page for it"
              >
                <span className={`relative h-3.5 w-6 rounded-full transition ${auto ? "bg-guard" : "bg-white/15"}`}>
                  <span className={`absolute top-0.5 size-2.5 rounded-full bg-white transition-all ${auto ? "left-3" : "left-0.5"}`} />
                </span>
                Auto-investigate new incidents
              </button>
            </>
          )}
        </div>
      </div>

      <div className="panel h-[calc(100vh-15rem)] min-h-[640px] overflow-hidden">
        {error ? (
          <p className="p-6 text-sm text-red-300">Couldn&apos;t load the investigation data.</p>
        ) : !payload ? (
          <Skeleton className="h-full rounded-none" />
        ) : (
          <GuardStudio payload={payload} mode={mode} onApi={onApi} onHarness={onHarness} onState={setReport} />
        )}
      </div>
      <p className="text-xs text-ink-3 md:hidden">AG Studio works best on a larger screen.</p>
    </>
  );
}
