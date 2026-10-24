"use client";

import { ArrowRight, Bot, Radar, Scale, Sparkles } from "lucide-react";
import type { IncidentDetail } from "@/lib/api";
import { ACTION_SHORT, CLASSIFICATION_LABEL, TIER_META, TRIGGER_LABEL, clock, timeAgo } from "@/lib/format";
import { DroppedBadge } from "./incident";
import { Badge } from "./ui";

/** Which policy rules ran, with the actions each produced: the last full decision plus any rules-first step
 * (R0, before the AI was asked). Falls back to the rules on the incident's actions. */
export function policyRules(incident: IncidentDetail): [string, string[]][] {
  const pairs: string[] = [];
  const decided = [...incident.timeline].reverse().find((t) => t.event === "decided");
  for (const t of incident.timeline) if (t.event === "rules_first") pairs.push(...((t.data.actions as string[] | undefined) ?? []));
  pairs.push(...((decided?.data.actions as string[] | undefined) ?? []));
  if (pairs.length === 0) pairs.push(...incident.actions.map((a) => `${a.type}:${a.rule}`));
  const byRule = new Map<string, string[]>();
  for (const p of pairs) {
    const [type, rule] = p.split(":");
    if (!rule) continue;
    const list = byRule.get(rule) ?? [];
    const label = ACTION_SHORT[type] ?? type;
    if (!list.includes(label)) list.push(label);
    byRule.set(rule, list);
  }
  return [...byRule.entries()];
}

function Step({ icon: Icon, title, children }: { icon: typeof Radar; title: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 flex-1 rounded-xl border border-slate-200 bg-white p-4">
      <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        <Icon className="size-3.5" /> {title}
      </p>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

const Arrow = () => <ArrowRight className="hidden size-4 shrink-0 self-center text-slate-300 lg:block" />;

/** detect → judge → decide: what fired, who judged it (AI model, kept verdict or rules fallback), which
 * fixed policy rules then ran. Every step is from our records; nothing here is the AI's own claim. */
export function DecisionStrip({ incident }: { incident: IncidentDetail }) {
  const tiers = incident.trigger_tiers ?? {};
  const v = incident.verdict;
  const pct = incident.confidence !== null ? Math.round(incident.confidence * 100) : null;
  const rules = policyRules(incident);
  const kept = incident.ai_available && incident.ai_refresh_failed_at;

  return (
    <div className="flex flex-col gap-3 lg:flex-row">
      <Step icon={Radar} title="Detect">
        <div className="flex flex-wrap gap-1.5">
          {incident.triggers.map((t) => {
            const tier = tiers[t];
            return (
              <Badge key={t} tone={tier ? TIER_META[tier].tone : "neutral"}>
                {TRIGGER_LABEL[t] ?? t}
                {tier && <span className="opacity-60">· {TIER_META[tier].label}</span>}
              </Badge>
            );
          })}
        </div>
        <p className="text-xs text-slate-500">
          Opened {clock(incident.opened_at)}
          {incident.detected_in_s !== null && incident.detected_in_s !== undefined && ` · ${incident.detected_in_s} s after the burst's first attempt`}
        </p>
      </Step>
      <Arrow />
      <Step icon={incident.ai_available ? Sparkles : Bot} title="Judge">
        {!v || incident.classification === null ? (
          <p className="text-slate-500">AI analyst is looking…</p>
        ) : (
          <>
            <p className="font-medium text-slate-800">
              {CLASSIFICATION_LABEL[incident.classification]}
              {pct !== null && <span className="font-normal text-slate-500"> · {pct}% confident</span>}
            </p>
            <p className="text-xs text-slate-600">
              {!incident.ai_available
                ? `Rules fallback${incident.ai_fallback_reason ? ` (${incident.ai_fallback_reason})` : ""}: not an AI verdict`
                : kept
                  ? `Kept previous verdict from ${incident.model_id ?? "the AI"} (re-check failed ${timeAgo(incident.ai_refresh_failed_at!)}${incident.ai_fallback_reason ? `: ${incident.ai_fallback_reason}` : ""})`
                  : `by ${incident.model_id ?? "the AI"}${incident.verdict_at ? ` · ${timeAgo(incident.verdict_at)}` : ""}`}
            </p>
            <DroppedBadge count={incident.dropped_evidence ?? 0} />
          </>
        )}
      </Step>
      <Arrow />
      <Step icon={Scale} title="Decide (fixed rules)">
        {rules.length === 0 ? (
          <p className="text-slate-500">No rule has run yet.</p>
        ) : (
          <ul className="space-y-1">
            {rules.map(([rule, actions]) => (
              <li key={rule} className="flex flex-wrap items-center gap-1.5">
                <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">{rule}</code>
                <span className="text-xs text-slate-600">{actions.join(" · ")}</span>
              </li>
            ))}
          </ul>
        )}
      </Step>
    </div>
  );
}
