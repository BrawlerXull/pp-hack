"use client";

import { useState } from "react";
import { useSWRConfig } from "swr";
import { api, type Summary } from "@/lib/api";

type Key = "kill_switch" | "autopilot";

// Turning these on relaxes protection, so it asks first; switching protection back on is one click.
const RELAXES: Record<Key, boolean> = { kill_switch: true, autopilot: true };

const COPY: Record<Key, { label: string; on: string; off: string }> = {
  kill_switch: {
    label: "Kill switch",
    on: "Guard paused: checkouts go straight to PayPal, the guard only notifies you.",
    off: "Off: the guard checks every checkout.",
  },
  autopilot: {
    label: "Autopilot refunds",
    on: "Confident attack verdicts refund matched test payments on their own, within your limits.",
    off: "Every refund waits for your approval.",
  },
};

/** The two switches an owner reaches for mid-incident. PATCH /settings/switches changes one flag only. */
export function GuardSwitches({ switches }: { switches: Summary["switches"] }) {
  const { mutate } = useSWRConfig();
  const [busy, setBusy] = useState<Key | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function flip(key: Key) {
    const next = !switches[key];
    if (next && RELAXES[key] && !window.confirm(`Turn on ${COPY[key].label.toLowerCase()}?\n\n${COPY[key].on}`)) return;
    setBusy(key);
    setError(null);
    try {
      await api("/settings/switches", { method: "PATCH", json: { [key]: next } });
      await mutate("/dashboard/summary");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change it");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
      {(Object.keys(COPY) as Key[]).map((key) => {
        const on = switches[key];
        const danger = key === "kill_switch" && on;
        return (
          <div key={key} className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className={`text-sm font-medium ${danger ? "text-red-300" : "text-ink-1"}`}>{COPY[key].label}</p>
              <p className="text-xs text-ink-3">{on ? COPY[key].on : COPY[key].off}</p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={on}
              aria-label={COPY[key].label}
              disabled={busy !== null}
              onClick={() => flip(key)}
              className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
                on ? (key === "kill_switch" ? "bg-red-500" : "bg-guard") : "bg-white/15"
              }`}
            >
              <span className={`inline-block size-5 rounded-full bg-white shadow transition ${on ? "translate-x-5" : "translate-x-0.5"}`} />
            </button>
          </div>
        );
      })}
      {error && <p className="text-xs text-red-300">{error}</p>}
    </div>
  );
}
