# Checkout Guard: frontend

Next.js 16 (App Router) + Tailwind. UI only: every `/api/*` request is proxied to the Python backend (`BACKEND_URL`, default `http://localhost:8000`), so the browser talks to one origin and the dashboard session cookie just works.

```bash
pnpm install
pnpm dev        # http://localhost:3000 (backend must be running on :8000)
pnpm build && pnpm start
```

| Route | What |
|---|---|
| `/` | Wick & Wax shop. PayPal JS SDK v6 button (`@paypal/react-paypal-js/sdk-v6`). Orders are created and captured by the backend, which runs the guard check first: blocked devices see a message, first-time shoppers on a held item get a human check, and card orders get 3-D Secure during an attack. |
| `/login` | Owner login (password from `ADMIN_PASSWORD`, default `guard-demo`) |
| `/guard` | War Room: status, KPIs, live activity chart (AG Charts), AI verdict with verified evidence, actions and approvals, phone alert, live attempts table (AG Grid), demo buttons |
| `/guard/incidents`, `/guard/incidents/[id]` | Incident history; morning report, evidence pack for PayPal (copy / download), audit timeline |
| `/guard/settings` | Autopilot, kill switch, refund caps, stand-down time, "I'm running a sale" |
| `/guard/lab` | Labelled simulations (offline or live sandbox), seed history, reset |

Live updates come from the backend's server-sent events (`/api/dashboard/stream`), with SWR polling as a fallback.

| Path | What |
|---|---|
| `src/lib/api.ts` | Typed API client + response types |
| `src/lib/live.ts` | SSE hook that revalidates SWR data |
| `src/components/shop/` | Checkout sheet |
| `src/components/guard/` | Dashboard shell and widgets |
