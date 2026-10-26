# Checkout Guard: frontend

Next.js 16 (App Router) + Tailwind. UI only: every `/api/*` request is proxied to the Python backend (`BACKEND_URL`, default `http://localhost:8000`), so the browser talks to one origin and the dashboard session cookie just works.

```bash
pnpm install
pnpm dev        # http://localhost:3000 (backend must be running on :8000)
pnpm build && pnpm start
```

`NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY` (optional, in `frontend/.env.local` for `pnpm dev`, a build arg for Docker/Render) removes AG Studio's trial watermark on the Investigate page.

`BACKEND_URL` is read when `next.config.ts` builds the rewrites, so a production build must know it at build time: the [Dockerfile](Dockerfile) takes it as a build arg (`docker compose` passes `http://api:8000`; Render passes the API's private hostname, and a value without a scheme gets `http://` and, without a port, `:8000`). The image build fails if it is empty.

| Route | What |
|---|---|
| `/` | Product page for judges: the problem, how it works (Watch → Judge → Act → Explain), attack vs flash sale, the PayPal capabilities used, guardrails, and what's real vs simulated |
| `/shop` | Wick & Wax shop. PayPal JS SDK v6 button (`@paypal/react-paypal-js/sdk-v6`). Orders are created and captured by the backend, which runs the guard check first: blocked devices see a message, and first-time shoppers on a held item get a human check. Without `PAYPAL_CLIENT_ID` on the backend, checkout says "PayPal not configured: use the Demo lab (offline)" instead of loading forever. |
| `/login` | Owner login (password from `ADMIN_PASSWORD`, default `guard-demo`) |
| `/guard` | War Room: system pills (AI, detector, PayPal, simulator, live updates), status hero with the Watch → Judge → Act → Explain pipeline and time to first defence, KPIs vs normal, activity chart in 10-second bars with the incident shaded and a marker where it was detected and where each defence fired (AG Charts), AI verdict with verified evidence (×N vs normal), decision trace (policy rule and PayPal mechanism per action, approvals), the phone's alert history, guard switches, devices (blocked/trusted), live attempts table (AG Grid: custom cells, search, outcome filters, a Why column) |
| `/guard/incidents`, `/guard/incidents/[id]` | Incident history; detect → judge → decide strip, traffic around the incident, verdict, decision trace, attack signature, morning report, evidence pack for PayPal (copy / download), audit timeline |
| `/guard/investigate` | AG Studio over the last 48 hours (`GET /api/studio/data`): pages for the live attack, incidents and attack vs flash sale; our own **AI verdict** and **Defence timeline** widgets; the night theme; the **Guard analyst** (Studio Agent Framework) with read-only Checkout Guard tools, delegating to Studio's built-in agents, every turn on Gemini via `POST /api/studio/agent/turn`; auto-investigation of new incidents. Files in `src/components/studio/` |
| `/guard/settings` | Autopilot, kill switch, refund caps, stand-down time, "I'm running a sale", AI guardrails |
| `/guard/lab` | Labelled simulations (offline or live sandbox), seed history, reset |

Every dashboard page has a simulation bar at the bottom (card-testing attack, flash sale, reset); everything it creates is tagged SIM.

The dashboard uses a dark "night console" theme (tokens in `src/app/globals.css`). The activity chart's five series colours are validated for colour-blind separation and contrast on the card surface.

Live updates come from the backend's server-sent events (`/api/dashboard/stream`), with SWR polling as a fallback.

| Path | What |
|---|---|
| `src/lib/api.ts` | Typed API client + response types |
| `src/lib/live.ts` | SSE hook that revalidates SWR data |
| `src/components/shop/` | Checkout sheet |
| `src/components/guard/` | Dashboard shell and widgets |
