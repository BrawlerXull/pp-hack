"use client";

import { ChevronDown } from "lucide-react";
import useSWR from "swr";
import type { Summary } from "@/lib/api";
import { hhmm, timeAgo } from "@/lib/format";

type Health = "good" | "warn" | "bad" | "neutral";

const DOT: Record<Health, string> = {
  good: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  neutral: "bg-gray-400",
};

type Check = { name: string; health: Health; value: string; detail?: string };

/**
 * One compact system-status control in the header of every /guard page: "All systems normal", or the one
 * thing that is not. Opens (native <details>, so keyboard and screen readers get it for free) to the four
 * checks: the AI, the detector, PayPal, live updates. Shares the War Room's /dashboard/summary request.
 */
export function SystemPills({ live }: { live: "connecting" | "live" | "offline" }) {
  const { data } = useSWR<Summary>("/dashboard/summary", { refreshInterval: 10_000 });
  const sys = data?.system;
  if (!sys) return <span className="h-8 w-44 animate-pulse rounded-lg bg-gray-100" aria-hidden />;

  const checks: Check[] = [
    aiCheck(sys.ai),
    workerCheck(sys.worker),
    {
      name: "PayPal",
      health: sys.paypal.configured ? "good" : "warn",
      value: sys.paypal.configured ? `Sandbox${sys.paypal.card_processing === "seen_working" ? " · cards working" : ""}` : "No credentials",
      detail:
        sys.paypal.card_processing === "seen_working"
          ? "A real sandbox card payment was captured in the last 7 days."
          : "No real sandbox card capture in the last 7 days.",
    },
    {
      name: "Live updates",
      health: live === "live" ? "good" : live === "connecting" ? "warn" : "neutral",
      value: live === "live" ? "Connected" : live === "connecting" ? "Connecting…" : "Paused",
    },
  ];
  const worst: Health = checks.some((c) => c.health === "bad") ? "bad" : checks.some((c) => c.health === "warn") ? "warn" : "good";
  const problem = checks.find((c) => c.health === worst && worst !== "good");
  const label = problem ? `${problem.name}: ${problem.value}` : "All systems normal";

  return (
    <details className="group relative">
      <summary className="flex h-8 min-w-0 cursor-pointer list-none items-center gap-2 whitespace-nowrap rounded-lg border border-line bg-white px-2.5 text-[13px] text-ink-2 hover:bg-gray-50 [&::-webkit-details-marker]:hidden">
        <span className={`size-2 rounded-full ${DOT[worst]}`} aria-hidden />
        <span className="max-w-[38vw] truncate font-medium text-ink-1 sm:max-w-none">{label}</span>
        <span className="sr-only">: show system status</span>
        <ChevronDown className="size-3.5 text-ink-3 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className="absolute right-0 z-40 mt-2 w-80 rounded-xl border border-line bg-white p-2 shadow-lg">
        <ul className="divide-y divide-line">
          {checks.map((c) => (
            <li key={c.name} className="flex items-start gap-2.5 px-2 py-2.5">
              <span className={`mt-1.5 size-2 shrink-0 rounded-full ${DOT[c.health]}`} aria-hidden />
              <div className="min-w-0">
                <p className="text-[13px] text-ink-1">
                  <span className="font-medium">{c.name}</span> · {c.value}
                </p>
                {c.detail && <p className="mt-0.5 text-xs text-ink-3">{c.detail}</p>}
              </div>
            </li>
          ))}
        </ul>
        {data?.system.simulator_enabled && (
          <p className="border-t border-line px-2 pt-2 pb-1 text-xs text-ink-3">Demo controls are on. Simulated traffic is labelled SIM everywhere.</p>
        )}
      </div>
    </details>
  );
}

function aiCheck(ai: Summary["system"]["ai"]): Check {
  const lastError = ai.last_error ? `Last error ${ai.last_error_at ? timeAgo(ai.last_error_at) : ""}: ${ai.last_error}` : undefined;
  if (ai.mode === "live")
    return {
      name: "AI",
      health: ai.reason ? "warn" : "good",
      value: ai.last_model ? `Live · ${ai.last_model}` : "Live",
      detail: [ai.reason, lastError].filter(Boolean).join(" ") || "Gemini explains verdicts and drafts reports. The scorecard decides.",
    };
  if (ai.mode === "cooling_down")
    return {
      name: "AI",
      health: "warn",
      value: `Cooling down${ai.until ? ` until ${hhmm(ai.until)}` : ""}`,
      detail: "The scorecard still decides; explanations use Checkout Guard's own template until then.",
    };
  return {
    name: "AI",
    health: "bad",
    value: "Off · scorecard only",
    detail: [ai.reason, "The scorecard still decides and acts; explanations use the template."].filter(Boolean).join(" "),
  };
}

function workerCheck(worker: Summary["system"]["worker"]): Check {
  const lastPass = worker.last_tick_at ? `Last pass ${timeAgo(worker.last_tick_at)}.` : "No pass has finished yet.";
  if (!worker.alive)
    return {
      name: "Detector",
      health: "bad",
      value: "Stopped",
      detail: `Start the worker: uv run python -m app.worker.main${worker.heartbeat_at ? ` (last seen ${timeAgo(worker.heartbeat_at)})` : ""}`,
    };
  if (worker.state === "failing") return { name: "Detector", health: "bad", value: "Failing", detail: `The last pass failed (${worker.last_tick_error ?? "error"}). ${lastPass}` };
  if (worker.state === "stalled") return { name: "Detector", health: "bad", value: "Stalled", detail: `${lastPass} Nothing is being detected.` };
  if (worker.state === "slow") return { name: "Detector", health: "warn", value: worker.assessing ? "Waiting on AI" : "Slow", detail: lastPass };
  return { name: "Detector", health: "good", value: "Running", detail: lastPass };
}
