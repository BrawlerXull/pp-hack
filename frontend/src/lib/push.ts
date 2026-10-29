"use client";

// Push notifications through Firebase Cloud Messaging. The browser gets an FCM token for our own service
// worker (public/sw.js draws the notifications), and the backend stores it for the signed-in member.

import { api, type PushConfig } from "./api";
import { isIOS, isStandalone, registerServiceWorker } from "./pwa";

const TOKEN_KEY = "cg_push_token";

export type PushSupport = "supported" | "ios-install-first" | "unsupported";

export function pushSupport(): PushSupport {
  if (typeof window === "undefined") return "unsupported";
  // iPhone and iPad only allow web push for apps added to the Home Screen (iOS 16.4+).
  if (isIOS() && !isStandalone()) return "ios-install-first";
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window ? "supported" : "unsupported";
}

export function storedToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function deviceLabel(): string {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const os = /Android/.test(ua) ? "Android" : isIOS() ? "iPhone or iPad" : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "device";
  return `${browser} on ${os}${isStandalone() ? " (app)" : ""}`;
}

async function messaging(cfg: PushConfig) {
  const { getApps, initializeApp } = await import("firebase/app");
  const { getMessaging, isSupported } = await import("firebase/messaging");
  if (!(await isSupported())) throw new Error("This browser doesn't support push notifications.");
  const app = getApps()[0] ?? initializeApp(cfg.firebase!);
  return getMessaging(app);
}

/** Ask for permission, get this browser's FCM token and register it for the signed-in member. */
export async function enablePush(cfg: PushConfig): Promise<void> {
  if (!cfg.enabled || !cfg.firebase || !cfg.vapid_key) throw new Error("Push isn't set up on the server yet.");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "Notifications are blocked for this site. Allow them in your browser's site settings, then try again."
        : "Notifications weren't allowed.",
    );
  }
  const registration = await registerServiceWorker();
  if (!registration) throw new Error("This browser can't run the service worker that shows notifications.");
  await navigator.serviceWorker.ready;
  const { getToken } = await import("firebase/messaging");
  const token = await getToken(await messaging(cfg), { vapidKey: cfg.vapid_key, serviceWorkerRegistration: registration });
  if (!token) throw new Error("Couldn't get a push token from Firebase.");
  await api("/push/devices", { method: "POST", json: { token, label: deviceLabel() } });
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // private mode: the device still gets pushes; this page just can't remember it is on
  }
}

export async function disablePush(cfg: PushConfig): Promise<void> {
  const token = storedToken();
  if (token) {
    await api("/push/devices/remove", { method: "POST", json: { token, label: "" } }).catch(() => undefined);
    try {
      const { deleteToken } = await import("firebase/messaging");
      if (cfg.firebase) await deleteToken(await messaging(cfg));
    } catch {
      // the server copy is gone, which is what stops the pushes
    }
  }
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}
