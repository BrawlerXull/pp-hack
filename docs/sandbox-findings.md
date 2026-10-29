# Sandbox findings (P1 spike)

Sources: `backend/scripts/spike.py`, the FastAPI webhook receiver, and end-to-end runs on Oct 16, 2026.

| Question | Result | Evidence |
|---|---|---|
| OAuth with the sandbox REST app | ✅ Works | `/healthz` → `paypal: ok` |
| Card order with `payment_source.card` | ✅ Works on a **US** sandbox business account (Oct 25). The original India-region account cannot enable card processing (`422 PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING`, no option in the dashboard); a US business account + new app fixed it. A card order returns `201 COMPLETED` (captured in one call) | live run seed 284339 |
| Wallet order (PayPal button) | ✅ `201 CREATED` (order `2EC25120XX084312G`) | `POST /shop/orders` method=wallet |
| JS SDK v6 client token (`response_type=client_token`) | ✅ Returns a token | `POST /shop/client-token` |
| Webhook registration via API | ✅ `POST /v1/notifications/webhooks` created `1A9810902M7052934` | `scripts/register_webhook.py` |
| Webhook delivery + postback verification | ✅ Simulated `PAYMENT.CAPTURE.COMPLETED` arrived through ngrok and verified `SUCCESS` (the docs said mock events can't be verified; in practice they were) | `paypal_events` table |
| Do `CCREJECT-*` triggers decline at create or capture? Which codes? | ✅ At create: `201` with the capture `DECLINED` and a real processor code: `DO_NOT_HONOR`, `SUSPECTED_FRAUD`, `CVV2_FAILURE_POSSIBLE_RETRY_WITH_CVV` seen | `checkout_attempts` (paypal_live) |
| Does the sandbox accept every published test card number used by live scenarios (`SANDBOX_TEST_CARDS`)? | ⏳ Blocked until card processing is enabled | — |
| Does PayPal replay a cached 4xx for a reused `PayPal-Request-Id`? | ⏳ Not tested yet | Mitigated for captures (new key after a definitive 4xx); a refund retried after a 4xx still reuses its key |
| Which webhooks fire for a declined card? | ✅ `PAYMENT.CAPTURE.DECLINED` (5 of 5), verified `SUCCESS` and applied | `paypal_events` |
| Does `SCA_ALWAYS` return `PAYER_ACTION_REQUIRED` for the 3DS test cards? | ✅ Once the guard switched to 3-D Secure mode, live bot card orders came back `PAYER_ACTION_REQUIRED` (5 of 5) and were never captured. The dedicated 3DS test-card matrix (`spike.py --sca`) is still to run | live run seed 284339 |
| Refunds on real sandbox captures | ✅ The guard refunded 2 real bot captures through `POST /v2/payments/captures/{id}/refund` (refund ids `1TX50898JA3418254`, `32V19960J5064801W`); `PAYMENT.CAPTURE.REFUNDED` webhooks arrived and were applied. A third bot capture on another item ($18) was queued for owner approval (above the $10 cap) after fixing item-only matching | live run seed 284339 |
| Browser Card Fields (JS SDK v6) + 3-D Secure | ✅ Verified in a real browser (Oct 25, US sandbox account). Normal mode: card `…1368` captured with no challenge. 3-D Secure mode (guard `allow_with_sca`): PayPal showed the bank's OTP challenge; challenge-pass card `4868719166101368` → order stays **CREATED** (not APPROVED) with `liability_shift POSSIBLE`, `enrollment Y`, `authentication Y` → capture succeeded (`1GD493293X309771M`). Challenge-fail card `4868719181895556` → `liability_shift NO`, `authentication N` → recorded `sca_failed`, never captured, shopper told they were not charged. Fixed: the capture gate required APPROVED and wrongly refused the passing card | `checkout_attempts` (source web) |
| Payment holds: authorise, then capture or void (Oct 29) | ✅ A 3-D Secure card payment in the browser was authorised (`2TH605043T5561100`), then captured by the worker when its incident closed (`5WF074077E341583N`). A second hold (`91S639918U3723457`) was voided: `204`, authorization status `VOIDED` | `checkout_attempts`, PayPal GET authorization |
| Observer mode with real webhooks (Oct 29, through ngrok) | ✅ An order created outside Checkout Guard (no `custom_id`, like a hosted button) sent `PAYMENT.CAPTURE.COMPLETED`, verified `SUCCESS`, and became one observed payment. PayPal did **not** send `CHECKOUT.ORDER.COMPLETED` for a card captured in the same call, although it is subscribed, so the worker reads the order (`GET /v2/checkout/orders/{id}`) for the item and card: filled in live (`VISA 1881`, item `HOSTED-BUTTON-TEALIGHT`). A `CCREJECT-SD` card on a foreign order sent `PAYMENT.CAPTURE.DECLINED` (processor `5100`), verified and recorded as an observed decline. Card orders carry no payer, so no email domain; the sandbox returned `bin_details` empty | `paypal_events`, `checkout_attempts` (source observed) |
| Real sandbox dispute, end to end (Oct 29) | ✅ A sandbox **personal** buyer paid $18 with the PayPal button (order `10S40591GT359220P`); as a first-time shopper the payment was held and captured by the worker after the 10-minute review window (`3FH935305A408593C` → `9EM29027R4490681H`, reason `review_passed`). The buyer opened "item not received" in the Resolution Center: `PP-R-CPM-10195172`. The Disputes API listed it within a minute; `CUSTOMER.DISPUTE.CREATED` arrived about **three minutes** later, verified `SUCCESS` (dispute sync via the API added as a backup). The assistant opened a case: rule D4 (your call), Gemini read the buyer correctly, note drafted with our capture id and date. Contest: PayPal refused evidence type `OTHER` (`EVIDENCE_TYPE_IS_NOT_ALLOWED`) and accepted `PROOF_OF_FULFILLMENT` (multipart `provide-evidence`, `200`); the dispute moved to `WAITING_FOR_BUYER_RESPONSE`, a second answer was refused (`ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE`), and the case now follows PayPal's status (contested) | `paypal_events`, `dispute_cases`, Disputes API |
| Disputes API access (Oct 29) | ✅ `GET /v1/customer/disputes` → `200` (no disputes on the account yet); `accept-claim` on an unknown id → `404 RESOURCE_NOT_FOUND`, so the app is allowed to use it. Opening a real sandbox dispute needs a buyer (personal) account's own PayPal payment: `POST /v1/customer/disputes` asks for a `buyer_transaction_id` | API probe |
| Sandbox rate limits at ~1 request/s | No 429s seen across 28 calls | spike log |
| Transaction Search latency | Not tested yet | — |

**Card processing:** enabled by moving to a US sandbox business account and app (Oct 25). Still open: the 3DS test-card matrix (`uv run python scripts/spike.py --count 30 --sca`), test-card acceptance for every number in `SANDBOX_TEST_CARDS`, Transaction Search latency, and whether PayPal replays a cached 4xx for a reused request id.

**Decision:** go. The full pipeline works against the real sandbox (orders, client token, webhooks) and in labelled offline simulation. The `live` attack mode switches on as soon as card processing is enabled.

## AI evaluation (`scripts/eval_ai.py`, live Gemini)

10/10 scenarios classified correctly (2 runs each). This run predates the decline-code table (Oct 18): the eval now derives outcomes and codes through the same path as the shop, so account_testing and flash_sale declines are no longer counted as errors. Re-run pending. Latency was 3–10 s per verdict with the reduced thinking settings.

| Scenario | Verdict | Confidence |
|---|---|---|
| card_testing | card_testing | 0.95–0.99 |
| account_testing | account_testing | 0.95 |
| slow_drip | card_testing | 0.95–0.98 |
| flash_sale (sale window on) | genuine_rush | 0.90 |
| paypal_outage | paypal_outage | 0.90–0.95 |

Notes:
- `gemini-3.5-flash` often returned 503 (high demand) or 429 (free-tier quota). The fallback chain (`gemini-2.5-flash`, then `gemini-flash-lite-latest`) handled it.
- Gemini's response schema rejects integer enums, so `window_min` is a plain `int`, validated in code.
