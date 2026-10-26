// The Studio Agent Framework on Checkout Guard: a "Guard analyst" agent with read-only Checkout Guard tools that
// hands dashboard building to Studio's built-in agents. Every model call goes through guardAdapter() to our
// backend (POST /studio/agent/turn), which runs it on Gemini: the key never reaches the browser.

import {
  createAiHarness,
  directLlmRunner,
  type AgAiEvent,
  type AgAiHarness,
  type AgAiHarnessSetup,
  type AgAiPromptStarter,
  type AgAiTool,
  type AgAiToolSchema,
  type AgLlmAdapter,
  type AgLlmRequest,
  type AgLlmResponse,
} from "ag-studio";
import { api } from "@/lib/api";
import type { IncidentDetail, IncidentSummary, Summary } from "@/lib/api";

const TURN_ENDPOINT = "/api/studio/agent/turn";

/** AG Studio LLM adapter: one turn = one POST to our backend; the reply is replayed as AG-UI events. */
export function guardAdapter(): AgLlmAdapter {
  return {
    executeTurn(request: AgLlmRequest, options?: { signal?: AbortSignal }) {
      let resolve!: (r: AgLlmResponse) => void;
      let reject!: (e: Error) => void;
      const complete = new Promise<AgLlmResponse>((res, rej) => {
        resolve = res;
        reject = rej;
      });
      // A failed turn is reported once, through `complete` (the host ends the run on that rejection).
      void complete.catch(() => {});

      async function* events(): AsyncIterableIterator<AgAiEvent> {
        let response: AgLlmResponse;
        try {
          const res = await fetch(TURN_ENDPOINT, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(request),
            signal: options?.signal,
          });
          const body = await res.json().catch(() => null);
          if (!res.ok) throw new Error(body?.error?.message ?? `The AI request failed (HTTP ${res.status})`);
          response = body as AgLlmResponse;
        } catch (e) {
          reject(e instanceof Error ? e : new Error(String(e)));
          return;
        }
        for (const item of response.output) {
          if (item.type === "message") {
            const text = item.content.map((c) => (c.type === "text" ? c.text : c.refusal)).join("");
            yield { type: "TEXT_MESSAGE_START", messageId: item.id, role: "assistant" };
            // Replay in small chunks so the answer reads as it arrives rather than popping in.
            for (let i = 0; i < text.length; i += 48) {
              yield { type: "TEXT_MESSAGE_CONTENT", messageId: item.id, delta: text.slice(i, i + 48) };
              await new Promise((r) => setTimeout(r, 6));
            }
            yield { type: "TEXT_MESSAGE_END", messageId: item.id };
          } else if (item.type === "function_call") {
            yield { type: "TOOL_CALL_START", toolCallId: item.callId, toolCallName: item.name };
            yield { type: "TOOL_CALL_ARGS", toolCallId: item.callId, delta: item.arguments };
            yield { type: "TOOL_CALL_END", toolCallId: item.callId };
          }
        }
        resolve(response);
      }

      const stream = events();
      return { stream: { [Symbol.asyncIterator]: () => stream }, complete };
    },
  };
}

type Args = Record<string, unknown>;

/** A read-only client tool: the agent sees `parameters` as JSON Schema; `run` returns JSON for the model. */
function readTool(name: string, description: string, parameters: object, run: (args: Args) => Promise<unknown>): AgAiTool {
  const schema = { name, description, parameters } as AgAiToolSchema;
  return {
    name,
    description,
    kind: "client",
    schema: () => schema,
    async execute(call, ctx) {
      try {
        return ctx.success(JSON.stringify(await run(call.args ?? {})));
      } catch (e) {
        return ctx.error(e instanceof Error ? e.message : "The Checkout Guard API did not answer");
      }
    },
  };
}

const NO_ARGS = { type: "object", properties: {}, additionalProperties: false };

function compactIncident(i: IncidentDetail) {
  return {
    id: i.id,
    status: i.status,
    opened_at: i.opened_at,
    closed_at: i.closed_at,
    detected_in_s: i.detected_in_s,
    triggers: i.triggers,
    verdict: i.classification,
    confidence: i.confidence,
    judged_by: i.ai_available ? i.model_id : `fixed rules (${i.ai_fallback_reason ?? "AI unavailable"})`,
    summary: i.verdict?.summary,
    evidence: (i.verdict?.evidence ?? []).map((e, n) => ({
      feature: e.feature,
      window_min: e.window_min,
      observed: i.evidence_vs_normal?.[n]?.observed ?? e.observed,
      times_normal: i.evidence_vs_normal?.[n]?.multiplier ?? null,
      why: e.why,
    })),
    ai_claims_dropped: i.dropped_evidence ?? 0,
    actions: i.actions.map((a) => ({ type: a.type, status: a.status, rule: a.rule, at: a.created_at, amount: a.params.amount ?? null })),
    attack_signature: {
      targeted_item: i.signature?.top_sku ?? null,
      devices: i.signature?.fingerprints?.length ?? 0,
      never_seen_email_domains: (i.signature?.new_email_domains ?? []).slice(0, 10),
    },
    owner_alert: i.report?.alert_text ?? null,
  };
}

const guardStatus = readTool(
  "get_guard_status",
  "Checkout Guard's live state: checkout status, defences in force (3-D Secure mode, blocked devices, held items), the open incident, the last 5/15 minutes vs normal, and whether the AI analyst and detector are running.",
  NO_ARGS,
  async () => {
    const s = await api<Summary>("/dashboard/summary");
    return {
      status: s.status,
      defences: s.guard_state,
      switches: s.switches,
      sale_window: s.sale_window,
      open_incident: s.open_incident && { id: s.open_incident.id, verdict: s.open_incident.classification, confidence: s.open_incident.confidence, opened_at: s.open_incident.opened_at },
      live: s.system.live,
      ai: s.system.ai.mode,
      detector: s.system.worker.state,
      last_24h: s.last_24h,
    };
  },
);

