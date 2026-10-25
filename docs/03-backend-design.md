# 03 · Python Backend Design

All endpoints are FastAPI routes. The OpenAPI schema is exported at build time, and the frontend generates its TypeScript client from it, so the two sides never drift.

## 1. API contract

### Shop: the demo merchant's server (`app/api/shop.py`)
| Method | Path | Caller | Purpose |
|---|---|---|---|
| GET | `/shop/products` | frontend | 3 SKUs incl. `SKU-TEALIGHT-3` ($3) |
| POST | `/shop/client-token` | frontend | Browser-safe token for JS SDK v6 |
| POST | `/shop/orders` | frontend / simulator | Guard check (incl. a per-device velocity breaker: 6+ attempts or 2+ declines in 10 min → challenge, past 12 / 4 even with a challenge token → block until they age out; never keyed on IP; check and attempt row are serialised per device with an advisory lock and the row is committed before the PayPal call, so parallel requests can't outrun it. The challenge itself is a demo click-through, not a bot barrier) → create PayPal order (adds `SCA_ALWAYS` when told to) → returns `order_id` or a guard refusal. `method: "card"` from the browser creates a Card Fields order with **no card data**: only `payment_source.card.attributes.verification.method` (`SCA_ALWAYS` on `allow_with_sca`, else `SCA_WHEN_REQUIRED`) and the return/cancel URLs. The request body has no card fields at all |
| POST | `/shop/orders/{id}/capture` | frontend | Capture after buyer approval (PayPal button) or Card Fields submit; logs the outcome. Browser card orders are read first (`GET /v2/checkout/orders/{id}`) and captured only if the 3-D Secure result allows it (below); otherwise the attempt becomes `sca_failed` and no capture is sent. Returns `{outcome, capture_id, decline_code, decline_category, liability_shift}` |
| POST | `/shop/orders/{id}/card-result` | frontend | Card Fields reported `failed` or `canceled`: read the order and record the bank's answer (`sca_failed` when a card reached PayPal), never capture. Same response shape |
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
| GET | `/dashboard/summary` | Status (quiet / under attack / recovering), today's counts, $ protected; `system` (worker heartbeat alive if < 15 s; detector running / slow after 30 s without a finished pass / stalled after 150 s / failing when the last pass raised, AI live / cooling down until / rules only + reason, PayPal sandbox + card processing seen or unknown, simulator flag, the detector's last 5/15-min numbers with ×N vs normal), `switches` (kill switch, autopilot), running `sale_window` |
| GET | `/dashboard/timeseries?minutes&bucket_s` | Counts by outcome per bucket (`date_bin`, default 60 s; the War Room uses 10 s) |
| GET | `/dashboard/attempts?from&to` | Rows for the AG Grid table and AG Studio data source |
| GET | `/dashboard/incidents`, `/dashboard/incidents/{id}` | Verdict, evidence, actions, timeline; trigger tiers, `dropped_evidence` (AI claims that didn't match our numbers), `ai_fallback_reason`, `evidence_vs_normal` (×N from our stored baseline, never the AI's), `detected_in_s` |
| GET | `/dashboard/incidents/{id}/report`, `/evidence` | Markdown (+ PDF) |
| POST | `/dashboard/incidents/{id}/actions/{action_id}/approve` | Owner approval of a queued refund. 409 `{code: "disputed"}` for a disputed payment (a refund on top of a chargeback pays the buyer twice) unless the body is `{"override_dispute": true}` |
| POST | `/dashboard/incidents/{id}/actions/{action_id}/reject` | Owner "Keep": the payment is never refunded by this or any later incident |
| GET | `/dashboard/alerts?limit=&incident_id=` | Alert history, newest first (audit `alert.sent`: incident, time, text) |
| GET | `/dashboard/devices` | Blocked and owner-trusted devices in force, with reason, incident and expiry |
| POST | `/dashboard/devices/{fingerprint}/unblock` | Release the block and trust the device (left out of the attack signature and refund matching, so enforcement does not re-block it) |
| POST | `/dashboard/incidents/{id}/undo` | Turn SCA off, unblock, end incident |
| GET | `/dashboard/stream` | SSE: `attempt`, `incident.opened/updated/closed`, `action`, `alert` |
| GET/PUT | `/settings` | Caps, approval threshold, sale windows, autopilot, kill switch |
| PATCH | `/settings/switches` | War Room toggles: `{kill_switch?, autopilot?}`, one flag without re-sending the form |
| POST | `/demo/reset`, `/demo/run/{scenario}` | Demo controls (judge mode). Reset also sets kill switch off and autopilot on; a second run while one is active is refused with 409 `{code: "run_active"}`; `announce_sale: true` sets a 2-hour sale window server-side, only once the run is accepted |

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
| `guard_state` | shop_id, mode (`normal` \| `sca_all` \| `sca_all+challenge`), held_skus (array), updated_at, set_by_incident |
| `device_entries` | id, shop_id, fingerprint, kind (`block` \| `trust`), incident_id, reason, created_at, expires_at (blocks 24 h, trust 30 days), released_at. One open entry per device and kind (partial unique index). The worker releases expired entries (audit `block.expired`); the guard check filters by expiry too |
| `checkout_attempts` | id, shop_id, ts, source (`web` \| `simulator`), sku, amount, method (`card` \| `wallet`), fingerprint_hash, ip_hash, email_domain, payer_id_hash, card_bin6, card_last4, card_expiry, card_brand (browser cards: brand, last 4, BIN and expiry come from PayPal's answer, never from the number), sca_liability_shift, sca_enrollment, sca_auth_status (PayPal's `authentication_result`, NULL when 3-D Secure did not run), guard_decision, paypal_order_id, order_status, capture_id, capture_tries, outcome (`created` \| `captured` \| `declined` \| `error` \| `blocked` \| `challenged` \| `sca_required` \| `sca_failed` \| `refunded`), decline_code (canonical name), processor_code (raw `processor_response.response_code`), decline_category (`fraud` \| `customer` \| `probing` \| `processor` \| `unknown_decline`), paypal_debug_id, disputed (true while a refund would pay twice: dispute open, or resolved any way but the seller keeping the money), dispute_status, dispute_id (a new dispute id reopens a resolved one), latency_ms |
| `paypal_events` | id, transmission_id (unique), event_type, resource_id, verified (enum), received_at, processed_at, payload (jsonb) |
| `baselines` | shop_id, hour_of_week, feature, mean, std, sample_days, refreshed_at |
| `incidents` | id, shop_id, opened_at, closed_at, status (`open` \| `contained` \| `closed` \| `false_alarm`), trigger, features (jsonb), verdict (jsonb), classification, confidence, model_id, prompt_version, ai_available (bool) |
| `actions` | id, incident_id, type (`SCA_ON` \| `CHALLENGE_ON` \| `BLOCK_FP` \| `HOLD_SKU` \| `REFUND` \| `NOTIFY` \| `DEESCALATE`), params (jsonb), policy_rule, status (`proposed` \| `executed` \| `awaiting_approval` \| `rejected` \| `failed` \| `in_flight`), approved_by, paypal_ref, created_at, claimed_at. A REFUND's params hold attempt_id, capture_id, amount, request_id, `match` (strong \| weak) and `matched_on`. Partial unique index `uq_actions_refund_attempt` on `params->>'attempt_id'` for REFUND rows in executed / awaiting_approval / rejected / in_flight: one refund decision per payment across all incidents (FAILED may be retried) |
| `reports` | incident_id, alert_text, report_md, evidence_md, generated_at, model_id |
| `audit_log` | id, ts, actor (`detector` \| `ai` \| `policy` \| `executor` \| `owner`), incident_id, event, data (jsonb). Append-only |

