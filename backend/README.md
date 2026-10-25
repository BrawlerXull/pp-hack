# Checkout Guard: backend

Python 3.12 · FastAPI · SQLAlchemy (async) + Postgres · Gemini · PayPal REST (sandbox only).

The backend has two processes:

| Process | Command | Does |
|---|---|---|
| API | `uv run uvicorn app.main:app --port 8000` | Shop order API, guard check, PayPal webhooks, dashboard API + live stream, demo controls |
| Worker | `uv run python -m app.worker.main` | Every 5 s (`DETECT_INTERVAL_S`): applies verified webhooks, expires device blocks, re-sends refunds left in flight, runs the detector and the scorecard, asks Gemini to explain (or break a tie), enforces the policy, de-escalates. Beats a heartbeat the dashboard shows as the "Detector" pill |

## How a checkout is protected

```
browser/bot ─► POST /shop/orders ─► guard check ─► PayPal Orders v2 ─► attempt logged ─┐
                                    (block / challenge / 3-D Secure / allow)            │
PayPal ─► POST /webhooks/paypal ─► verified + stored ─► worker applies ─────────────────┤
                                                                                         ▼
worker: features (1/5/10/15/60 min) ─► triggers ─► scorecard ─► policy engine ─► executor
                                  (Poisson, binomial, (attack / rush /  (fixed rules)   (3DS, block, hold SKU,
                                   CUSUM)              outage; Gemini                    capped PayPal refunds)
                                                       breaks ties)
                                  ─► Gemini explains the decision ─► reporter: alert, morning report, evidence pack
```

- **Deterministic code decides, the AI explains.** The scorecard [app/detection/classifier.py](app/detection/classifier.py) classifies the incident; Gemini explains it and breaks ties only when the scorecard is unsure. [app/policy/engine.py](app/policy/engine.py) decides what may happen. The AI never moves money.
- **No invented numbers.**
  - Every number the AI cites as evidence is checked against our own computed features; mismatches are dropped.
  - Reports use `{{placeholders}}` that are filled from the database.
- **Rules first.** A fraud-tier trigger switches 3-D Secure on (policy R0) before any verdict, and a confident scorecard verdict's defences are committed before Gemini is called, so a slow or failing Gemini never leaves a burst undefended.
- **Fallbacks.** If Gemini is down or over quota, the next model is tried; a per-model circuit breaker (shared through Postgres) skips paused models. If none answers, the scorecard still decides and acts (refunds included when it is sure) and the text is our own template. Only an unsure scorecard loses something: no second opinion, so it stays "unclear" and the policy's R3 family decides: 3-D Secure; with a fraud signal, no sale and PayPal working, also blocking only unmistakable bot devices and holding the targeted item (R3b); notify only for a returning-customer rush (R3c) or PayPal errors (R3d). `AI_ENABLED=false` works the same without any call, labelled "AI disabled".
- **Privacy.** The AI sees aggregated features plus 20 masked rows: email domain, 8-character device prefix, SKU and outcome. It never sees card numbers, names, emails or credentials.

## Setup

