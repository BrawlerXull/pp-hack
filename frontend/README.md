# Checkout Guard: frontend

Next.js 16 (App Router) + Tailwind. UI only: every `/api/*` request is proxied to the Python backend (`BACKEND_URL`, default `http://localhost:8000`), so the browser talks to one origin and the dashboard session cookie just works.

```bash
pnpm install
pnpm dev        # http://localhost:3000 (backend must be running on :8000)
pnpm build && pnpm start
```

| Route | What |
|---|---|
| `/` | Product page for judges: the problem, how it works (Watch → Judge → Act → Explain), attack vs flash sale, the PayPal capabilities used, guardrails, and what's real vs simulated |
| `/shop` | Wick & Wax shop. PayPal JS SDK v6 button (`@paypal/react-paypal-js/sdk-v6`). Orders are created and captured by the backend, which runs the guard check first: blocked devices see a message, first-time shoppers on a held item get a human check, and card orders get 3-D Secure during an attack. |
| `/login` | Owner login (password from `ADMIN_PASSWORD`, default `guard-demo`) |
| `/guard` | War Room: status hero with the Watch → Judge → Act → Explain pipeline, KPIs (incl. time to first defence), traffic chart (AG Charts) with a marker where each defence fired, AI verdict with verified evidence, decision trace (rule + PayPal mechanism per action, approvals), phone alert, live attempts table (AG Grid: custom cells, search, outcome filters) |
| `/guard/incidents`, `/guard/incidents/[id]` | Incident history; traffic around the incident, verdict, decision trace, attack signature, morning report, evidence pack for PayPal (copy / download), audit timeline |
| `/guard/settings` | Autopilot, kill switch, refund caps, stand-down time, "I'm running a sale", AI guardrails |
| `/guard/lab` | Labelled simulations (offline or live sandbox), seed history, reset |

Every dashboard page has a simulation bar at the bottom (card-testing attack, flash sale, reset); everything it creates is tagged SIM.

The dashboard uses a dark "night console" theme (tokens in `src/app/globals.css`). The traffic chart's five series colours are validated for colour-blind separation and contrast on the card surface.

Live updates come from the backend's server-sent events (`/api/dashboard/stream`), with SWR polling as a fallback.

| Path | What |
|---|---|
| `src/lib/api.ts` | Typed API client + response types |
| `src/lib/live.ts` | SSE hook that revalidates SWR data |
| `src/components/shop/` | Checkout sheet |
| `src/components/guard/` | Dashboard shell and widgets |
