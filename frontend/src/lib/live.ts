"use client";

import { useEffect, useRef, useState } from "react";
import { useSWRConfig } from "swr";
import type { LiveEvent } from "./api";

/**
 * Subscribes to the backend's server-sent events and revalidates SWR data when something
 * happens (new attempt, incident update, action, alert). Bursts are coalesced so a 300-attempt
 * attack doesn't trigger 300 refetches.
 */
export function useLiveUpdates(onEvent?: (e: LiveEvent) => void): "connecting" | "live" | "offline" {
  const { mutate } = useSWRConfig();
  const [state, setState] = useState<"connecting" | "live" | "offline">("connecting");
  const pending = useRef<number | null>(null);
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    const es = new EventSource("/api/dashboard/stream", { withCredentials: true });
    const flush = () => {
      pending.current = null;
      mutate((key) => typeof key === "string" && key.startsWith("/dashboard"));
      mutate("/demo/runs");
    };
    es.addEventListener("hello", () => setState("live"));
    es.addEventListener("message", (msg) => {
      try {
        const evt = JSON.parse((msg as MessageEvent).data) as LiveEvent;
        onEventRef.current?.(evt);
      } catch {
        /* ignore malformed */
      }
      if (pending.current === null) pending.current = window.setTimeout(flush, 600);
    });
    es.onerror = () => setState("offline");
    es.onopen = () => setState("live");
    return () => {
      es.close();
      if (pending.current) window.clearTimeout(pending.current);
    };
  }, [mutate]);

  return state;
}
