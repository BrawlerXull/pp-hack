// Typed client for the Python backend. All calls go through the Next.js `/api/*` rewrite,
// so the browser talks to one origin and the session cookie just works.

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(errorMessage(status, body));
  }

  // Structured FastAPI detail ({code, message}), e.g. the 409 for approving a refund on a disputed payment.
  get code(): string | undefined {
    const d = typeof this.body === "object" && this.body ? (this.body as { detail?: unknown }).detail : undefined;
    return typeof d === "object" && d && "code" in d ? String((d as { code: unknown }).code) : undefined;
  }
}

function errorMessage(status: number, body: unknown): string {
  const d = typeof body === "object" && body && "detail" in body ? (body as { detail: unknown }).detail : undefined;
  if (typeof d === "object" && d && "message" in d) return String((d as { message: unknown }).message);
  // Pydantic validation: a list of {loc, msg}; the first one is enough for a form.
  if (Array.isArray(d) && d[0] && typeof d[0] === "object" && "msg" in d[0]) return String(d[0].msg).replace(/^Value error, /, "");
  return d !== undefined ? String(d) : `HTTP ${status}`;
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

// GET /shop/config: public keys the shop page needs. turnstile_site_key is "" when the human check is a click-through.
export type ShopConfig = {
  paypal_client_id: string;
  environment: "sandbox";
  currency: string;
  turnstile_site_key: string;
  turnstile_test_keys: boolean;
  contact_email: string;
  // PayPal FraudNet source id; "" when not configured (then no device-data script is loaded).
  fraudnet_source_id: string;
};

export type Decision = "allow" | "allow_with_sca" | "challenge" | "block";
export type Outcome =
  | "created"
  | "captured"
  | "declined"
  | "error"
  | "blocked"
  | "challenged"
  | "sca_required"
  | "sca_failed"
  | "refunded"
  // Payment hold: authorised during an incident, not settled yet; voided = released, never charged.
  | "authorized"
  | "voided";

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

// POST /shop/orders/{id}/capture. liability_shift: the bank's 3-D Secure answer for a card (YES, POSSIBLE,
// NO, UNKNOWN), null when 3-D Secure did not run.
export type CaptureResult = {
  order_id: string;
  outcome: Outcome;
  capture_id: string | null;
  decline_code: string | null;
  decline_category: string | null;
  liability_shift: string | null;
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
  // Last AI re-check failed and the previous AI verdict was kept (the badge keeps showing that model).
  ai_refresh_failed_at: string | null;
  // What the triggers alone suggest; shown next to an "unclear" verdict, never used as the classification.
  rules_hint?: RulesHint | null;
  trigger_tiers?: Record<string, TriggerTier>;
  // AI evidence items that did not match our computed numbers and were dropped before display.
  dropped_evidence?: number;
  // Why the last assessment could not use the AI (rules fallback or kept verdict); null when it answered.
  ai_fallback_reason?: string | null;
  // Who wrote the verdict's words: a Gemini model id, or "template" (Checkout Guard's own wording).
  model_id: string | null;
  verdict_at: string | null;
  // P9: the deterministic scorecard decides the classification; Gemini explains it or breaks a tie.
  classifier_version?: string | null;
  decided_by?: DecidedBy | null;
  class_probs?: Record<string, number> | null;
  class_margin?: number | null;
  scorecard?: Scorecard | null;
};

export type DecidedBy = "scorecard" | "scorecard+gemini_agree" | "gemini_disagree_unclear";

export type ScoreReason = {
  cls: string;
  feature: string | null; // null: context that is not a feature (the owner's sale window)
  window_min: number;
  observed: number | null;
  points: number;
  text: string;
  supporting: boolean;
};

export type Tiebreak = {
  outcome: "agree" | "disagree" | "unavailable" | "unverified";
  gemini: Classification | null;
  model: string | null;
};

export type Scorecard = {
  version: string;
  classification: Classification;
  confidence: number;
  top_class: Classification;
  runner_up: Classification;
  margin: number;
  probabilities: Record<string, number>;
  scores: Record<string, number>;
  window_min: number;
  top_reasons: ScoreReason[];
  notes: string[];
  tiebreak: Tiebreak | null;
};

export type TriggerTier = "volume" | "fraud" | "ops" | "holding";

export type RulesHint = "fraud_signals" | "processor_trouble" | "returning_customer_rush" | "unusual_volume";

export type GuardStatus = "quiet" | "under_attack" | "busy_genuine_rush" | "paypal_issue" | "investigating" | "recovering";

export type LiveWindow = {
  attempts: number;
  attempts_vs_normal: number | null;
  decline_rate: number;
  decline_rate_vs_normal: number | null;
  captured: number;
  stopped: number;
};

export type SystemStatus = {
  worker: {
    alive: boolean;
    // running; slow (no finished pass for 30 s); stalled (2.5 min); failing (the last pass raised); stopped.
    state: "running" | "slow" | "stalled" | "failing" | "stopped";
    // An incident is waiting on its AI assessment: the only time "waiting on AI" is true.
    assessing: boolean;
    heartbeat_at: string | null;
    last_tick_at: string | null;
    last_tick_error: string | null;
    last_tick_error_at: string | null;
    started_at: string | null;
  };
  ai: {
    mode: "live" | "cooling_down" | "rules_only";
    until: string | null;
    reason: string | null;
    last_model: string | null;
    last_answer_at: string | null;
    last_error: string | null;
    last_error_at: string | null;
  };
  paypal: { environment: "sandbox"; configured: boolean; card_processing: "seen_working" | "unknown" };
  simulator_enabled: boolean;
  // The detector's last 5/15-minute numbers; null while the worker is down (they would be stale).
  live: { "5m": LiveWindow; "15m": LiveWindow; at: string } | null;
};

export type DemoActivity = {
  kind: "other_visitor" | "idle_restore";
  event: string;
  at: string;
  data: Record<string, unknown>;
};

export type Summary = {
  status: GuardStatus;
  open_incident: IncidentSummary | null;
  system: SystemStatus;
  switches: { kill_switch: boolean; autopilot: boolean };
  sale_window: { end: string; note: string } | null;
  guard_state: { mode: "normal" | "sca_all"; blocked_devices: number; held_skus: string[] };
  // Shared hosted demo: the latest change another visitor made, or the idle stand-down (30 min window).
  demo_activity?: DemoActivity | null;
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
  // observed: seen only through PayPal webhooks (a checkout that does not call the guard); api: another
  // shop's checkout calling POST /v1/guard/check.
  source: "web" | "simulator" | "seed" | "observed" | "api";
  paypal_live: boolean;
  scenario: string | null;
  sku: string;
  amount: string;
  // unknown: an observed payment whose order PayPal has not shown us yet (observer mode).
  method: "card" | "wallet" | "unknown";
  fingerprint: string;
  email_domain: string | null;
  decision: Decision;
  // Why the guard decided that (backend app/guard/check.py), e.g. fingerprint_blocked, sca_mode.
  guard_reason: string | null;
  outcome: Outcome;
  decline_code: string | null;
  decline_category?: string | null;
  capture_id?: string | null;
  refund_id?: string | null;
  card_last4?: string | null;
  card_brand?: string | null;
  sca_liability_shift?: string | null;
};

// t: bucket start (bucket_s wide). minute: the same, older name.
export type TimeseriesPoint = { t: string; minute: string } & Partial<Record<Outcome, number>>;

export type AlertRow = { id: number; ts: string; incident_id: string; text: string | null; model_id: string | null };

export type EvidenceVsNormal = { feature: string; window_min: number; observed: number | null; normal: number | null; multiplier: number | null };

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
  type: "SCA_ON" | "BLOCK_FP" | "HOLD_SKU" | "REFUND" | "VOID" | "NOTIFY" | "DEESCALATE" | "RELAX";
  // in_flight: a refund sent to PayPal whose answer is not in yet (re-sent with the same request id if lost).
  status: "executed" | "awaiting_approval" | "rejected" | "failed" | "in_flight";
  rule: string;
  params: Record<string, unknown>;
  paypal_ref: string | null;
  error: string | null;
  approved_by: string;
  created_at: string;
};