## 3. PayPal integration (`app/paypal/`)
| Capability | Endpoint | Used for |
|---|---|---|
| OAuth | `POST /v1/oauth2/token` | Cached token, refreshed 60 s before expiry |
| Client token | v6 client token flow | Browser SDK init |
| Orders v2 | `POST /v2/checkout/orders`, `POST /v2/checkout/orders/{id}/capture`, `GET /v2/checkout/orders/{id}` | Checkout; per-order `payment_source.card.attributes.verification.method` = `SCA_ALWAYS` when the guard says so, `SCA_WHEN_REQUIRED` for other browser card orders. The order `GET` reads a browser card's 3-D Secure result before capture, and resolves unknown or `ORDER_ALREADY_CAPTURED` captures |
| Refunds | `POST /v2/payments/captures/{id}/refund` | Executor refunds attack captures |
| Webhooks | `POST /v1/notifications/verify-webhook-signature` (postback); offline CRC32 + cert check later | Verification (Web) |
| Webhook events subscribed | `CHECKOUT.ORDER.APPROVED`, `CHECKOUT.ORDER.COMPLETED`, `PAYMENT.CAPTURE.COMPLETED`, `.DECLINED`, `.DENIED`, `.PENDING`, `.REFUNDED`, `.REVERSED`, `CUSTOMER.DISPUTE.CREATED/UPDATED/RESOLVED` | Subscribe to both DECLINED and DENIED (Web) |
| Transaction Search | `GET /v1/reporting/transactions` | Hourly baseline refresh. About 3 h latency, so never on the hot path (Web, partly confirmed) |
| Disputes (sandbox) | `POST /v1/customer/disputes`, list/get | Optional "disputes prevented" demo (Web) |

