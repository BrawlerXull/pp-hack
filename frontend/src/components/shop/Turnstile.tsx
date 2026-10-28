"use client";

import { useEffect, useRef } from "react";

// Cloudflare Turnstile, rendered explicitly so the widget only loads when the guard asks for a human check.
// The server verifies the token (POST /shop/challenge → Cloudflare siteverify); the page never decides.
type TurnstileApi = {
  render(el: HTMLElement, opts: { sitekey: string; theme?: "light" | "dark" | "auto"; callback: (token: string) => void; "error-callback"?: () => void }): string;
  remove(id: string): void;
};

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
let loading: Promise<void> | null = null;

function load(): Promise<void> {
  if (window.turnstile) return Promise.resolve();
  loading ??= new Promise<void>((resolve, reject) => {
    const s = document.createElement("script");
    s.src = SRC;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => {
      loading = null;
      reject(new Error("Turnstile did not load"));
    };
    document.head.appendChild(s);
  });
  return loading;
}

export function Turnstile({ siteKey, onToken, onError }: { siteKey: string; onToken: (token: string) => void; onError: () => void }) {
  const el = useRef<HTMLDivElement>(null);
  const handlers = useRef({ onToken, onError });
  useEffect(() => {
    handlers.current = { onToken, onError };
  });

  useEffect(() => {
    let id: string | undefined;
    let cancelled = false;
    load()
      .then(() => {
        if (cancelled || !el.current || !window.turnstile) return;
        id = window.turnstile.render(el.current, {
          sitekey: siteKey,
          theme: "light",
          callback: (token) => handlers.current.onToken(token),
          "error-callback": () => handlers.current.onError(),
        });
      })
      .catch(() => handlers.current.onError());
    return () => {
      cancelled = true;
      if (id && window.turnstile) window.turnstile.remove(id);
    };
  }, [siteKey]);

  return <div ref={el} className="min-h-[65px]" />;
}
