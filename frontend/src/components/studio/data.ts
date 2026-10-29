// AG Studio's data for the Investigate page: three related tables from GET /studio/data (backend
// app/api/studio.py), with display fields derived here so widgets and the AI read plain-English values.

import type { AgDataSourcesDefinition, AgExpressionFieldDefinition, AgFieldDefinition, AgFormat, AgRelationDefinition } from "ag-studio";
import { ACTION_SHORT, CLASSIFICATION_LABEL, DECISION_META, GUARD_REASON_LABEL, OUTCOME_META, SKU_NAME, ruleLabel } from "@/lib/format";
import type { Classification, Decision, Outcome } from "@/lib/api";
import type { GuardRegistry } from "./widgets";

export type StudioAttempt = {
  id: string;
  ts: string;
  source: string;
  scenario: string | null;
  paypal_live: boolean;
  sku: string;
  amount: number | null;
  method: string;
  device: string;
  email_domain: string | null;
  decision: Decision;
  guard_reason: string | null;
  outcome: Outcome;
  decline_code: string | null;
  decline_category: string | null;
  // Same rule as the War Room's decline rate: the guard's own blocks never reach a bank.
  reached_paypal: boolean;
  incident_id: string | null;
};

export type StudioIncident = {
  id: string;
  opened_at: string;
  closed_at: string | null;
  status: "open" | "closed";
  classification: Classification | null;
  confidence: number | null;
  model_id: string | null;
  ai_available: boolean;
  classifier_version: string | null;
  triggers: string;
  rules_hint: string | null;
  dropped_evidence: number;
  refunds: number;
  refunded_total: number;
};

export type StudioAction = {
  id: string;
  incident_id: string;
  created_at: string;
  type: string;
  status: string;
  rule: string;
  amount: number | null;
  paypal_ref: string | null;
  approved_by: string;
};

export type StudioPayload = {
  as_of: string;
  hours: number;
  ai: { available: boolean; model: string };
  attempts: StudioAttempt[];
  incidents: StudioIncident[];
  actions: StudioAction[];
};

// The simulator's everyday providers (backend app/simulator/scenarios.py COMMON_DOMAINS).
const EVERYDAY = new Set(["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com", "proton.me"]);

const TRAFFIC: Record<string, string> = {
  card_testing: "Simulated attack",
  account_testing: "Simulated attack",
  slow_drip: "Simulated attack",
  flash_sale: "Simulated flash sale",
  paypal_outage: "Simulated PayPal outage",
  normal_night: "Simulated normal night",
};

const STOPPED = new Set<Outcome>(["blocked", "challenged", "sca_required", "sca_failed"]);

// The War Room chart's five groups. Studio colours legend entries in alphabetical order, and ./theme.ts orders its
// palette to match, so each result keeps its War Room colour here too.
const RESULT: Partial<Record<Outcome, string>> = {
  captured: "Paid",
  declined: "Declined by bank",
  blocked: "Stopped by guard",
  challenged: "Stopped by guard",
  sca_required: "Stopped by guard",
  refunded: "Refunded",
  // Sorts last: errors are rare, so the common results keep their palette slots.
  error: "Unavailable (PayPal error)",
};

