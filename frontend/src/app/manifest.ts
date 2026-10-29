import type { MetadataRoute } from "next";

// Installable app (Android, desktop Chrome/Edge, iPhone "Add to Home Screen"). It opens on the War Room;
// the service worker (public/sw.js) adds the offline page and push notifications.
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/guard",
    name: "Checkout Guard",
    short_name: "Checkout Guard",
    description: "The night guard for your PayPal checkout: stops card-testing attacks and tells you what happened.",
    start_url: "/guard",
    scope: "/",
    display: "standalone",
    orientation: "any",
    background_color: "#f5f6f8",
    theme_color: "#ffffff",
    categories: ["business", "finance", "security"],
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
    shortcuts: [
      { name: "War Room", url: "/guard", icons: [{ src: "/icons/icon-192.png", sizes: "192x192" }] },
      { name: "Incidents", url: "/guard/incidents", icons: [{ src: "/icons/icon-192.png", sizes: "192x192" }] },
      { name: "Settings", url: "/guard/settings", icons: [{ src: "/icons/icon-192.png", sizes: "192x192" }] },
    ],
  };
}
