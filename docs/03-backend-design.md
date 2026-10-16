# 03 · Python Backend Design

All endpoints are FastAPI routes. The OpenAPI schema is exported at build time, and the frontend generates its TypeScript client from it, so the two sides never drift.

## 1. API contract

### Shop: the demo merchant's server (`app/api/shop.py`)
| Method | Path | Caller | Purpose |
|---|---|---|---|
| GET | `/shop/products` | frontend | 3 SKUs incl. `SKU-TEALIGHT-3` ($3) |
| POST | `/shop/client-token` | frontend | Browser-safe token for JS SDK v6 |
| POST | `/shop/orders` | frontend / simulator | Guard check → create PayPal order (adds `SCA_ALWAYS` when told to) → returns `order_id` or a guard refusal |
| POST | `/shop/orders/{id}/capture` | frontend | Capture after buyer approval or card submit; logs the outcome |
| POST | `/sim/orders` | simulator only (`SIMULATOR_ENABLED`) | Same as above but with a sandbox test card in `payment_source.card`, then auto-capture. Tagged `source=simulator` |

### Guard: the product (`app/api/guard.py`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/guard/check` | Request: `{shop_id, sku, amount, method, fingerprint_hash, email_domain, payer_id_hash?, ip_hash, user_agent_hash}`. Response: `{decision: allow \| allow_with_sca \| challenge \| block, reason_code, incident_id?}`. Shop API key auth. In-process via `GuardClient` for the demo shop |
| POST | `/v1/guard/outcome` | Shops report the order outcome (the demo shop writes it directly) |

### Webhooks (`app/api/webhooks.py`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/webhooks/paypal` | Raw body → verify → `paypal_events` → 200. Processing happens in the worker |

### Dashboard (`app/api/dashboard.py`, cookie auth)
| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard/summary` | Status (quiet / under attack / recovering), today's counts, $ protected |
| GET | `/dashboard/attempts?from&to` | Rows for the AG Grid table and AG Studio data source |
| GET | `/dashboard/incidents`, `/dashboard/incidents/{id}` | Verdict, evidence, actions, timeline |
| GET | `/dashboard/incidents/{id}/report`, `/evidence` | Markdown (+ PDF) |
| POST | `/dashboard/incidents/{id}/actions/{action_id}/approve` | Owner approval above caps |
| POST | `/dashboard/incidents/{id}/undo` | Turn SCA off, unblock, end incident |
| GET | `/dashboard/stream` | SSE: `attempt`, `incident.opened/updated/closed`, `action`, `alert` |
| GET/PUT | `/settings` | Caps, approval threshold, sale windows, autopilot, kill switch |
| POST | `/demo/reset`, `/demo/run/{scenario}` | Demo controls (judge mode) |

### Agent (`app/api/agent.py`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/agent/studio` | Server side of AG Studio's Studio Agent Framework `clientToolRunner`. It takes the conversation and the Studio tool definitions, calls Gemini with those tools plus our own read-only tools (`query_attempts`, `get_incident`), and returns the next round, including Studio tool calls like add widget or execute query. **The exact request/response shape must be checked in the P1 spike against AG Studio docs (AG-UI events).** |

### Ops
`GET /healthz` (DB + PayPal token check) · `GET /version`.

## 2. Data model (SQLAlchemy, Postgres)

| Table | Key columns |
|---|---|
| `shops` | id, name, api_key_hash, paypal_merchant_id |
| `merchant_settings` | shop_id, autopilot (bool), kill_switch, max_auto_refund_per_txn (default 10.00), max_auto_refund_per_incident (100.00), approval_required_above, sale_windows (jsonb), deescalate_after_min (30) |
| `guard_state` | shop_id, mode (`normal` \| `sca_all` \| `sca_all+challenge`), blocked_fingerprints (array), held_skus (array), updated_at, set_by_incident |
| `checkout_attempts` | id, shop_id, ts, source (`web` \| `simulator`), sku, amount, method (`card` \| `wallet`), fingerprint_hash, ip_hash, email_domain, payer_id_hash, card_bin6, card_last4, guard_decision, paypal_order_id, order_status, capture_id, outcome (`captured` \| `declined` \| `error` \| `blocked` \| `sca_failed` \| `pending_action`), decline_code, paypal_debug_id, latency_ms |
| `paypal_events` | id, transmission_id (unique), event_type, resource_id, verified (enum), received_at, processed_at, payload (jsonb) |
| `baselines` | shop_id, hour_of_week, feature, mean, std, sample_days, refreshed_at |
| `incidents` | id, shop_id, opened_at, closed_at, status (`open` \| `contained` \| `closed` \| `false_alarm`), trigger, features (jsonb), verdict (jsonb), classification, confidence, model_id, prompt_version, ai_available (bool) |
| `actions` | id, incident_id, type (`SCA_ON` \| `CHALLENGE_ON` \| `BLOCK_FP` \| `HOLD_SKU` \| `REFUND` \| `NOTIFY` \| `DEESCALATE`), params (jsonb), policy_rule, status (`proposed` \| `executed` \| `awaiting_approval` \| `rejected` \| `failed`), approved_by, paypal_ref, created_at |
| `reports` | incident_id, alert_text, report_md, evidence_md, generated_at, model_id |
| `audit_log` | id, ts, actor (`detector` \| `ai` \| `policy` \| `executor` \| `owner`), incident_id, event, data (jsonb). Append-only |