// A chargeback or dispute and what to do about it (app/disputes): rules recommend, the AI drafts, the owner decides.
export type DisputeCase = {
  id: string;
  attempt_id: string;
  incident_id: string | null;
  reason: string | null;
  amount: string;
  response_due: string | null;
  status: "open" | "accepted" | "contested" | "dismissed" | "failed" | "resolved";
  recommendation: "accept" | "contest" | "review";
  rule: string;
  evidence_type: string | null;
  buyer_says: string | null;
  contradicts_reason: boolean;
  note: string;
  note_by: string;
  paypal_live: boolean;
  created_at: string;
  decided_at: string | null;
  paypal_result: string | null;
};

export type DeviceRow = { fingerprint: string; reason: string | null; incident_id: string | null; created_at: string; expires_at: string };
export type Devices = { blocked: DeviceRow[]; trusted: DeviceRow[] };

export type IncidentDetail = IncidentSummary & {
  verdict: Verdict | null;
  features: Record<string, unknown>;
  signature: { top_sku?: string | null; fingerprints?: string[]; strong_fingerprints?: string[]; weak_fingerprints?: string[]; new_email_domains?: string[]; suspect_domains?: string[]; window_start?: string };
  prompt_version: string | null;
  ai_refresh_failures: number;
  // One row per verdict evidence item: our own number, the shop's normal and the ×N (never the AI's baseline).
  evidence_vs_normal: EvidenceVsNormal[];
  // Seconds from the burst's first attempt to the incident opening.
  detected_in_s: number | null;
  // Payments in the incident's window seen through a guard check vs. only through PayPal webhooks (observer mode).
  seen_via?: { guarded: number; observed: number };
  actions: ActionRow[];
  report: { alert_text: string | null; report_md: string | null; evidence_md: string | null; model_id: string | null; generated_at: string } | null;
  timeline: { ts: string; actor: string; event: string; data: Record<string, unknown> }[];
};