**Rules for all PayPal calls:**
- `PayPal-Request-Id` idempotency key on every POST, derived from our ids so a repeat is the same request: `cg-order-{attempt_id}`, `cg-capture-{order_id}[-{capture_tries}]`, `cg-refund-{attempt_id}` (the refund key is also stored in the action's params). The capture key is reused while the last result is unknown (timeout, 5xx); after a definitive 4xx refusal (`PAYER_ACTION_REQUIRED`, `INSTRUMENT_DECLINED`) `capture_tries` is bumped, so the capture after 3-D Secure or a new funding source is a new request even if PayPal replays cached 4xx responses (not yet confirmed). A capture never downgrades a row already `captured`/`refunded` (racing request, webhook), and on a timeout/5xx or `ORDER_ALREADY_CAPTURED` the order is read back to decide. Refunds lock the attempt row, so two incidents can never both record one refund.
- `capture_web_order` is a no-op once the attempt is captured; `ORDER_ALREADY_CAPTURED` is resolved by `GET /v2/checkout/orders/{id}`.
- OAuth or network failures come back as a status-0 response (stored as `error`), never as an exception that aborts a worker tick.
- Owner approve/reject lock the action row (`SELECT … FOR UPDATE`), so a second click gets 409.
- SCA_ALWAYS orders and browser card orders send `experience_context.return_url/cancel_url` (`PUBLIC_APP_URL`, default `http://localhost:3000`). Card Fields run the bank's window in the page, so the shop does not handle the `?3ds=` return itself.

**Browser card payments and 3-D Secure (`app/paypal/three_ds.py`, `capture_web_order`):** the shop's **Pay by card** tab renders PayPal-hosted Card Fields (JS SDK v6, loaded with a client token). On Pay, the browser asks `/shop/orders` for a card order (guard check as usual, no card data), calls `submit(orderId)` (PayPal shows the bank's 3-D Secure window when needed), then calls the capture endpoint for a `succeeded` result and the record-only `card-result` endpoint for `failed` or `canceled` (so a closed bank window is logged as `sca_failed`, never charged). The Pay button stays disabled until PayPal's `validitychange` event says the number, expiry and CVV are valid, so a typo creates no order. The browser's word is not proof, so the server reads every browser order first and decides from `payment_source.card.authentication_result`:

| Liability shift | Guard required 3-D Secure (`SCA_ALWAYS`) | Normal mode (`SCA_WHEN_REQUIRED`) |
|---|---|---|
| `YES`, `POSSIBLE` | capture | capture |
| no result (3-D Secure did not run) | `sca_failed` | capture (the bank decides) |
| `NO` with enrollment `N`, `U` or `B` (not enrolled, issuer unavailable, bypassed), or a bare `NO` with no `three_d_secure` details | `sca_failed` | capture (PayPal's recommended action; the bank decides) |
| `NO` with enrollment `Y` (challenged, not passed), `UNKNOWN` | `sca_failed` | `sca_failed` |
| order still `PAYER_ACTION_REQUIRED` | `sca_failed` | `sca_failed` |

A card order is captured only once PayPal reports it `APPROVED`; a `CREATED` order with no card on it is left alone (nothing submitted yet), so PayPal is never asked to capture an unapproved order. A card submitted to an order that was created as a **wallet** order (no card guard decision, no 3-D Secure attribute) is refused as `sca_failed` and the row's method becomes `card`.

`sca_failed` sends no capture. A refusal is final: a repeat call on a `sca_failed` attempt returns it without calling PayPal, a captured attempt returns the stored capture, and an order already captured elsewhere is read, not captured again. If a card order cannot be read (timeout, 5xx), nothing is captured in either mode and the row is left for a retry. Brand, last 4, BIN (when PayPal sends `bin_details`), expiry and the authentication result are stored on the attempt from PayPal's answer, so detection's card features see real browser cards. In the shop, the buyer sees plain sentences, never raw codes ("Your bank could not verify this card…", "Your card was declined for insufficient funds…").

**Decline classification (`backend/app/paypal/codes.py`):** one table maps processor codes (`9500`) and issue names (`INSTRUMENT_DECLINED`) to a canonical name, a category and an outcome. Codes in the table are `declined` (or `sca_required` for `PAYER_ACTION_REQUIRED`); an unknown 422 issue on a card payment is `declined` / `unknown_decline` (on a wallet it is `error`); 0/5xx, unknown non-422 4xx and listed integration/concurrency problems (e.g. `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`, `AMOUNT_MISMATCH`, `PREVIOUS_REQUEST_IN_PROGRESS`) are `error`. A 201 capture with `status: DECLINED` is a decline even inside a `COMPLETED` order. Webhooks read `processor_response` for DECLINED/DENIED, and link events by capture id, order id or our `custom_id` (`{shop_id}:{attempt_id}`); unmatched events are retried every 15 s for 2 minutes and skipped in between (`retry_after`), so events that never match cannot starve new ones. Offline simulation derives its decline code from the `CCREJECT-*` card name through the same table.

**Card key:** unique-card counts use `bin6 + last4 + expiry` (for a browser card without a BIN from PayPal, `brand* + last4 + expiry`: two cards of one brand sharing the last 4 then count as one, which can only under-count). Expiry is included on purpose: card testers try many expiry dates against one number, and each guess is a separate credential to the issuer. Live-mode scenarios rotate through PayPal's published sandbox Visa test numbers with a random expiry (returning shoppers keep one card), so a live attack shows a high unique-card ratio like a real one.
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
| `detect` | 10 s | Features over 1/5/10/15/60 min windows vs baseline (history excludes incident windows, guard-stopped attempts and blocked devices) → triggers → open an incident (or update the open one) → scorecard classifier (Gemini tie-break when unsure) → policy → executor → Gemini explanation → reporter alert |
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