## 3. PayPal integration (`app/paypal/`)
| Capability | Endpoint | Used for |
|---|---|---|
| OAuth | `POST /v1/oauth2/token` | Cached token, refreshed 60 s before expiry |
| Client token | v6 client token flow | Browser SDK init |
| Orders v2 | `POST /v2/checkout/orders`, `POST /v2/checkout/orders/{id}/capture` | Checkout; per-order `payment_source.card.attributes.verification.method` = `SCA_ALWAYS` when the guard says so (Web) |
| Refunds | `POST /v2/payments/captures/{id}/refund` | Executor refunds attack captures |
| Webhooks | `POST /v1/notifications/verify-webhook-signature` (postback); offline CRC32 + cert check later | Verification (Web) |
| Webhook events subscribed | `CHECKOUT.ORDER.APPROVED`, `CHECKOUT.ORDER.COMPLETED`, `PAYMENT.CAPTURE.COMPLETED`, `.DECLINED`, `.DENIED`, `.PENDING`, `.REFUNDED`, `.REVERSED`, `CUSTOMER.DISPUTE.CREATED/UPDATED/RESOLVED` | Subscribe to both DECLINED and DENIED (Web) |
| Transaction Search | `GET /v1/reporting/transactions` | Hourly baseline refresh. About 3 h latency, so never on the hot path (Web, partly confirmed) |
| Disputes (sandbox) | `POST /v1/customer/disputes`, list/get | Optional "disputes prevented" demo (Web) |

**Rules for all PayPal calls:**
- `PayPal-Request-Id` idempotency key on every POST.
- Store `paypal-debug-id` on every row.
- Retry with backoff on 429/5xx; never retry 4xx.
- Settings refuse to start if `PAYPAL_API_BASE` isn't the sandbox URL (hackathon safety).

**Sandbox test data (Web):**
- `4012888888881881` with name-on-card triggers `CCREJECT-REFUSED`, `-SF`, `-EC`, `-IF`, `-LS`, `-CVV_F` produces declines.
- 3DS cards:
  - `4868719166101368`: challenge, success
  - `4868719181895556`: challenge, fail
  - `4868719196829038`: frictionless, success
  - `4868719158130060`: frictionless, fail
- Buyer accounts: dashboard Bulk Upload (≤ 40 per file).

## 4. Worker (`backend/worker/main.py`)
A single process holds a Postgres advisory lock so only one instance runs.

| Job | Every | Does |
|---|---|---|
| `process_webhooks` | 2 s | Unprocessed `paypal_events` → update attempts/captures/refunds/disputes |
| `detect` | 10 s | Features over 1/5/15/60 min windows vs baseline → triggers → open an incident (or update the open one) → AI analyst → policy → executor → reporter alert |
| `reassess` | 5 min per open incident | Updated features → AI `continue / escalate / de-escalate` → policy |
| `deescalate` | 1 min | No attack signal for `deescalate_after_min` → restore `guard_state`, close incident, write report + evidence |
| `refresh_baseline` | 1 h | Transaction Search + our attempts → `baselines` |

Each job is idempotent and logs to `audit_log`. An SSE fan-out uses Postgres `LISTEN/NOTIFY`, so the API pushes worker events to the dashboard.

## 5. Simulator (`simulator/`)
Run with `cg-sim run <scenario> --target <backend-url> [--speed 10x] [--seed N]`.

| Scenario | Shape |
|---|---|
| `normal_night` | 2–3 orders over hours, mixed SKUs |
| `card_testing` | ~300 attempts in ~10 min (compressed), cheapest SKU, new card or email domain each time, ~70% declines |
| `account_testing` | PayPal-wallet style: many unique payer IDs, same SKU (uses sandbox buyer accounts if automated; otherwise simulated payer IDs, **labelled**) |
| `slow_drip` | 1 attempt/min for 2 h, high decline ratio |
| `flash_sale` | Owner sale window active, 5× volume, varied baskets, low declines, returning fingerprints |
| `paypal_outage` | Injects 5xx/timeouts at the PayPal client layer; declines are processor errors, not card declines |

Every simulated request carries the header `X-CG-Simulation: true`, and each row gets `source=simulator`.

## 6. Testing
- **Unit:** feature math, triggers, policy rules (caps, confidence gate, sale window, kill switch, refund matching), webhook verification parsing.
- **Contract:** the OpenAPI snapshot test; respx-mocked PayPal responses recorded from the P1 spike.
- **Integration:** docker-compose Postgres; scenario replay end to end with Gemini mocked (fixtures) and live (nightly).
- **Smoke (deployed):** `/healthz`, one `card_testing` run at 10× speed, assert the incident is contained in < 5 min of simulated time.