With Docker, `docker compose up --build` at the repo root runs Postgres, the API (migrations first), the worker and the frontend (see the [top-level README](../README.md#run-it)). By hand:

```bash
cp ../.env.example ../.env        # fill PAYPAL_CLIENT_ID/SECRET and GEMINI_API_KEY
createdb checkout_guard           # local Postgres
uv sync
uv run alembic upgrade head
```

### PayPal sandbox webhooks

PayPal needs a public **https** URL. Locally, use ngrok:

```bash
ngrok http 8000                                              # note the https URL
uv run python scripts/register_webhook.py https://<id>.ngrok-free.dev
```

This creates (or updates) the webhook `https://<host>/webhooks/paypal` on your sandbox app, subscribes it to the events below, and writes `PAYPAL_WEBHOOK_ID` to `.env`. Restart the API afterwards.

To do the same by hand: open Developer Dashboard → Apps & Credentials → **Sandbox** → your app → **Sandbox Webhooks** → **Add Webhook**. Use the URL `https://<host>/webhooks/paypal`, tick these events, and copy the Webhook ID into `PAYPAL_WEBHOOK_ID`:

- `CHECKOUT.ORDER.APPROVED`, `CHECKOUT.ORDER.COMPLETED`
- `PAYMENT.CAPTURE.COMPLETED`, `DECLINED`, `DENIED`, `PENDING`, `REFUNDED`, `REVERSED`
- `CUSTOMER.DISPUTE.CREATED`, `UPDATED`, `RESOLVED`

When deployed, re-run the script with the Render URL.

### Card payments in the sandbox

The sandbox business account must have **Advanced Credit and Debit Card Payments** enabled. Without it, every card order fails with `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`. To enable it: Developer Dashboard → Apps & Credentials → Sandbox → your app → **Features** → tick *Advanced Credit and Debit Card Payments* → Save. If the option is missing, create a new sandbox **business** account (US) under Testing Tools → Sandbox Accounts, and create the app on that account. Wallet (PayPal button) orders work without it.

## Running a demo

```bash
uv run uvicorn app.main:app --port 8000 &
uv run python -m app.worker.main &

curl -c /tmp/cg -X POST localhost:8000/auth/login -H 'content-type: application/json' -d '{"password":"guard-demo"}'
curl -b /tmp/cg -X POST "localhost:8000/demo/seed?days=28"            # 28 days of labelled synthetic history
curl -b /tmp/cg -X POST localhost:8000/demo/run/card_testing -H 'content-type: application/json' \
     -d '{"mode":"offline","count":120,"speed":3}'
curl -b /tmp/cg localhost:8000/dashboard/summary
```

**Scenarios:** `card_testing`, `account_testing`, `slow_drip`, `flash_sale`, `paypal_outage`, `normal_night`.

**Modes:**

| Mode | What happens | Labelling |
|---|---|---|
| `live` | Real PayPal sandbox orders with sandbox test cards; declines come from `CCREJECT-*` name triggers. Needs card processing enabled (see above) | Real sandbox calls |
| `offline` | No PayPal call; the outcome is synthesised | Stored with `paypal_live=false` and shown as simulated |

`account_testing` and `paypal_outage` are offline only.

**Controls:** `POST /demo/reset` clears demo activity and keeps seeded history. Interactive API docs are at http://localhost:8000/docs.

### Attacker terminal

`scripts/attack.py` does the login and the run above, then tails the run: one line per attempt as its outcome lands (time, method, item, amount, device prefix, email domain, outcome, decline code or guard reason, `[simulated]` or `[LIVE sandbox]`), and a `>> GUARD:` line when the defender changes something (status, 3-D Secure, blocked devices, held items, refunds). It polls `/demo/runs`, `/dashboard/attempts` and `/dashboard/summary`; Ctrl-C stops the run on the server. It imports nothing from `app`, so it can point at a deployment. `flash_sale` announces a 2-hour sale window first, like the War Room button (`--no-announce-sale` to skip it).

```bash
uv run python scripts/attack.py card_testing --count 120 --speed 3
uv run python scripts/attack.py card_testing --mode live --count 20
uv run python scripts/attack.py flash_sale --url https://<frontend-host>/api --password <judge password>   # or ADMIN_PASSWORD=...
```

## Configuration

All settings are environment variables (or `.env` at the repo root); [.env.example](../.env.example) lists them. The ones that change behaviour:

| Variable | Default | Effect |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` refuses to start with the demo `ADMIN_PASSWORD`, `SHOP_API_KEY`, or a `SESSION_SECRET` that is a known placeholder or shorter than 32 characters |
| `AI_ENABLED` | `true` | `false`: no Gemini call at all; incidents get the rules fallback labelled "AI disabled", reports use templates, the AI pill says "Rules only · AI disabled" |
| `DETECT_INTERVAL_S` / `REASSESS_INTERVAL_S` | `5` / `120` | Detector pass interval; minimum age of a verdict before a material change re-asks the AI (retries after failures back off 1x, 2x, 4x) |
| `NOTIFY_WEBHOOK_URL` | empty | Also push each owner alert to a Slack incoming webhook or an ntfy topic URL (best effort) |
| `SIMULATOR_ENABLED` | `true` | Mounts `/demo/*` |

## Health

| Endpoint | Answers | Used by |
|---|---|---|
| `GET /livez` | 200 while the process runs and the database answers, else 503 | Render `healthCheckPath`, the compose healthcheck |
| `GET /healthz` | `{"status": "ok" \| "degraded", "db", "paypal", "ai", "stream"}`; **503** when degraded (database or PayPal OAuth failing, every Gemini model paused or the key rejected, or the live stream reconnecting). `ai: "disabled"` is not degraded | The daily GitHub Action (`.github/workflows/healthcheck.yml`), people |

`stream` is the API's Postgres LISTEN connection behind the dashboard's live updates (`app/events.py`). A termination listener and a 30-second ping notice a lost connection; it reconnects with backoff (1 s doubling to 30 s) and sends dashboards a `stream.resumed` event so they refetch what they missed.

## Tests and evaluation

```bash
uv run pytest                      # unit + end-to-end pipeline tests (Postgres test DB: checkout_guard_test)
uv run python scripts/eval_ai.py --runs 2   # live Gemini: attack vs rush vs outage, scored
uv run python scripts/spike.py --count 20 --sca   # raw PayPal sandbox behaviour (docs/sandbox-findings.md)
uv run ruff check .
```

The pipeline tests need a local database: `createdb checkout_guard_test`.

## Layout

| Path | What |
|---|---|
| `app/api/` | FastAPI routers: `shop`, `guard` (public `/v1/guard/check`), `webhooks`, `dashboard`, `settings`, `demo`, `auth` |
| `app/guard/check.py` | The hot-path decision: allow / 3-D Secure / challenge / block |
| `app/shop/service.py` | Demo shop order flow; logs every attempt |
| `app/detection/` | Features, baseline from shop history, triggers, attack signature |
| `app/ai/` | Gemini wrapper, analyst (verdict + evidence check), reporter (alert, report, evidence pack) |
| `app/policy/engine.py` | Fixed safety rules |
| `app/actions/executor.py` | Applies actions; capped refunds via the PayPal Refunds API; owner approvals |
| `app/incidents/` | Incident lifecycle, webhook processor |
| `app/worker/main.py` | Background loop (single instance via a Postgres advisory lock) |
| `app/simulator/` | Labelled attack/control scenarios and seeded history |
| `app/events.py` | Live event bus (Postgres LISTEN/NOTIFY → SSE), with reconnect |
| `app/notify.py` | Optional alert push (`NOTIFY_WEBHOOK_URL`) |
| `scripts/` | `attack.py` (attacker terminal), `register_webhook.py`, `eval_ai.py` (live AI eval), `spike.py` (raw sandbox behaviour) |
| `migrations/` | Alembic |
| `Dockerfile` | One image for the API (default command) and the worker (`python -m app.worker.main`) |
