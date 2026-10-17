// Typed client for the Python backend. All calls go through the Next.js `/api/*` rewrite,
// so the browser talks to one origin and the session cookie just works.

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(typeof body === "object" && body && "detail" in body ? String((body as { detail: unknown }).detail) : `HTTP ${status}`);
  }
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(`/api${path}`, {
    credentials: "include",
    ...rest,
    headers: { ...(json !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const fetcher = <T,>(path: string) => api<T>(path);

// ---- types (mirror backend responses) -------------------------------------------------------

export type Product = { sku: string; name: string; price: string; description: string };

export type Decision = "allow" | "allow_with_sca" | "challenge" | "block";
export type Outcome =
  | "created"
  | "captured"
  | "declined"
  | "error"
  | "blocked"
  | "challenged"
  | "sca_required"
  | "refunded";

export type CheckoutResult = {
  attempt_id: string;
  decision: Decision;
  reason: string;
  outcome: Outcome;
  order_id: string | null;
  order_status: string | null;
  capture_id: string | null;
  decline_code: string | null;
};

export type Classification = "card_testing" | "account_testing" | "genuine_rush" | "paypal_outage" | "unclear";

export type IncidentSummary = {
  id: string;
  status: "open" | "closed";
  opened_at: string;
  closed_at: string | null;
  classification: Classification | null;
  confidence: number | null;
  summary: string | null;
  triggers: string[];
  ai_available: boolean;
};

export type GuardStatus = "quiet" | "under_attack" | "busy_genuine_rush" | "paypal_issue" | "investigating" | "recovering";

export type Summary = {
  status: GuardStatus;
  open_incident: IncidentSummary | null;
  guard_state: { mode: "normal" | "sca_all"; blocked_devices: number; held_skus: string[] };
  last_24h: {
    attempts: number;
    captured: number;
    declined: number;
    stopped_by_guard: number;
    refunded: number;
    refunded_amount: string;
  };
};

export type Attempt = {
  id: string;
  ts: string;
  source: "web" | "simulator" | "seed";
  paypal_live: boolean;
  scenario: string | null;
  sku: string;
  amount: string;
  method: "card" | "wallet";
  fingerprint: string;
  email_domain: string | null;
  decision: Decision;
  outcome: Outcome;
  decline_code: string | null;
  capture_id?: string | null;
  refund_id?: string | null;
  card_last4?: string | null;
};

export type TimeseriesPoint = { minute: string } & Partial<Record<Outcome, number>>;

export type Evidence = { feature: string; window_min: number; observed: number; baseline: number | null; why: string };

export type Verdict = {
  classification: Classification;
  confidence: number;
  summary: string;
  evidence: Evidence[];
  recommended_actions: string[];
  trend: "escalating" | "stable" | "subsiding";
  uncertainty_notes: string;
};

export type ActionRow = {
  id: string;
  incident_id: string;
  type: "SCA_ON" | "BLOCK_FP" | "HOLD_SKU" | "REFUND" | "NOTIFY" | "DEESCALATE";
  status: "executed" | "awaiting_approval" | "rejected" | "failed";
  rule: string;
  params: Record<string, unknown>;
  paypal_ref: string | null;
  approved_by: string;
  created_at: string;
};

export type IncidentDetail = IncidentSummary & {
  verdict: Verdict | null;
  features: Record<string, unknown>;
  signature: { top_sku?: string | null; fingerprints?: string[]; new_email_domains?: string[]; window_start?: string };
  model_id: string | null;
  prompt_version: string | null;
  actions: ActionRow[];
  report: { alert_text: string | null; report_md: string | null; evidence_md: string | null; model_id: string | null; generated_at: string } | null;
  timeline: { ts: string; actor: string; event: string; data: Record<string, unknown> }[];
};

export type SaleWindow = { start: string; end: string; note: string };

export type MerchantSettings = {
  autopilot: boolean;
  kill_switch: boolean;
  max_auto_refund_per_txn: string | number;
  max_auto_refund_per_incident: string | number;
  deescalate_after_min: number;
  sale_windows: SaleWindow[];
};

export type Scenario = { name: string; live_supported: boolean };

export type SimRun = {
  run_id: string;
  scenario: string;
  mode: "live" | "offline";
  total: number;
  done: number;
  outcomes: Partial<Record<Outcome, number>>;
  started_at: string;
  finished_at: string | null;
  error: string | null;
};

export type LiveEvent = { kind: string; data: Record<string, unknown> };
