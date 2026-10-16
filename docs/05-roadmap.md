# 05 · Roadmap

**Timeline:**
- **Today:** Oct 11, 2026
- **Internal freeze:** Nov 9
- **Submit:** Nov 10 (hard deadline Nov 12, 12:00 PM PT)
- **Keep the demo alive until:** Dec 15

**[A] Team:** 3–4 people, part-time (about 3–4 h/day). Adjust once the team is confirmed. Solo builder: drop *(opt)* items and add about 30% to the estimates.

| Role | Owns |
|---|---|
| **A: Backend / PayPal (Python)** | FastAPI app, PayPal client, shop + guard + webhooks, DB, worker, Render |
| **B: AI / detection (Python)** | Features, triggers, analyst, policy engine, reporter, studio agent backend, eval harness |
| **C: Frontend (Next.js)** | Shop UI, JS SDK v6 checkout, dashboard, AG Studio War Room + custom widgets, agent panel |
| **D: Product / demo / QA** | Eligibility, Discord questions, scenarios, README, video, deck, Devpost, checklist |

## Phases

### P0 · Scope & eligibility (Oct 11–12) · Owner D · Must
- Every member checks eligibility (age, country, conflicts) and registers on Devpost. Create the project and send invites.
- Ask on PayPal Discord:
  1. AG Studio license through Dec 15 / what an unlicensed build looks like
  2. boilerplate reuse terms
  3. whether sandbox declines send webhooks
- Claim the Render $50 credits. **Hold off activating the AG trial** until about Oct 20, unless an extension is confirmed.
- **Done when:** everyone is registered and the questions are posted.

### P1 · Sandbox spike (Oct 12–15) · A + B (+C for AG) · **Must** (Idea › Risks: "test that before building anything else")
- Sandbox business account, REST app, buyer accounts (bulk upload), webhook URL via ngrok subscribed to all events.
- **Python spike script** (`backend/scripts/spike.py`, httpx). This replaces the existing `scripts/spike.ts`:
  - ~100 card orders with `CCREJECT-*` triggers, then capture
  - refunds
  - the 3DS card matrix with `SCA_ALWAYS` vs `SCA_WHEN_REQUIRED`
  - logs to JSONL
- Minimal FastAPI `/webhooks/paypal` logger with postback verification. This replaces the Next.js route.
- Check the Python `paypal-server-sdk` covers Orders, Payments and client token in v6.
- Check Transaction Search latency and sandbox dispute creation.
- **AG Studio check (C+B):** get a `clientToolRunner` round-trip working against a stub Python endpoint, and write down the message format.
- **Deliverable:** a filled-in [sandbox-findings.md](sandbox-findings.md) plus recorded fixtures.
- **Done when:** a scripted attack produces captures, declines and refunds we can see, and the webhook behaviour is known. **Go/no-go on Oct 15.**
- **Fallbacks:**
  - no decline webhooks → our own attempt log is the main signal (already planned)
  - 3DS challenge UI flaky → use frictionless-fail cards
  - Python SDK gaps → httpx for those calls

### P2 · Core backend + frontend skeleton (Oct 15–22) · A, C · Must
- Restructure the repo into `frontend/` and `backend/`, plus `docker-compose.yml`. Add Alembic migrations for all tables (doc 03 §2).
- **Backend:**
  - config, PayPal client (token cache)
  - shop routes, guard check + state cache
  - webhooks (verify, store, dedupe)
  - attempt logging, settings, auth
  - `/healthz`
  - the OpenAPI export
- **Frontend:** move the scaffold; Next rewrites to the backend; shop pages with JS SDK v6 PayPal button + Card Fields; generated API client; dashboard shell.
- **Baseline seeder:** a nightly job that places realistic "normal" sandbox orders, so real history builds up from Oct 16.
- **Done when:** a browser checkout works end to end through the Python backend, every attempt is logged, and the guard can force `SCA_ALWAYS`.

### P3 · Detection, AI, policy, actions (Oct 18–28) · B (+A) · Must
- Features, triggers, analyst (structured output), policy engine with unit tests, executor (SCA, block, hold SKU, refunds), reporter (alert, report, evidence).
- Worker jobs: process_webhooks, detect, reassess, deescalate, refresh_baseline.
- Simulator CLI with all 6 scenarios; eval harness (replay mode).
- **Done when:** in replay mode, card_testing is contained and flash_sale gets no defences; 0 false blocks; time-to-detect < 5 min; the alert, report and evidence pack are generated.
- **Fallback:** if the LLM misclassifies, tune the prompt against fixtures, with the rule fallback always in place.

### P4 · Dashboard + sponsor integrations (Oct 22–Nov 2) · C (AG), A (Render), B (agent) · High
- AG Studio War Room:
  - PayPal-like theme and layout
  - custom **Attack Timeline** and **Defence Status** widgets
  - AG Grid attempts table
  - live updates over SSE
- "Ask the Guard": the Studio Agent Framework connected to `/agent/studio` (Gemini + read-only tools).
- Incident page, report viewer, settings, approve/undo buttons, demo controls (run scenario, reset).
- `render.yaml`: backend + worker + frontend + paid Postgres; deploy.
- *(opt)* PayPal Agent Toolkit or MCP for read-only lookups. Check Python support first; it's TypeScript-first (Web).
- **Done when:** the hosted URL shows a live incident end to end, and the agent builds a widget on request.
- **Fallback:** if the Studio agent integration gets stuck, narrow it to 2–3 tools. If Studio is unavailable, use AG Grid + AG Charts (a weaker prize case).

### P5 · Integration & hardening (Nov 2–6) · All · Must
- End-to-end on Render; live eval nightly; Playwright smoke test on the frontend; pytest in CI (GitHub Actions).
- Load: 300 attempts in about 10 min against sandbox rate limits, throttling if needed.
- Fresh-clone test following only the README; gitleaks scan.
- **Done when:** 3 clean demo runs in a row from `/demo/reset`.

### P6 · Polish & demo (Nov 5–9) · D (+C) · Must
- UI polish, empty and loading states, phone-style alert card, report PDF.
- Record the video (≤ 2:50), build the deck, capture screenshots, write the Devpost text.
- Record a labelled "replay" backup.
- **Done when:** the video is public on YouTube and two people have reviewed it.

### P7 · Submit (Nov 9–10, buffer to Nov 12) · Representative · Must
- Run the compliance checklist (doc 06), then submit.
- **Done when:** Devpost shows "Submitted".

### Keep-alive (Nov 12–Dec 15) · A · Must
- Uptime monitor; weekly scenario run; check AG license and Render credits on Dec 1 and Dec 8; freeze the deploy branch.

## Dependency summary
P0 → P1 (go/no-go) → P2 → P3 → P5 → P6 → P7. P4 starts once P2's API and P3's data exist, and runs alongside P3.

## Time sinks to avoid
- WooCommerce plugin
- Playwright-driven wallet bot
- Render Workflows
- custom ML model
- multi-merchant onboarding
- extra sponsor tools (Channel3, Bryntum, Elastic, Zapier)

Only *(opt)* items after P5 is green.
