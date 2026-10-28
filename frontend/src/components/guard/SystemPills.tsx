"use client";

import useSWR from "swr";
import type { Summary } from "@/lib/api";
import { hhmm, timeAgo } from "@/lib/format";

type PillTone = "good" | "warn" | "bad" | "neutral";

const DOT: Record<PillTone, string> = {
  good: "bg-emerald-400",
  warn: "bg-amber-400",
  bad: "bg-red-400",
  neutral: "bg-ink-3",
};

function Pill({ tone, title, children }: { tone: PillTone; title?: string; children: React.ReactNode }) {
  return (
    <span title={title} className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.04] px-2.5 py-1 text-xs text-ink-2">
      <span className={`size-2 shrink-0 rounded-full ${DOT[tone]}`} />
      {children}
    </span>
  );
}

/**
 * Header pills on every /guard page: is the AI answering, is the detector running, which PayPal, and are
 * live updates flowing. Shares the War Room's /dashboard/summary request (SWR dedupes it).
 */
export function SystemPills({ live }: { live: "connecting" | "live" | "offline" }) {
  const { data } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const sys = data?.system;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {sys ? (
        <>
          <AiPill ai={sys.ai} />
          <WorkerPill worker={sys.worker} />
          <Pill
            tone={sys.paypal.configured ? "good" : "warn"}
            title={
              sys.paypal.card_processing === "seen_working"
                ? "A real sandbox card payment was captured in the last 7 days."
                : "Card processing: unknown (no real sandbox card capture in the last 7 days)."
            }
          >
            PayPal sandbox{sys.paypal.configured ? "" : ": no credentials"}
            {sys.paypal.configured && sys.paypal.card_processing === "seen_working" && " · cards working"}
          </Pill>
          {sys.simulator_enabled && (
            <Pill tone="neutral" title="Demo controls are on. Simulated traffic is labelled SIM everywhere.">
              Simulator on
            </Pill>
          )}
        </>
      ) : (
        <span className="h-6 w-64 animate-pulse rounded-full bg-white/[0.06]" />
      )}
      <Pill tone={live === "live" ? "good" : live === "connecting" ? "warn" : "neutral"}>
        {live === "live" ? "Live updates on" : live === "connecting" ? "Connecting…" : "Live updates paused"}
      </Pill>
    </div>
  );
}

function AiPill({ ai }: { ai: Summary["system"]["ai"] }) {
  const lastError = ai.last_error ? `Last error ${ai.last_error_at ? timeAgo(ai.last_error_at) : ""}: ${ai.last_error}` : "";
  if (ai.mode === "live")
    return (
      <Pill tone={ai.reason ? "warn" : "good"} title={[ai.reason, lastError].filter(Boolean).join("\n") || undefined}>
        AI live{ai.last_model ? ` · ${ai.last_model}` : ""}
      </Pill>
    );
  if (ai.mode === "cooling_down")
    return (
      <Pill tone="warn" title={[ai.reason, "The scorecard still decides; explanations use Checkout Guard's own template until then.", lastError].filter(Boolean).join("\n")}>
        AI cooling down{ai.until ? ` until ${hhmm(ai.until)}` : ""}
      </Pill>
    );
  return (
    <Pill tone="bad" title={[ai.reason, "The scorecard still decides and acts; explanations use Checkout Guard's own template, and unsure cases get no AI second opinion (fixed safety rules).", lastError].filter(Boolean).join("\n")}>
      AI off · scorecard only{ai.reason ? ` · ${ai.reason.split(":")[0]}` : ""}
    </Pill>
  );
}

function WorkerPill({ worker }: { worker: Summary["system"]["worker"] }) {
  if (!worker.alive)
    return (
      <Pill tone="bad" title="The background worker detects attacks. Start it with: uv run python -m app.worker.main">
        Detector stopped{worker.heartbeat_at ? ` · last seen ${timeAgo(worker.heartbeat_at)}` : ""}
      </Pill>
    );
  const since = worker.started_at ? `Running since ${new Date(worker.started_at).toLocaleString()}.` : "";
  const lastPass = worker.last_tick_at ? `Last finished pass ${timeAgo(worker.last_tick_at)}.` : "No pass has finished since it started.";
  if (worker.state === "failing")
    return (
      <Pill tone="bad" title={[`The last detector pass failed (${worker.last_tick_error ?? "error"}). See the worker log.`, lastPass].join("\n")}>
        Detector failing
      </Pill>
    );
  if (worker.state === "stalled")
    return (
      <Pill tone="bad" title={[lastPass, "Nothing is being detected. Check or restart the worker."].join("\n")}>
        Detector stalled{worker.assessing ? " (waiting on AI)" : ""}
      </Pill>
    );
  if (worker.state === "slow")
    return (
      <Pill tone="warn" title={[lastPass, since].filter(Boolean).join("\n")}>
        {worker.assessing ? "Detector waiting on AI" : "Detector slow"}
      </Pill>
    );
  return (
    <Pill tone="good" title={since || undefined}>
      Detector running
    </Pill>
  );
}
