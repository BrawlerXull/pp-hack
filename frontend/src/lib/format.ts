import type { ActionRow, Classification, Decision, GuardStatus, Outcome, RulesHint, TriggerTier } from "./api";

export const money = (v: string | number) => `$${Number(v).toFixed(2)}`;

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** "42 s", "3 min 10 s", "1 h 5 min" */
export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 1) return "< 1 s";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 && m < 10 ? `${m} min ${s % 60} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

/** "06:10": compact 24-hour minute label for chart axes. */
export const minuteLabel = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

// "×12 vs normal"; one decimal below 10.
export const times = (m: number) => `×${m >= 10 ? Math.round(m) : m.toFixed(1)}`;

export const hhmm = (iso: string | number | Date) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export type Tone = "good" | "bad" | "warn" | "info" | "neutral";

export const CLASSIFICATION_LABEL: Record<Classification, string> = {
  card_testing: "Card-testing attack",
  account_testing: "Stolen-account testing",
  genuine_rush: "Genuine rush",
  paypal_outage: "PayPal processing issue",
  unclear: "Unclear activity",
};

export function classificationTone(c: Classification | null | undefined): Tone {
  if (c === "card_testing" || c === "account_testing") return "bad";
  if (c === "genuine_rush") return "info";
  if (c === "paypal_outage" || c === "unclear") return "warn";
  return "neutral";
}

export const STATUS_COPY: Record<GuardStatus, { title: string; tone: Tone; line: string }> = {
  quiet: { title: "All quiet", tone: "good", line: "Checkout Guard is watching every payment. Nothing unusual right now." },
  under_attack: { title: "Attack in progress", tone: "bad", line: "Defences are active. Real customers can still pay." },
  busy_genuine_rush: { title: "Busy: real customers", tone: "info", line: "A genuine rush. Nobody is being blocked." },
  paypal_issue: { title: "PayPal is having trouble", tone: "warn", line: "Errors are coming from the processor, not from an attacker." },
  investigating: { title: "Investigating unusual activity", tone: "warn", line: "The AI analyst is looking at the traffic." },
  recovering: { title: "Standing down", tone: "warn", line: "The attack has stopped. Extra checks switch off once traffic settles." },
};

export const OUTCOME_META: Record<Outcome, { label: string; tone: Tone; color: string }> = {
  captured: { label: "Paid", tone: "good", color: "var(--color-series-paid)" },
  created: { label: "Awaiting buyer", tone: "neutral", color: "var(--color-ink-3)" },
  declined: { label: "Declined by bank", tone: "warn", color: "var(--color-series-declined)" },
  error: { label: "PayPal error", tone: "warn", color: "var(--color-series-error)" },
  blocked: { label: "Blocked", tone: "bad", color: "var(--color-series-stopped)" },
  challenged: { label: "Human check", tone: "bad", color: "var(--color-series-stopped)" },
  sca_required: { label: "3-D Secure", tone: "info", color: "var(--color-series-stopped)" },
  refunded: { label: "Refunded", tone: "info", color: "var(--color-series-refunded)" },
  sca_failed: { label: "3-D Secure failed", tone: "bad", color: "var(--color-series-stopped)" },
  authorized: { label: "Held: approved, not settled", tone: "info", color: "var(--color-series-paid)" },
  voided: { label: "Voided: never charged", tone: "good", color: "var(--color-series-refunded)" },
};

/** The activity chart folds the outcomes into five stories, in validated palette order. */
export const SERIES = [
  { key: "paid", label: "Paid", color: "#3987e5", outcomes: ["captured", "authorized"] },
  { key: "declined", label: "Declined by bank", color: "#d95926", outcomes: ["declined"] },
  { key: "stopped", label: "Stopped by Checkout Guard", color: "#199e70", outcomes: ["blocked", "challenged", "sca_required", "sca_failed"] },
  { key: "refunded", label: "Refunded or voided before dispute", color: "#c98500", outcomes: ["refunded", "voided"] },
  { key: "error", label: "PayPal error", color: "#9085e9", outcomes: ["error"] },
] as const satisfies readonly { key: string; label: string; color: string; outcomes: readonly Outcome[] }[];

export const DECISION_META: Record<Decision, { label: string; tone: Tone }> = {
  allow: { label: "Allowed", tone: "neutral" },
  allow_with_sca: { label: "3-D Secure", tone: "info" },
  challenge: { label: "Human check", tone: "warn" },
  block: { label: "Blocked", tone: "bad" },
};

export const ACTION_LABEL: Record<ActionRow["type"], string> = {
  SCA_ON: "3-D Secure switched on for every card",
  BLOCK_FP: "Blocked devices showing card-testing behaviour",
  HOLD_SKU: "Human check for first-time buyers of the targeted item",
  REFUND: "Refund",
  VOID: "Held payment voided: never charged, no fee",
  NOTIFY: "Owner notified",
  DEESCALATE: "Stood down: checkout back to normal",
  RELAX: "Extra checks switched off: judged not to be fraud",
};

/** How each defence uses PayPal, shown next to the action so the integration is visible. */
export const ACTION_MECHANISM: Record<ActionRow["type"], string> = {
  SCA_ON: "Orders v2 · verification.method = SCA_ALWAYS",
  BLOCK_FP: "Stopped before a PayPal order is created",
  HOLD_SKU: "Guard check before order creation",
  REFUND: "Payments v2 · captures/{id}/refund",
  VOID: "Payments v2 · authorizations/{id}/void",
  NOTIFY: "Phone alert + morning report",
  DEESCALATE: "Guard state restored",
  RELAX: "Guard state restored",
};

const RULE_LABEL: Record<string, string> = {
  R0_fraud_signal: "Fixed rule: fraud declines seen, applied before the AI answered",
  R1_kill_switch: "Kill switch is on: watching only",
  R3_soft_defence: "Not clear yet: soft defence only",
  R3b_ai_down_fraud_signal: "AI unavailable: fixed rules acted on fraud signals",
  R3c_returning_customer_rush: "Mostly returning customers: notify only",
  R3d_processor_trouble_notify_only: "Processor errors only: notify, don't add friction",
  R4_attack: "Scorecard verdict: attack (≥ 70% confident)",
  R4_attack_during_sale_soft_only: "Attack during your sale: 3-D Secure only, no blocks",
  R5_refund_matched: "Verdict ≥ 85% confident · strong match to the attack · within your cap",
  R5_over_cap_needs_owner: "Above your auto-refund limit: needs your approval",
  R5_over_daily_cap_needs_owner: "Above today's auto-refund limit (all incidents): needs your approval",
  R5_weak_match_needs_owner: "Weak match to the attack: needs your approval",
  R5_sca_passed_needs_owner: "Buyer passed 3-D Secure, which bots rarely do: needs your approval",
  R5_stale_verdict_needs_owner: "Verdict is out of date: needs your approval",
  always_notify: "Owner kept in the loop",
  owner_closed: "You stood the guard down",
  auto_deescalate_quiet: "Quiet again: defences switched off automatically",
  demo_idle_restore: "Demo idle: stood down to its defaults for the next visitor",
};

export function ruleLabel(rule: string): string {
  if (RULE_LABEL[rule]) return RULE_LABEL[rule];
  if (rule.startsWith("R2_relax_")) return "Judged harmless: extra checks switched off";
  if (rule.startsWith("R2_")) return "Judged harmless: notify only";
  return rule.replace(/_/g, " ");
}

export const FEATURE_LABEL: Record<string, string> = {
  attempts: "Attempts",
  attempts_per_min: "Attempts per minute",
  velocity_z: "Speed vs normal (z-score)", // incidents from before P9
  expected_attempts: "Normal attempts for this hour",
  velocity_surprise: "Volume surprise (−log10 p)",
  decline_rate: "Decline rate",
  decline_surprise: "Decline surprise (−log10 p)",
  fraud_declines: "Declines coded as fraud",
  fraud_decline_share: "Share of declines coded as fraud",
  error_rate: "PayPal error rate",
  captured: "Payments that went through",
  blocked_or_challenged: "Stopped by the guard",
  sca_required: "Stopped by 3-D Secure",
  unique_fingerprints: "Distinct devices",
  top_fingerprint_share: "Share from busiest device",
  unique_cards_ratio: "New card per attempt",
  unique_payers_ratio: "New PayPal account per attempt",
  top_sku_share: "Share on one item",
  cheapest_sku_share: "Share on cheapest item",
  cheapest_sku_attempts: "Attempts on cheapest item",
  cheapest_sku_cards: "Distinct cards on cheapest item",
  cheapest_sku_payers: "Distinct PayPal accounts on cheapest item",
  cheapest_sku_decline_rate: "Decline rate on cheapest item",
  avg_amount: "Average amount",
  new_email_domain_share: "Never-seen email domains",
  known_customer_share: "Returning customers",
  wallet_share: "PayPal wallet share",
  cards_per_device_max: "Most cards on one device",
  cards_per_device_p90: "Cards per device (90th pct)",
  payers_per_device_max: "Most PayPal accounts on one device",
  payers_per_device_p90: "PayPal accounts per device (90th pct)",
  aged_wallet_orders: "Wallet orders old enough to approve",
  unapproved_wallet_share: "Wallet orders never approved",
  interarrival_cv: "Timing irregularity (1 ≈ human)",
  error_devices: "Devices hit by errors",
  error_top_device_share: "Errors from one device",
  error_known_share: "Errors hitting returning customers",
  error_top_sku_share: "Errors on one item",
  distinct_skus: "Distinct items",
  sku_entropy: "Basket variety (0-1)",
  amount_cv: "Amount spread",
  probing_decline_share: "Declines for wrong CVV/expiry",
  customer_decline_share: "Declines that are honest bad luck",
  processor_share: "Failures that are PayPal errors",
  non_customer_declines: "Fraud, guessing or unexplained declines",
  slow_decline_cusum: "Slow-decline build-up (CUSUM)",
  multi_card_devices: "Devices with 3+ cards",
  suspect_declines: "Stolen-card or guessing declines",
  suspect_decline_devices: "Devices with stolen-card or guessing declines",
  client_errors: "Errors a client caused (not counted)",
};

export const DECIDED_BY_LABEL: Record<string, string> = {
  scorecard: "scorecard",
  "scorecard+gemini_agree": "scorecard, confirmed by the AI second opinion",
  gemini_disagree_unclear: "scorecard unsure, AI disagreed: stays unclear",
};

const RATIO_FEATURES = new Set([
  "decline_rate",
  "fraud_decline_share",
  "cheapest_sku_decline_rate",
  "error_rate",
  "top_fingerprint_share",
  "unique_cards_ratio",
  "unique_payers_ratio",
  "top_sku_share",
  "cheapest_sku_share",
  "new_email_domain_share",
  "known_customer_share",
  "wallet_share",
  "unapproved_wallet_share",
  "error_top_device_share",
  "error_known_share",
  "error_top_sku_share",
  "probing_decline_share",
  "customer_decline_share",
  "processor_share",
]);

export function featureValue(feature: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  if (RATIO_FEATURES.has(feature)) return `${Math.round(v * 100)}%`;
  if (feature === "avg_amount") return money(v);
  if (feature === "velocity_z") return `${v > 100 ? Math.round(v) : v.toFixed(1)}×σ`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

export const TRIGGER_LABEL: Record<string, string> = {
  velocity_spike: "Traffic spike",
  sku_concentration: "One item targeted",
  decline_spike: "Decline spike",
  fraud_declines: "Bank fraud codes",
  cheap_sku_card_cycling: "Cards cycling on cheapest item",
  slow_declines: "Slow run of declines",
  processor_errors: "PayPal errors",
  blocked_activity: "Blocked devices still trying",
};

export const TIER_META: Record<TriggerTier, { label: string; tone: Tone }> = {
  fraud: { label: "fraud", tone: "bad" },
  volume: { label: "volume", tone: "info" },
  ops: { label: "ops", tone: "warn" },
  holding: { label: "holding", tone: "neutral" },
};

// Why the guard decided what it did for one checkout (backend app/guard/check.py).
export const GUARD_REASON_LABEL: Record<string, string> = {
  normal: "Normal checks",
  guard_disabled: "Kill switch on: guard off",
  fingerprint_blocked: "Device blocked",
  device_velocity_limit: "Too many tries from this device",
  device_velocity: "Fast retries: challenged",
  network_velocity: "Many new devices with declines from one network: challenged",
  sku_on_hold: "Item on hold for new shoppers",
  sca_mode: "First-time shopper during an attack: 3-D Secure",
  returning_customer: "Returning customer: no extra checks",
  payment_hold: "First-time shopper during an attack: payment held",
  guard_unavailable: "Guard too slow: let through (fail-open)",
};

export const ACTION_SHORT: Record<string, string> = {
  SCA_ON: "3-D Secure",
  BLOCK_FP: "Block devices",
  HOLD_SKU: "Hold item",
  REFUND: "Refund",
  VOID: "Void hold",
  NOTIFY: "Notify owner",
  DEESCALATE: "Stand down",
  RELAX: "Relax checks",
};

export const RULES_HINT_LABEL: Record<RulesHint, string> = {
  fraud_signals: "fraud signals (bank fraud codes, card cycling or a decline spike)",
  processor_trouble: "PayPal errors only",
  returning_customer_rush: "mostly returning customers, few declines",
  unusual_volume: "unusual volume",
};

export const SKU_NAME: Record<string, string> = {
  "SKU-TEALIGHT-3": "Tea-light pack",
  "SKU-PILLAR-18": "Amber pillar",
  "SKU-GIFTSET-42": "Evening gift set",
};