const STATUS_LABEL: Record<string, string> = {
  executed: "Done",
  awaiting_approval: "Needs owner",
  rejected: "Kept by owner",
  failed: "Failed",
  in_flight: "Sent to PayPal",
};

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const pad = (n: number) => String(n).padStart(2, "0");
// "10-26 06:55": sorts chronologically across the window's days.
const minuteOf = (iso: string) => {
  const d = new Date(iso);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

function traffic(a: StudioAttempt): string {
  if (a.source === "web") return "Real shopper";
  if (a.source === "seed") return "Normal history";
  return TRAFFIC[a.scenario ?? ""] ?? "Simulated";
}

export function incidentLabel(i: Pick<StudioIncident, "classification" | "opened_at">): string {
  return `${i.classification ? CLASSIFICATION_LABEL[i.classification] : "Unclassified"} · ${hhmm(i.opened_at)}`;
}

type FieldSpec = [id: string, name: string, format: AgFormat, description?: string, hide?: boolean];

const fields = (specs: FieldSpec[]): AgFieldDefinition[] =>
  specs.map(([id, name, format, description, hide]) => ({ id, name, format, ...(description && { description }), ...(hide && { hide }) }));

const manyToOne = (id: string, source: string, target: string): AgRelationDefinition => {
  const [sourceTable, sourceField] = source.split(".");
  const [targetTable, targetField] = target.split(".");
  return { id, source: { tableId: sourceTable, fieldId: sourceField }, target: { tableId: targetTable, fieldId: targetField }, type: "many-to-one" };
};

const ratio = (id: string, name: string, description: string, numerator: string, denominator: string): AgExpressionFieldDefinition => ({
  id,
  name,
  description,
  format: "percentageFormat",
  isMeasure: true,
  expression: { operator: "divide", inputs: [{ id: numerator, aggregation: "sum" }, { id: denominator, aggregation: "sum" }] },
});

export function buildStudioData(p: StudioPayload): AgDataSourcesDefinition<GuardRegistry> {
  const incidentNames = new Map(p.incidents.map((i) => [i.id, incidentLabel(i)]));

  const attempts = p.attempts.map((a) => ({
    id: a.id,
    time: a.ts,
    minute: minuteOf(a.ts),
    traffic: traffic(a),
    result: RESULT[a.outcome] ?? "Awaiting buyer",
    outcome: OUTCOME_META[a.outcome]?.label ?? a.outcome,
    decision: DECISION_META[a.decision]?.label ?? a.decision,
    guardReason: a.guard_reason ? (GUARD_REASON_LABEL[a.guard_reason] ?? a.guard_reason) : "Normal checks",
    item: SKU_NAME[a.sku] ?? a.sku,
    amount: a.amount ?? 0,
    method: a.method === "wallet" ? "PayPal wallet" : "Card",
    device: a.device,
    emailDomain: a.email_domain ?? "(none)",
    domainKind: a.email_domain && EVERYDAY.has(a.email_domain) ? "Everyday provider" : "Unusual domain",
    declineCode: a.decline_code ?? "",
    declineCategory: a.decline_category ?? "",
    incidentId: a.incident_id ?? "",
    incident: a.incident_id ? (incidentNames.get(a.incident_id) ?? a.incident_id) : "No incident",
    attempts: 1,
    reached: a.reached_paypal ? 1 : 0,
    paid: a.outcome === "captured" || a.outcome === "authorized" ? 1 : 0,
    declined: a.outcome === "declined" ? 1 : 0,
    stopped: STOPPED.has(a.outcome) ? 1 : 0,
    refunded: a.outcome === "refunded" || a.outcome === "voided" ? 1 : 0,
  }));

  const incidents = p.incidents.map((i) => ({
    id: i.id,
    name: incidentNames.get(i.id) ?? i.id,
    opened: i.opened_at,
    closed: i.closed_at,
    status: i.status === "open" ? "Active" : "Closed",
    verdict: i.classification ? CLASSIFICATION_LABEL[i.classification] : "Unclassified",
    confidence: i.confidence ?? 0,
    // The scorecard decides the class (P9); the AI only explains it. Older incidents were judged by the AI.
    judgedBy: i.classifier_version
      ? `${i.classifier_version}${i.ai_available && i.model_id ? `, explained by ${i.model_id}` : ""}`
      : i.ai_available
        ? (i.model_id ?? "AI")
        : "Fixed rules (AI unavailable)",
    triggers: i.triggers,
    droppedClaims: i.dropped_evidence,
    refunds: i.refunds,
    refundedTotal: i.refunded_total,
  }));

  const actions = p.actions.map((a) => ({
    id: a.id,
    incidentId: a.incident_id,
    incident: incidentNames.get(a.incident_id) ?? a.incident_id,
    time: a.created_at,
    action: ACTION_SHORT[a.type] ?? a.type,
    status: STATUS_LABEL[a.status] ?? a.status,
    rule: a.rule,
    why: ruleLabel(a.rule),
    amount: a.amount ?? 0,
    paypalRef: a.paypal_ref ?? "",
    approvedBy: a.approved_by,
  }));

  return {
    description:
      "Checkout Guard: every checkout attempt at the Wick & Wax candle shop in the last " +
      `${p.hours} hours, the incidents the detector opened, and every action Checkout Guard took. ` +
      "Card-testing bots buy the cheapest item (Tea-light pack, $3) with a new card each time, mostly from unusual email domains, and banks decline them with fraud codes. " +
      "A genuine rush (flash sale) has varied items, everyday email providers and few declines. " +
      "Traffic labelled Simulated comes from the labelled demo simulator; Normal history is seeded background traffic. Money is USD.",
    sources: [
      {
        id: "attempts",
        name: "Checkout attempts",
        description: "One row per checkout attempt the guard checked. Count rows with Attempts; Declined/Stopped/Paid/Refunded are 0/1 flags to sum.",
        data: attempts,
        fields: fields([
          ["id", "Attempt ID", "textFormat", undefined, true],
          ["time", "Time", "dateTimeFormat"],
          ["minute", "Minute", "textFormat", "Local month-day and minute (MM-DD HH:MM) the attempt arrived; the category for per-minute charts."],
          ["traffic", "Traffic", "textFormat", "Who sent it: Real shopper, Normal history, or a labelled simulation (attack, flash sale, outage)."],
          ["result", "Result", "textFormat", "Paid, Declined by bank, Stopped by guard (blocked, human check or 3-D Secure), Refunded or Unavailable (PayPal error)."],
          ["outcome", "Outcome", "textFormat", "The detailed outcome: Paid, Declined by bank, Blocked, Human check, 3-D Secure, Refunded, PayPal error."],
          ["decision", "Guard decision", "textFormat", "The guard's decision before PayPal: Allowed, 3-D Secure, Human check, Blocked."],
          ["guardReason", "Guard reason", "textFormat"],
          ["item", "Item", "textFormat"],
          ["amount", "Amount", "currencyFormat"],
          ["method", "Paid with", "textFormat"],
          ["device", "Device", "textFormat", "Short hash of the device fingerprint. Bots reuse a few devices."],
          ["emailDomain", "Email domain", "textFormat"],
          ["domainKind", "Email domain kind", "textFormat", "Everyday provider (gmail, outlook…) or Unusual domain."],
          ["declineCode", "Bank decline code", "textFormat"],
          ["declineCategory", "Decline category", "textFormat", "PayPal's decline category, e.g. fraud, funds, guess."],
          ["incidentId", "Incident ID", "textFormat", undefined, true],
          ["incident", "Incident", "textFormat", "The incident this attempt fell in, or No incident."],
          ["attempts", "Attempts", "integerFormat"],
          ["reached", "Reached PayPal", "integerFormat", "Attempts that got to PayPal (not blocked or challenged by the guard)."],
          ["paid", "Paid", "integerFormat"],
          ["declined", "Declined", "integerFormat"],
          ["stopped", "Stopped by guard", "integerFormat"],
          ["refunded", "Refunded", "integerFormat"],
        ]),
      },
      {
        id: "incidents",
        name: "Incidents",
        description: "Each time the detector saw something unusual: the AI verdict and what was refunded.",
        data: incidents,
        fields: fields([
          ["id", "Incident ID", "textFormat", undefined, true],
          ["name", "Incident name", "textFormat"],
          ["opened", "Opened", "dateTimeFormat"],
          ["closed", "Closed", "dateTimeFormat"],
          ["status", "Incident status", "textFormat"],
          ["verdict", "Verdict", "textFormat", "Card-testing attack, Stolen-account testing, Genuine rush, PayPal processing issue or Unclear."],
          ["confidence", "Confidence", "percentageFormat"],
          ["judgedBy", "Judged by", "textFormat"],
          ["triggers", "Detector triggers", "textFormat"],
          ["droppedClaims", "AI claims dropped", "integerFormat", "AI evidence items whose numbers did not match ours."],
          ["refunds", "Refunds", "integerFormat"],
          ["refundedTotal", "Refunded total", "currencyFormat"],
        ]),
      },
      {
        id: "actions",
        name: "Guard actions",
        description: "Every action Checkout Guard took or proposed, with the fixed policy rule that allowed it.",
        data: actions,
        fields: fields([
          ["id", "Action ID", "textFormat", undefined, true],
          ["incidentId", "Incident ID", "textFormat", undefined, true],
          ["incident", "Action incident", "textFormat"],
          ["time", "Action time", "dateTimeFormat"],
          ["action", "Action", "textFormat"],
          ["status", "Action status", "textFormat"],
          ["rule", "Policy rule", "textFormat"],
          ["why", "Why", "textFormat"],
          ["amount", "Refund amount", "currencyFormat"],
          ["paypalRef", "PayPal reference", "textFormat"],
          ["approvedBy", "Approved by", "textFormat"],
        ]),
      },
    ],
    relationships: [manyToOne("attempt-incident", "attempts.incidentId", "incidents.id"), manyToOne("action-incident", "actions.incidentId", "incidents.id")],
    expressions: [
      ratio(
        "declineRate",
        "Decline rate",
        "Declined by bank / attempts that reached PayPal (the guard's own blocks never reach a bank), as in the War Room.",
        "attempts.declined",
        "attempts.reached",
      ),
      ratio("stoppedShare", "Stopped share", "Stopped by the guard / attempts.", "attempts.stopped", "attempts.attempts"),
    ],
  };
}
