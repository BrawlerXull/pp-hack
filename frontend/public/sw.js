// Checkout Guard service worker: an offline page for navigations, a cache for Next's hashed static files,
// and push notifications from Firebase Cloud Messaging (data-only messages from backend/app/push.py).
// API calls are never cached: money and attack state must always be live.

const CACHE = "cg-v1";
const OFFLINE = "/offline";
const PRECACHE = [OFFLINE, "/icons/icon-192.png", "/icons/badge-96.png"];
// Dev chunk names are not content hashes, so caching them would serve stale code.
const CACHE_STATIC = !["localhost", "127.0.0.1"].includes(self.location.hostname);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if ((CACHE_STATIC && url.pathname.startsWith("/_next/static/")) || url.pathname.startsWith("/icons/")) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return res;
          }),
      ),
    );
  }
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { data: { title: "Checkout Guard", body: event.data ? event.data.text() : "" } };
  }
  // FCM wraps data-only messages as {data: {...}}; accept a bare object too.
  const d = payload.data || payload.notification || payload;
  const title = d.title || "Checkout Guard";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: d.body || "",
      icon: "/icons/icon-192.png",
      badge: "/icons/badge-96.png",
      tag: d.tag || undefined,
      renotify: Boolean(d.tag),
      data: { url: d.url || "/guard" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/guard", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const open = wins.find((w) => new URL(w.url).origin === self.location.origin);
      if (open) return open.focus().then(() => open.navigate(target));
      return self.clients.openWindow(target);
    }),
  );
});
