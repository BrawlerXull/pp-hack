"use client";

// AG Studio inside the night console. Studio owns the report being viewed or edited; this component feeds it
// our data, theme, widgets and AI, and hands its API and AI harness back to the page.

import { AgStudioAiModule, enableStudioDevValidations, type AgAiHarness, type AgReportState, type AgStudioApi, type AgStudioMode } from "ag-studio";
import { AgStudio, AgStudioProvider } from "ag-studio-react";
import { useMemo, useState } from "react";
import { buildGuardAi } from "./ai";
import { buildStudioData, type StudioPayload } from "./data";
import { initialReport } from "./report";
import { nightStudioTheme } from "./theme";
import { guardWidgets, type GuardRegistry } from "./widgets";

if (process.env.NODE_ENV !== "production") enableStudioDevValidations();

const STORAGE_KEY = "cg_studio_report_v1";
const LICENSE_KEY = process.env.NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY || undefined;
const MODULES_AI = [AgStudioAiModule];
const LAYOUT = { widgetBorderEnabled: true };
// The AI panel lives in edit mode (Studio has only filters in view mode): keep it first on the right.
const PANELS = { edit: { left: ["data" as const], right: ["ai" as const, "edit" as const, "filters" as const] } };
const STYLE = { height: "100%", width: "100%" };

export function loadSavedReport(): AgReportState<GuardRegistry> {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) return JSON.parse(saved) as AgReportState<GuardRegistry>;
  } catch {
    /* storage blocked or corrupt: start from the default report */
  }
  return initialReport;
}

export function saveReport(state: AgReportState<GuardRegistry> | null) {
  try {
    if (state) localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* a convenience only */
  }
}

export default function GuardStudio({
  payload,
  mode,
  onApi,
  onHarness,
  onState,
}: {
  payload: StudioPayload;
  mode: AgStudioMode;
  onApi: (api: AgStudioApi<GuardRegistry>) => void;
  onHarness: (h: AgAiHarness) => void;
  onState: (state: AgReportState<GuardRegistry>) => void;
}) {
  // Read once: Studio takes initialState, ai and the data structure at creation.
  const [initialState] = useState(loadSavedReport);
  const [aiAvailable] = useState(payload.ai.available);
  const ai = useMemo(() => (aiAvailable ? buildGuardAi(onHarness) : undefined), [aiAvailable, onHarness]);
  // Rows refresh in place (same sources and fields), which Studio applies to every widget.
  const data = useMemo(() => buildStudioData(payload), [payload]);

  return (
    <AgStudioProvider licenseKey={LICENSE_KEY} modules={ai ? MODULES_AI : undefined}>
      <AgStudio<GuardRegistry>
        style={STYLE}
        theme={nightStudioTheme}
        layout={LAYOUT}
        panels={PANELS}
        data={data}
        widgets={guardWidgets}
        ai={ai}
        initialState={initialState}
        mode={mode}
        // The ready event is typed for the default registry; it is this Studio's API, created with ours.
        onApiReady={(e) => onApi(e.api as unknown as AgStudioApi<GuardRegistry>)}
        onStudioReady={(e) => onState(e.api.getState())}
        onStateUpdated={(e) => {
          onState(e.state);
          saveReport(e.state);
        }}
      />
    </AgStudioProvider>
  );
}
