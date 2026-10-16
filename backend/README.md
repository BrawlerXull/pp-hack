# Checkout Guard: backend

Python 3.12 · FastAPI · SQLAlchemy (async) + Postgres · Gemini · PayPal REST (sandbox only).

The backend has two processes:

| Process | Command | Does |
|---|---|---|
| API | `uv run uvicorn app.main:app --port 8000` | Shop order API, guard check, PayPal webhooks, dashboard API + live stream, demo controls |
| Worker | `uv run python -m app.worker.main` | Every 5 s: applies verified webhooks, runs the detector, asks the AI, enforces the policy, de-escalates |

## How a checkout is protected

```
browser/bot ─► POST /shop/orders ─► guard check ─► PayPal Orders v2 ─► attempt logged ─┐
                                    (block / challenge / 3-D Secure / allow)            │
PayPal ─► POST /webhooks/paypal ─► verified + stored ─► worker applies ─────────────────┤
                                                                                         ▼
worker: features (1/5/15/60 min) ─► triggers ─► Gemini verdict ─► policy engine ─► executor
        attack / rush / outage      (deterministic)  (AI, with reasons) (fixed rules)   (3DS, block, hold SKU,
                                                                                         capped PayPal refunds)
                                                              ─► reporter: alert, morning report, evidence pack
```

- **AI judges, rules decide.** Gemini classifies the incident and explains it. [app/policy/engine.py](app/policy/engine.py) decides what may happen. The AI never moves money.
- **No invented numbers.**
  - Every number the AI cites as evidence is checked against our own computed features; mismatches are dropped.
  - Reports use `{{placeholders}}` that are filled from the database.
- **Fallbacks.** If Gemini is down or over quota, the next model is tried. If all of them fail, a rule-based "unclear" verdict applies soft defences only (3-D Secure, no refunds or blocks).
- **Privacy.** The AI sees aggregated features plus 20 masked rows: email domain, 8-character device prefix, SKU and outcome. It never sees card numbers, names, emails or credentials.

## Setup

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
| `migrations/` | Alembic |
