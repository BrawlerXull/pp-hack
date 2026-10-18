import type { Classification, GuardStatus, Outcome, RulesHint } from "./api";

export const money = (v: string | number) => `$${Number(v).toFixed(2)}`;

export function timeAgo(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export const CLASSIFICATION_LABEL: Record<Classification, string> = {
  card_testing: "Card-testing attack",
  account_testing: "Stolen-account testing",
  genuine_rush: "Genuine rush",
  paypal_outage: "PayPal processing issue",
  unclear: "Unclear activity",
};

export const STATUS_COPY: Record<GuardStatus, { title: string; tone: Tone }> = {
  quiet: { title: "All quiet", tone: "good" },
  under_attack: { title: "Attack in progress, defences active", tone: "bad" },
  busy_genuine_rush: { title: "Busy: real customers", tone: "info" },
  paypal_issue: { title: "PayPal is having trouble", tone: "warn" },
  investigating: { title: "Investigating unusual activity", tone: "warn" },
  recovering: { title: "Recovering: extra checks still on", tone: "warn" },
};

export type Tone = "good" | "bad" | "warn" | "info" | "neutral";

export const OUTCOME_META: Record<Outcome, { label: string; tone: Tone; color: string }> = {
  captured: { label: "Paid", tone: "good", color: "#16a34a" },
  created: { label: "Awaiting buyer", tone: "neutral", color: "#94a3b8" },
  declined: { label: "Declined", tone: "warn", color: "#f59e0b" },
  error: { label: "PayPal error", tone: "warn", color: "#a855f7" },
  blocked: { label: "Blocked", tone: "bad", color: "#dc2626" },
  challenged: { label: "Challenged", tone: "bad", color: "#f97316" },
  sca_required: { label: "3-D Secure", tone: "info", color: "#2563eb" },
  refunded: { label: "Refunded", tone: "info", color: "#0891b2" },
};

export const ACTION_LABEL: Record<string, string> = {
  SCA_ON: "3-D Secure required on every card",
  BLOCK_FP: "Blocked devices showing card-testing behaviour",
  HOLD_SKU: "Paused first-time checkouts of the targeted item",
  REFUND: "Refund",
  NOTIFY: "Owner notified",
  DEESCALATE: "Stood down: checkout back to normal",
  RELAX: "Extra checks switched off: AI judged it was not fraud",
};

export const RULES_HINT_LABEL: Record<RulesHint, string> = {
  fraud_signals: "fraud signals (bank fraud codes, card cycling or a decline spike)",
  processor_trouble: "PayPal errors only",
  returning_customer_rush: "mostly returning customers, few declines",
  unusual_volume: "unusual volume",
};

export const FEATURE_LABEL: Record<string, string> = {
  attempts: "Attempts",
  attempts_per_min: "Attempts per minute",
  velocity_z: "Speed vs normal (z-score)",
  decline_rate: "Decline rate",
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
]);

export function featureValue(feature: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  if (RATIO_FEATURES.has(feature)) return `${Math.round(v * 100)}%`;
  if (feature === "avg_amount") return money(v);
  if (feature === "velocity_z") return `${v > 100 ? Math.round(v) : v.toFixed(1)}×σ`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

export const SKU_NAME: Record<string, string> = {
  "SKU-TEALIGHT-3": "Tea-light pack",
  "SKU-PILLAR-18": "Amber pillar",
  "SKU-GIFTSET-42": "Evening gift set",
};