export type SaleWindow = { start: string; end: string; note: string };

export type Role = "owner" | "viewer";

export type Member = {
  id: string;
  email: string;
  name: string;
  role: Role;
  is_demo: boolean;
  created_at: string;
  last_login_at: string | null;
};

export type Me = Member & { shop_id: string };

export type AuthOptions = { demo_email: string; signup_enabled: boolean; signup_role: Role };

export type PushKind = "incident" | "approval" | "dispute" | "report";

export type PushConfig = {
  enabled: boolean;
  firebase: Record<string, string> | null;
  vapid_key: string | null;
  kinds: PushKind[];
  devices: { id: string; label: string; created_at: string }[];
};

export type MerchantSettings = {
  autopilot: boolean;
  kill_switch: boolean;
  max_auto_refund_per_txn: string | number;
  max_auto_refund_per_incident: string | number;
  max_auto_refund_per_day: string | number;
  deescalate_after_min: string | number;
  review_hold_min: string | number;
  sale_windows: SaleWindow[];
};

export type Scenario = { name: string; live_supported: boolean };

export type SimRun = {
  run_id: string;
  scenario: string;
  mode: "live" | "offline" | "observer";
  total: number;
  done: number;
  outcomes: Partial<Record<Outcome, number>>;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  seed: number | null;
  context: RunContext;
  stopped_by_guard: number;
  reached_paypal: number;
};

/** Defences already in place when a demo run started (they explain a run that never reached PayPal). */
export type RunContext = Partial<{
  open_incident: string | null;
  open_incident_class: Classification | null;
  sca_all: boolean;
  held_skus: string[];
  blocked_devices: number;
  defences_active: boolean;
}>;

export type LiveEvent = { kind: string; data: Record<string, unknown> };

// ---- Developers page (GET /integration/*) ----------------------------------------------------------

export type ApiKeyRow = { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null };
export type NewApiKey = ApiKeyRow & { key: string }; // the secret: in the create answer only

export type IntegrationStatus = {
  last_call_at: string | null;
  checks_24h: number;
  latest_check: { ts: string; sku: string; decision: Decision; outcome: Outcome } | null;
  last_webhook_at: string | null;
  webhook_path: string;
};

// ---- Ask Guard (POST /assistant/chat) --------------------------------------------------------------

export type ChatRole = "user" | "assistant" | "note";

export type Proposal = {
  id: string;
  tool: string;
  title: string;
  detail: string;
  danger: boolean;
  owner_only: boolean;
  request: { method: "POST" | "PATCH" | "PUT" | "DELETE"; path: string; body: Record<string, unknown> | null };
};

export type AssistantReply = { reply: string; proposals: Proposal[]; looked_up: string[]; model: string | null };
