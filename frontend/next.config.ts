import type { NextConfig } from "next";

// All business logic lives in the Python backend; Next.js only serves the UI and
// proxies /api/* so the browser talks to a single origin (no CORS, cookies just work).
// Read at build time (rewrites are compiled into the build), so Docker/Render pass it as a build arg too.
// Render's fromService gives a bare private hostname: no scheme means http, and no port means the API's
// fixed 8000 (render.yaml sets PORT=8000 on the API).
const RAW_BACKEND_URL = (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/+$/, "");
const BACKEND_URL = /^https?:\/\//.test(RAW_BACKEND_URL)
  ? RAW_BACKEND_URL
  : `http://${RAW_BACKEND_URL}${/:\d+$/.test(RAW_BACKEND_URL) ? "" : ":8000"}`;

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND_URL}/:path*` }];
  },
  // The dev-only Next.js badge sits over the sidebar in demo recordings.
  devIndicators: false,
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
