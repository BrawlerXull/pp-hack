"use client";

import { useEffect, useState } from "react";

const currentTime = () => Date.now();

/** The current time, refreshed every `intervalMs`, so render stays pure and live durations keep ticking. */
export function useNow(intervalMs = 15_000): number {
  const [now, setNow] = useState(currentTime);
  useEffect(() => {
    const id = window.setInterval(() => setNow(currentTime()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