const getIncident = readTool(
  "get_incident",
  "One incident in full: detector triggers, the AI verdict with its evidence (our own numbers, with ×N vs normal), every action and the policy rule that allowed it, the attack signature and the owner's alert. Omit incident_id for the newest incident.",
  { type: "object", properties: { incident_id: { type: "string", description: "Incident id; omit for the newest" } }, additionalProperties: false },
  async (args) => {
    let id = typeof args.incident_id === "string" && args.incident_id ? args.incident_id : null;
    if (!id) id = (await api<IncidentSummary[]>("/dashboard/incidents?limit=1"))[0]?.id ?? null;
    if (!id) return { incident: null, note: "No incidents yet." };
    return compactIncident(await api<IncidentDetail>(`/dashboard/incidents/${encodeURIComponent(id)}`));
  },
);

const listIncidents = readTool(
  "list_incidents",
  "The 15 most recent incidents: id, when, verdict and confidence, status and triggers.",
  NO_ARGS,
  async () =>
    (await api<IncidentSummary[]>("/dashboard/incidents?limit=15")).map((i) => ({
      id: i.id,
      opened_at: i.opened_at,
      closed_at: i.closed_at,
      status: i.status,
      verdict: i.classification,
      confidence: i.confidence,
      triggers: i.triggers,
    })),
);

const INSTRUCTIONS = `You are the Guard analyst inside Checkout Guard, the night guard for a small shop's PayPal checkout (Wick & Wax, a candle shop).
Checkout Guard watches every checkout attempt, a deterministic detector opens an incident when traffic looks wrong, a Gemini analyst classifies it (card-testing attack, stolen-account testing, genuine rush, PayPal outage), and fixed policy rules decide the defences: 3-D Secure on every card, blocking bot devices, holding the targeted item, refunding matched test payments within the owner's caps.

You help the owner investigate. You only read: you cannot change defences, approve refunds or move money, and you never pretend to.
- For live state, call get_guard_status. For an incident, call get_incident (newest by default) or list_incidents.
- For questions about the attempts themselves, query the report's data (view_schema, then execute_query). Tables: attempts (checkout attempts), incidents, actions.
- To build or change the dashboard (new pages, charts, grids, filters), delegate to the dashboard team ("lead") with a precise brief: the page name, each widget, and the exact field ids to use (e.g. attempts.minute, attempts.outcome, attempts.emailDomain, attempts.device, attempts.incidentId, incidents.verdict, actions.action).
- Every number you state must come from a tool result or a query. Never invent one. Say "simulated" when the traffic is from the labelled simulator.
- Answer like a calm security engineer talking to a shop owner: short, concrete, plain English, no jargon. Use short bullet lists for findings.`;

const PROMPT_STARTERS: AgAiPromptStarter[] = [
  { label: "What happened in the last incident?", prompt: "What happened in the most recent incident, and what did Checkout Guard do about it?" },
  { label: "Investigate the latest attack", prompt: "Investigate the newest incident: build a page for it with its attempts per minute by outcome, the devices and email domains involved, and its defence timeline. Then summarise in three bullets." },
  { label: "Attack vs flash sale", prompt: "Compare the simulated attack with the simulated flash sale: which signals tell them apart? Use the attempts data." },
  { label: "Which devices tried the most cards?", prompt: "Which devices made the most attempts, and how many were declined or stopped?" },
];

export const GUARD_AGENT_ID = "guard";

/** Studio's AI setup. `onHarness` receives the harness so the page can start investigations itself. */
export function buildGuardAi(onHarness: (h: AgAiHarness) => void): AgAiHarnessSetup {
  const adapter = guardAdapter();
  return ({ api: studioApi }) => {
    const harness = createAiHarness(studioApi, ({ builtIn }) => ({
      agents: [
        directLlmRunner({
          id: GUARD_AGENT_ID,
          name: "Guard analyst",
          description: "Investigates Checkout Guard incidents and builds views of the attack with Studio's dashboard team.",
          adapter,
          instructions: () => INSTRUCTIONS,
          tools: (ctx) => [guardStatus, getIncident, listIncidents, ctx.studio.viewSchema(), ctx.studio.executeQuery(), ctx.studio.viewReport(), ctx.tools.delegateTo(["lead"])],
          maxTurns: 24,
        }),
        // Studio's own dashboard team (lead, data, page, planning, widget) on the same Gemini adapter.
        ...Object.values(builtIn).map((agent) => directLlmRunner({ ...agent, adapter })),
      ],
      primary: GUARD_AGENT_ID,
      promptStarters: PROMPT_STARTERS,
    }));
    onHarness(harness);
    return harness;
  };
}

/** The anomaly alert: when an incident opens, the Guard analyst builds an investigation page for it. */
export async function investigate(harness: AgAiHarness, incident: Pick<IncidentSummary, "id" | "opened_at">) {
  const at = new Date(incident.opened_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const session = await harness.createThread({ agentId: GUARD_AGENT_ID, title: `Incident ${at}` });
  session.sendMessage(
    `An incident opened at ${at} (id ${incident.id}). Investigate it: call get_incident for it, then ask the dashboard team to add a new page named "Incident ${at}" ` +
      `filtered to attempts.incidentId = "${incident.id}", with attempts per minute by outcome, the devices and email domains involved, and the guard decisions. ` +
      "Finish with three short bullets: what happened, what Checkout Guard did, and whether the owner needs to do anything.",
  );
  return session;
}
