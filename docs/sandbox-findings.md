# Sandbox findings (P1 spike)

Sources: `backend/scripts/spike.py`, the FastAPI webhook receiver, and end-to-end runs on Oct 16, 2026.

| Question | Result | Evidence |
|---|---|---|
| OAuth with the sandbox REST app | ✅ Works | `/healthz` → `paypal: ok` |
| Card order with `payment_source.card` | ❌ `422 PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` on every card, including 3DS test cards | `spike-20261016T163103Z.jsonl` |
| Wallet order (PayPal button) | ✅ `201 CREATED` (order `2EC25120XX084312G`) | `POST /shop/orders` method=wallet |
| JS SDK v6 client token (`response_type=client_token`) | ✅ Returns a token | `POST /shop/client-token` |
| Webhook registration via API | ✅ `POST /v1/notifications/webhooks` created `1A9810902M7052934` | `scripts/register_webhook.py` |
| Webhook delivery + postback verification | ✅ Simulated `PAYMENT.CAPTURE.COMPLETED` arrived through ngrok and verified `SUCCESS` (the docs said mock events can't be verified; in practice they were) | `paypal_events` table |
| Do `CCREJECT-*` triggers decline at create or capture? Which codes? | ⏳ Blocked until card processing is enabled | — |
| Which webhooks fire for a declined card? | ⏳ Blocked until card processing is enabled | — |
| Does `SCA_ALWAYS` return `PAYER_ACTION_REQUIRED` for the 3DS test cards? | ⏳ Blocked until card processing is enabled | — |
| Refunds on real sandbox captures | ⏳ Needs a real capture (card, or a wallet order approved in the browser) | — |
| Sandbox rate limits at ~1 request/s | No 429s seen across 28 calls | spike log |
| Transaction Search latency | Not tested yet | — |

**Action needed (owner):** enable *Advanced Credit and Debit Card Payments* on the sandbox app (see `backend/README.md`), then re-run `uv run python scripts/spike.py --count 30 --sca` and fill in the ⏳ rows.

**Decision:** go. The full pipeline works against the real sandbox (orders, client token, webhooks) and in labelled offline simulation. The `live` attack mode switches on as soon as card processing is enabled.

## AI evaluation (`scripts/eval_ai.py`, live Gemini)

10/10 scenarios classified correctly (2 runs each). Latency was 3–10 s per verdict with the reduced thinking settings.

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
