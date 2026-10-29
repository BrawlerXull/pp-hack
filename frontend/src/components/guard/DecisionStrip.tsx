"use client";

import { ArrowRight, Calculator, Radar, Scale } from "lucide-react";
import type { IncidentDetail } from "@/lib/api";
import { ACTION_SHORT, CLASSIFICATION_LABEL, FEATURE_LABEL, TIER_META, TRIGGER_LABEL, clock, featureValue, timeAgo } from "@/lib/format";
import { ClassBars, DecisionSource, DroppedBadge } from "./incident";
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
    <div className="min-w-0 flex-1 rounded-xl border border-line bg-gray-50 p-4">
      <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-ink-3">
        <Icon className="size-3.5" /> {title}
      </p>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

const Arrow = () => <ArrowRight className="hidden size-4 shrink-0 self-center text-ink-3/60 lg:block" />;

/** detect → judge → decide: what fired, how the scorecard judged it (probabilities, top reasons, the AI
 * tie-break if one was needed, who wrote the words), which fixed policy rules then ran. Every step is from
 * our records; nothing here is the AI's own claim. */
export function DecisionStrip({ incident }: { incident: IncidentDetail }) {
  const tiers = incident.trigger_tiers ?? {};
  const v = incident.verdict;
  const pct = incident.confidence !== null ? Math.round(incident.confidence * 100) : null;
  const rules = policyRules(incident);
  const reasons = incident.scorecard?.top_reasons ?? [];

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
        <p className="text-xs text-ink-3">
          Opened {clock(incident.opened_at)}
          {incident.detected_in_s !== null && incident.detected_in_s !== undefined && ` · ${incident.detected_in_s} s after the burst's first attempt`}
        </p>
      </Step>
      <Arrow />
      <Step icon={Calculator} title="Judge (scorecard)">
        {!v || incident.classification === null ? (
          <p className="text-ink-3">Scoring…</p>
        ) : (
          <>
            <p className="font-medium text-ink-1">
              {CLASSIFICATION_LABEL[incident.classification]}
              {pct !== null && (
                <span className="font-normal text-ink-3">
                  {" "}
                  · {incident.classification === "unclear" ? "top class" : "confidence"} {pct}%
                </span>
              )}
              {incident.verdict_at && <span className="font-normal text-ink-3"> · {timeAgo(incident.verdict_at)}</span>}
            </p>
            {incident.class_probs && <ClassBars probs={incident.class_probs} />}
            {reasons.length > 0 && (
              <ul className="space-y-0.5 text-xs text-ink-2">
                {reasons.slice(0, 3).map((r, i) => (
                  <li key={i}>
                    <span className="tabular-nums text-ink-3">+{r.points}</span> {r.text}
                    {r.feature && r.observed !== null && (
                      <span className="text-ink-3">
                        {" "}
                        ({FEATURE_LABEL[r.feature] ?? r.feature}: {featureValue(r.feature, r.observed)})
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <DecisionSource incident={incident} />
            {incident.ai_refresh_failed_at && (
              <p className="text-xs text-amber-700">
                Kept the AI-confirmed verdict (re-check failed {timeAgo(incident.ai_refresh_failed_at)})
              </p>
            )}
            <DroppedBadge count={incident.dropped_evidence ?? 0} />
          </>
        )}
      </Step>
      <Arrow />
      <Step icon={Scale} title="Decide (fixed rules)">
        {rules.length === 0 ? (
          <p className="text-ink-3">No rule has run yet.</p>
        ) : (
          <ul className="space-y-1">
            {rules.map(([rule, actions]) => (
              <li key={rule} className="flex flex-wrap items-center gap-1.5">
                <code className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-ink-2">{rule}</code>
                <span className="text-xs text-ink-2">{actions.join(" · ")}</span>
              </li>
            ))}
          </ul>
        )}
      </Step>
    </div>
  );
}
