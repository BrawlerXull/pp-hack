"use client";

import { useEffect } from "react";
import { registerServiceWorker } from "@/lib/pwa";

/** Registers the service worker on every page, so the app is installable and can receive pushes. */
export function PwaRegister() {
  useEffect(() => {
    registerServiceWorker();
  }, []);
  return null;
}
