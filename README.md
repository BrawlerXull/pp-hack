# Checkout Guard

**The night guard for your PayPal checkout.** A bot testing stolen cards can push hundreds of tiny payments through a small shop's checkout overnight. The owner wakes up to chargebacks, fees and a PayPal risk review. Checkout Guard watches the checkout and, within seconds, tells a card-testing attack apart from a real sales rush or a PayPal outage. Fixed safety rules then act on that: 3-D Secure for card payments, blocks on the attacking devices, and capped refunds of the test charges before they turn into disputes. Finally it tells the owner what happened, in plain English.

Deterministic code decides, the AI explains. A transparent scorecard classifies the traffic from our own computed features; Gemini writes the explanation for the owner and gives a second opinion only when the scorecard is unsure. A deterministic policy engine then decides what may happen, and every number the owner sees comes from our own database.

Built for the PayPal AI Hackathon 2026. Everything runs in the **PayPal sandbox**. The attacker is a labelled simulation, and the "stolen" cards are sandbox test cards.

## For judges

| | |
|---|---|
| Hosted demo | `https://<frontend-host>` *(placeholder: filled in at submission)*: product page, with links to the shop and the dashboard |
| Owner dashboard | `https://<frontend-host>/guard`, password: `<judge password>` *(given in the submission form)* |
| Sandbox buyer (PayPal button) | `<sandbox-buyer-email>` / `<sandbox-buyer-password>` *(placeholder)* |

### Click path (about 5 minutes)

1. **Product page** (`/`): what Checkout Guard does, attack vs flash sale, the PayPal capabilities it uses, and what's real vs simulated.
   **Shop** (`/shop`): the demo store "Wick & Wax". *Buy now* opens a real PayPal sandbox checkout (JS SDK v6): the PayPal button (log in with the sandbox buyer above) or **Pay by card** (PayPal-hosted card fields; the sheet lists sandbox test cards). The backend runs the guard check before it creates the order. After an attack has switched 3-D Secure on, pay by card with `4868719166101368` (the bank challenge passes and the payment goes through) and with `4868719181895556` (the challenge fails: not charged, and the War Room shows **3-D Secure failed**).
2. **Shop owner → log in** with the judge password. You land on the **War Room**.
3. The 28 days of normal history are seeded automatically and renewed before they age out. To re-seed by hand: **Demo lab → Seed 28 days of normal history**.
4. **Simulation bar (bottom of every dashboard page) → Card-testing attack** (150 offline attempts over about 2 minutes). Watch:
   - The **status pills** in the header: AI, detector, PayPal, simulator and live updates.
   - The **status hero**: the checkout state, the live defences (3-D Secure, blocked devices, held items), and the **Watch → Judge → Act → Explain** pipeline:
     - which triggers fired, and how many seconds after the burst began;
     - how the scorecard (`scorecard-v1`) judged it, with its confidence, and who wrote the words (a Gemini model or our template);
     - what the fixed rules did, and when the owner was alerted, with the time from detection to the first defence.
   - The **chart** (last 10 minutes in 10-second bars): the incident is shaded, with markers where it was detected and where each defence fired.
   - The **detect → judge → decide strip**: per-class probability bars, the scorecard's top reasons, the AI tie-break if one was needed, and which policy rules ran.
   - **From verdict to PayPal**: every action, the policy rule that allowed it, and the PayPal mechanism that carried it out.
   - The **attempts table**, with a *Why* column (the guard's reason or PayPal's decline code). Every simulated row carries a **SIM** badge.
   - **What Meera's phone says**: the alert history as a notification stack.
   - The **Blocked devices** card: each blocked device links to its incident, and **Unblock and trust** marks it as a trusted shopper.
5. **Open the incident**. It shows the **detect → judge → decide strip** (triggers and their tier, who judged, which policy rules ran), the verdict with the scorecard's reasons and our numbers ("×12 vs normal"), the actions taken, refunds waiting for approval (**Refund / Keep**), the attack signature, the morning report, and an **evidence pack for PayPal** to copy or download.
6. **Simulation bar → Flash sale**. It announces a 2-hour sale window and sends a rush of mostly returning customers. Expect no blocks and no refunds: the scorecard calls it a genuine rush (policy R2) or the rules see a returning-customer rush (R3c). **Reset** clears the demo.

   The hosted demo is shared by everyone who logs in:
   - A banner says when another visitor changed something ("Another visitor ran a card-testing simulation 2 min ago").
   - Another visitor can't start a run within 30 seconds of yours.
   - After 30 idle minutes the demo stands itself down: it closes the open incident and keeps its report, switches the defences off, unblocks devices and restores the default settings.
7. **Investigate** (AG Studio): every attempt, incident and action from the last 48 hours as an editable report, with our own **AI verdict** and **Defence timeline** widgets. Click a bar to cross-filter the page. **Ask the Guard analyst** opens the AI assistant: ask "What happened in the last incident?" or "Investigate the latest attack", and it reads the incident through Checkout Guard's tools and has Studio's dashboard team build the page. With *Auto-investigate* on, a new incident gets its own investigation page without asking.
8. **Settings**: autopilot, kill switch, refund caps, stand-down time, "I'm running a sale".
9. Optional: the **attacker terminal** for a split screen, `uv run python scripts/attack.py card_testing --url https://<frontend-host>/api --password <judge password>` (see [Attacker terminal](#attacker-terminal)).

## What is real and what is simulated

| Real (PayPal sandbox, live services) | Simulated, and labelled on screen |
|---|---|
| PayPal OAuth, order creation and capture (Orders v2), 3-D Secure (`SCA_ALWAYS`) on card orders while the guard is in 3-D Secure mode, refunds (Payments v2), webhook signature verification | The attacker: scenario scripts in `backend/app/simulator/`, rows tagged `source=simulator` and shown with a **SIM** badge |
| Shop checkout with the PayPal button (sandbox buyer accounts) or **Pay by card** (PayPal-hosted Card Fields, sandbox test cards; the bank's 3-D Secure result is checked on our server before capture) | "Stolen" credentials: sandbox test cards and synthetic buyers |
| **Live** simulation mode: real sandbox orders with sandbox test cards; declines come from PayPal's `CCREJECT-*` name triggers | **Offline** simulation mode: no PayPal call, the outcome is synthesised and stored with `paypal_live=false` (the default mode, and the simulation bar uses it) |
| Gemini calls (explanations, tie-break second opinions, report writer) | The PayPal outage scenario (injected processor errors; offline only) |
| Postgres, worker, live dashboard (server-sent events) | 28 days of seeded "normal" history (`source=seed`) |
| Optional alert push to Slack or ntfy (`NOTIFY_WEBHOOK_URL`) | The phone on the War Room is a mock-up of the owner's phone, showing the real alert texts |
| The human check is Cloudflare Turnstile, verified on our server with Cloudflare | It runs with Cloudflare's public test keys, so it always passes (the widget says so). Set `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` for a real one |

## PayPal APIs we call

All calls go to `https://api-m.sandbox.paypal.com`. The backend refuses to start with any other base URL.

| API | Endpoint | Used for |
|---|---|---|
| OAuth 2.0 | `POST /v1/oauth2/token` | Server access token, cached until it expires |
| Client token (JS SDK v6) | `POST /v1/oauth2/token` with `response_type=client_token` | Browser-safe token that loads the shop's PayPal button and Card Fields (`/shop/client-token`). Without it the PayPal button still loads with the public client id and card payments are switched off |
| JS SDK v6 Card Fields | `components: ["paypal-payments", "card-fields"]`, `usePayPalCardFieldsOneTimePaymentSession().submit(orderId)` | **Pay by card** in the shop. PayPal hosts the number, expiry and CVV inputs (iframes) and runs the bank's 3-D Secure window; the card number goes from the browser to PayPal and never reaches our server |
| Orders v2 | `POST /v2/checkout/orders`, `POST /v2/checkout/orders/{id}/capture`, `GET /v2/checkout/orders/{id}` | Every checkout. We send `PayPal-Request-Id` for idempotency, a `custom_id` that links webhooks to attempts, and on card orders `payment_source.card.attributes.verification.method`: `SCA_ALWAYS` while the guard is in 3-D Secure mode (and for the simulator's cards then), else `SCA_WHEN_REQUIRED` for browser cards. A browser card order carries no card data. Before capturing it we `GET` the order and read `payment_source.card.authentication_result` (liability shift, 3-D Secure enrollment and authentication status) plus the brand, last 4 and BIN |
| Payments v2 refunds | `POST /v2/payments/captures/{id}/refund` | Capped refunds of matched attack captures. Each refund has a stable `PayPal-Request-Id`, so a re-send after a crash is made exactly once |
| Webhooks | `POST /v1/notifications/verify-webhook-signature` | Every incoming event is verified against the raw request body. Unverified events are stored but never acted on |
| Webhook management | `GET/POST/PATCH /v1/notifications/webhooks` | `scripts/register_webhook.py` only, at setup time |

We do **not** call the webhook simulator (`/v1/notifications/simulate-event`), Transaction Search or the Disputes API. See [Built vs planned](#built-vs-planned).

## AI: who decides, who explains

| Role | What it does | What it can never do |
|---|---|---|
| **Scorecard classifier** ([app/detection/classifier.py](backend/app/detection/classifier.py), `scorecard-v1`) | **Decides** the classification: `card_testing`, `account_testing`, `genuine_rush`, `paypal_outage` or `unclear`. Each class has a short list of documented additive rules over our computed features (cards and PayPal accounts per device, decline rate and decline-code mix, wallet orders never approved, timing regularity, basket variety, error spread, returning customers…). Scores go through a softmax into per-class probabilities; a small margin or a top probability under 0.55 is `unclear`. Its top reasons are the verdict's evidence, with our own numbers | Read attacker text, call the network, or change between two runs on the same data |
| **Gemini explainer** ([app/ai/analyst.py](backend/app/ai/analyst.py) `explain`) | Writes the owner-facing summary of the scorecard's decision | Change the classification or the confidence. Text that argues for another class, or cites numbers that don't match ours, is replaced by our template |
| **Gemini tie-breaker** (`analyse`, [app/incidents/decision.py](backend/app/incidents/decision.py)) | Asked only when the scorecard is unsure (`unclear`, or a margin under 0.30), without being shown the scorecard's answer. If it agrees with the scorecard's top class (with evidence that matches our numbers), that class is used with confidence capped at 0.80 (in practice it stays under 0.70, so policy R3b applies); if it disagrees, the incident stays `unclear` (soft defences only) | Pick a class the scorecard did not rank first, raise a confidence, or reach the 0.85 refund floor |
| **Report writer** ([app/ai/reporter.py](backend/app/ai/reporter.py)) | Writes the wording of the phone alert, the morning report and the PayPal evidence pack | Write numbers. It may only use `{{placeholders}}`, which we fill from the database |
| **Guard analyst** (Investigate page; [frontend/src/components/studio/ai.ts](frontend/src/components/studio/ai.ts), [app/ai/studio_agent.py](backend/app/ai/studio_agent.py)) | An AG Studio Agent Framework agent: reads the guard's live state and incidents through read-only tools, queries the report's data, and hands dashboard building to Studio's built-in agents. Each LLM turn goes through our backend to Gemini (`POST /studio/agent/turn`), so the key stays on the server | Change defences, approve refunds or move money: its tools only read |

Why not let the LLM decide: a Gemini call takes 3 to 25 seconds, free-tier quota runs out, the same input can get a different answer, and attacker-controlled text reaches the prompt. The scorecard answers in milliseconds, and a confident verdict's defences are committed **before** Gemini is asked for the words. The incident stores `classifier_version`, the per-class probabilities, the margin, the top reasons and which path decided (`scorecard`, `scorecard+gemini_agree` or `gemini_disagree_unclear`); the audit log has the same in its `verdict` event. The War Room shows "Decided by: scorecard-v1" with the probability bars and "Explained by: <model> / template".

**Models:** `gemini-3.5-flash`, then `gemini-2.5-flash`, then `gemini-flash-lite-latest` (`GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`).

**Guards on the AI's output:**
- **Evidence check.** An evidence value that does not match our computed feature (5 % tolerance) is dropped. A second opinion left with no matching evidence does not count as agreement. The War Room shows a badge when claims were dropped.
- **Number guard.** A number in the AI's prose that matches no computed feature makes us replace that text with a sentence built from verified evidence.
- **Report checks.** An AI draft that writes its own digits, uses an unknown placeholder or leaves out a required one is replaced by the template.
- **Privacy.** The AI sees aggregated features plus 20 masked rows: SKU, amount, outcome, decline code, an 8-character device prefix and the email domain (stripped to a safe character set). It never sees card numbers, names, emails or credentials.

**When Gemini is down, everything still works:**
1. If a model fails (429 quota, 503 overload, timeout), the next one is tried. If every model fails on `GEMINI_API_KEY`, the same chain is tried on the optional `GEMINI_API_KEY_2`, which has its own breaker.
2. A per-model **circuit breaker**, shared by the API and the worker through Postgres, pauses a failing model. It honours Gemini's `retryDelay`, pauses for an hour on a daily quota, and pauses every model until restart on a rejected key. Gemini is then retried on a 2/4/8-minute backoff; the scorecard never waits for it.
3. With no model answering, the scorecard still decides and acts (including refunds when it is sure), and the text is our own template. Only an **unsure** scorecard loses something: no second opinion, so the policy's R3b/R3d rules apply (the same as when Gemini agrees with an unsure scorecard). Whether Gemini wrote the explanation never changes the defences.
4. A failed explanation keeps the AI's last wording for the same class. A failed re-check of a tie-break the AI had agreed to keeps that agreed class ("kept"); it yields to new triggers that contradict it, and after 3 failures or 20 minutes it is stale.
5. `AI_ENABLED=false` switches the AI off: no Gemini call at all, labelled "AI disabled".

**Evaluation** (all SIMULATED; the weights were tuned on the same simulator, so this shows consistency with our model of attacks, not real-world accuracy). [scripts/eval_detection.py](backend/scripts/eval_detection.py) runs 13 scenarios × 20 seeds through the real simulator, triggers and scorecard on a simulated clock, with no network and no AI. Excerpt (full table, confusion matrices and what still fails: [docs/eval-detection.md](docs/eval-detection.md)):

| Scenario | Fraud-tier triggers | Median time to detect | Final verdict correct |
|---|---|---|---|
| card_testing | 100% | 10 s | 100% |
| account_testing (most wallet logins never approved) | 95% | 10 s | 100% |
| slow_drip (one attempt every 12 s) | 100% | 85 s | 100% |
| stealth_bot (new device every try, one try every 1-5 min) | 100% | about 25 min | 100% |
| attack during an announced sale | 100% | 5 s | 100% |
| flash sale, announced / unannounced (first-time shoppers) | 0% / 0% | 15 s | 100% genuine rush |
| rush on the cheapest item plus one shopper fumbling a security code and two more cards | 0% | 15 s | never an attack (some "unclear") |
| paypal_outage | 0% | 10 s | 100% |
| normal_night | 0% (no trigger at all) | - | - |

[scripts/eval_ai.py](backend/scripts/eval_ai.py) (needs a Gemini key and spends quota) compares three columns on the same traffic: scorecard alone, Gemini alone, and both (Gemini only breaking ties).

## Safety rules (policy engine)

[app/policy/engine.py](backend/app/policy/engine.py) is pure and unit-tested. It acts on the scorecard's classification. First match wins, except R0, which runs before any verdict.

| Rule | When | What happens |
|---|---|---|
| **R0** | A fraud-tier trigger fired (`decline_spike`, `fraud_declines`, `cheap_sku_card_cycling`, `slow_declines`) | 3-D Secure at once, before the verdict. Not during an announced sale, and not with the kill switch on |
| **R1** | Kill switch on | Notify only |
| **R2** + **RELAX** | Genuine rush or PayPal outage, confidence ≥ 0.70 | Notify, and undo this incident's 3-D Secure and SKU holds (device blocks stay). An outage call never relaxes while fraud-tier triggers fired in the incident: anyone can make captures fail on purpose, so that gets R3 |
| **R3** | `unclear`, or confidence < 0.70 | 3-D Secure for cards, and notify |
| R3b | Scorecard unsure and not contradicted (Gemini agreed, or was down, off or unverifiable), fraud-tier trigger, no sale, PayPal not failing | 3-D Secure, block only the *strong* devices (several cards or payers, or several fraud/probing refusals, never a known customer), and hold the targeted item for first-time shoppers. No refunds |
| R3c | Volume-only triggers, mostly returning customers, few declines | Notify only (no friction for a real rush) |
| R3d | Scorecard unsure and not contradicted, and only PayPal errors | Notify only (3-D Secure would only add a failing step) |
| **R4** | Attack, confidence ≥ 0.70 | 3-D Secure, block the attacking devices, hold the targeted item for first-time shoppers, notify. During an announced sale: 3-D Secure and notify only |
| **R5** | Attack, confidence ≥ 0.85, outside a sale | Refund the matched attack captures. Only the scorecard on its own reaches 0.85: an AI-confirmed tie-break is capped at 0.80 |

**Payment holds: void instead of refund.** A first-time shopper's payment is *authorised* at checkout and *captured* after a short review window (10 minutes by default, `Settings → Hold first-time shoppers' payments`; 0 turns it off). Returning customers are charged at once. During an incident, first-time shoppers' payments are held until the incident is judged. When the policy would refund the attack (R4/R5), its held payments are **voided** instead (`/v2/payments/authorizations/{id}/void`): nothing settles, so no PayPal fee is lost (a refund keeps the fixed fee) and there is nothing left for the cardholder to dispute. Strong matches are voided automatically; weak ones wait for the owner (**Void / Keep**). Every other hold is captured (`/authorizations/{id}/capture`): once its window passes, when the incident covering it is closed or judged harmless, or after 24 hours at the latest. Because the window starts at checkout, the payments an attack makes *before* it is detected are still holds when it is confirmed. A hold does not stop a card tester learning that the card works (the bank approved it); 3-D Secure does that. Holds stop the money, the fee and the dispute. Verified on the live sandbox: a 3-D Secure card payment authorised, then captured when the incident closed, and a hold voided.

**Refund limits (R5):**
- **Caps.** Per transaction $10, per incident $100 and per day $250 across all incidents (rolling 24 hours) by default (Settings). Above a cap, a refund waits for the owner. Autopilot off means every refund waits.
- **Neutral note.** The refund's note to the cardholder says it was refunded as a precaution and asks them to contact the shop. It never says "card testing": the card's owner is usually the victim. A blocked shopper sees the shop's contact email (`SHOP_CONTACT_EMAIL`).
- **Matching.** Matching is deterministic: we never take it from the AI.
  - A *strong* match (a strong device, or one already blocked as strong) can be refunded automatically.
  - A *weak* match waits for the owner's **Approve / Reject**. Weak means a flagged device, a single honest-looking decline, or only a suspect email domain.
  - So does a capture that passed 3-D Secure.
- **Disputes.** A disputed capture is never refunded automatically: the refund would pay the buyer twice.
- **Stale verdict.** A stale kept AI-confirmed verdict routes every refund to the owner (it is capped below the refund floor anyway).
- **In flight.** A refund still in flight after 5 minutes (a crash, or no answer from PayPal) is re-sent by the worker with the same `PayPal-Request-Id`, up to 6 times.

**Before any incident:**
- **Guard check.** On every checkout ([app/guard/check.py](backend/app/guard/check.py)) the guard checks the kill switch, then blocked devices, then device velocity (6+ attempts or 2+ declines in 10 minutes → challenge), then network velocity (6+ devices with 3+ declines from one /24 in 10 minutes → challenge, so a bot that rotates its device id still meets the human check), then held items, then 3-D Secure mode. During an attack, 3-D Secure is **targeted**: only first-time devices are asked. A returning customer (a capture from before the attack window) checks out without extra friction.
- **Device and network signals.** A browser's device id is pinned in a signed cookie on its first checkout, so a bot that keeps its cookies can't rotate ids. The client IP is read from `X-Forwarded-For` counted `TRUSTED_PROXY_HOPS` from the right (the left end is whatever the client typed) and stored only as a keyed hash, with its /24 network.
- **Device blocks** expire after 24 hours. A device the owner unblocks is **trusted** for 30 days.
- **Stand down.** After `deescalate_after_min` quiet minutes (10 by default) the incident closes and the checkout goes back to its state before the incident.

**If the guard itself is slow, the checkout still works (fail-open).** The guard check gets `GUARD_CHECK_BUDGET_MS` (default 800 ms). If it runs over or the database errors, the payment goes on with the last guard state the server saw: blocked devices stay blocked and 3-D Secure stays on, but the per-device velocity count and the item hold are skipped. With no recent state the payment is allowed and recorded as *Guard too slow: let through*. The public `/v1/guard/check` API behaves the same way.

**Hardening.**
- **Webhooks.** A body over 256 KB is refused before it is read whole. A signing certificate that is not served over https from paypal.com is refused without asking PayPal. Buyer names, emails, addresses and phone numbers are removed before an event is stored; the signature is still checked against the body exactly as received.
- **Rate limits** per client IP, counted with the same trusted-proxy rule as the guard. Login: 10 wrong passwords per 5 minutes (correct logins don't count). Orders: 30 per minute. Precheck: 60. Human check: 10. Card tokens: 20. AI agent turns: 60. `RATE_LIMITS_ENABLED=false` switches them off for load tests.
- **One open incident per shop**, enforced by a partial unique index in Postgres.

## Run it

### One command (Docker)

```bash
cp .env.example .env     # optional: PayPal sandbox app + GEMINI_API_KEY (without them: offline demo, rules only)
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:3000 | Product page |
| http://localhost:3000/shop | Shop |
| http://localhost:3000/guard | Dashboard (password from `ADMIN_PASSWORD`, default `guard-demo`) |
| http://localhost:8000/docs | API docs |

- **Services:** `db` (Postgres 16, which also creates `checkout_guard_test`), `api` (runs `alembic upgrade head`, then uvicorn), `worker` (starts once the API is healthy) and `frontend` (built with `BACKEND_URL=http://api:8000`).
- **Ports:** stop local dev servers on :3000 and :8000 first.

### Manual

```bash
cp .env.example .env                       # PayPal sandbox app credentials + GEMINI_API_KEY
createdb checkout_guard && createdb checkout_guard_test
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000 --no-proxy-headers    # API (terminal 1)
uv run python -m app.worker.main           # worker (terminal 2): without it nothing detects attacks
cd ../frontend && pnpm install && pnpm dev # http://localhost:3000 (terminal 3)
```

PayPal webhooks (ngrok + `scripts/register_webhook.py`), enabling card payments in the sandbox and the demo API are covered in [backend/README.md](backend/README.md).

### Deploy (Render)

[render.yaml](render.yaml) is a Blueprint for the API, the worker, the frontend and Postgres, all on paid plans so nothing sleeps or expires before Dec 15.
- **Migrations** run as the API's `preDeployCommand`.
- **Secrets.** `ENVIRONMENT=production` makes startup refuse the demo `ADMIN_PASSWORD`, `SESSION_SECRET` and `SHOP_API_KEY`. The Blueprint generates all three, and the generated admin password is the judge password.
- **Health.** Render's health check uses `/livez`: the process is up and the database answers. `/healthz` is the full readiness report (database, PayPal, Gemini, live stream) and answers 503 when degraded. A daily GitHub Action ([.github/workflows/healthcheck.yml](.github/workflows/healthcheck.yml)) checks it on the `DEMO_URL` secret.
- **CI.** [.github/workflows/ci.yml](.github/workflows/ci.yml) runs ruff, the migrations with a drift check, and pytest against Postgres for the backend, and tsc, lint and build for the frontend.

### Attacker terminal

For a split-screen video, put this terminal next to the War Room. It logs in as the owner, starts the same labelled simulation as the Demo lab, and prints one line per attempt as it lands, plus `>> GUARD:` lines when the defender acts. Ctrl-C stops the run.

```bash
cd backend
uv run python scripts/attack.py card_testing --count 120 --speed 3                 # local API, offline
uv run python scripts/attack.py card_testing --mode live --count 20                 # real sandbox orders
uv run python scripts/attack.py flash_sale --url https://<frontend-host>/api --password <judge password>
```

`flash_sale` announces a 2-hour sale window first, like the simulation bar (`--no-announce-sale` runs it unannounced, which the guard may answer with 3-D Secure or blocks).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Header pill **AI cooling down** or **AI off · scorecard only**, verdicts "Explained by: Checkout Guard's own template" | Gemini answered 429 (quota) or 503 (overload) on every model. The scorecard keeps deciding and acting; only the wording falls back to the template, and unsure cases get no second opinion (policy R3b/R3d). Wait, use a key with quota, or set `AI_ENABLED=false` to run without Gemini on purpose. A rejected key pauses the AI until the processes restart |
| Card orders fail with `PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING` | The sandbox business account lacks Advanced Credit and Debit Card Payments. Enable it under Apps & Credentials → your sandbox app → Features, or create a new US sandbox business account. PayPal button (wallet) orders work without it, and offline simulations need neither |
| The shop says **"PayPal not configured: use the Demo lab (offline)"** | The backend has no `PAYPAL_CLIENT_ID`. Fill the PayPal sandbox app credentials in `.env` and restart, or demo with offline simulations |
| Header pill **Detector stopped** | The worker is not running: start `uv run python -m app.worker.main` (or the `worker` service) |
| `/healthz` answers 503 | Its JSON names what is down: `db`, `paypal`, `ai` (`cooling_down` / `key_rejected`) or `stream` (`reconnecting`: the API lost its Postgres LISTEN connection for live updates and is retrying with backoff; the dashboard keeps polling meanwhile) |
| The API refuses to start: "ENVIRONMENT=production refuses default secrets" | Set a real `ADMIN_PASSWORD`, `SHOP_API_KEY` and a 32+ character random `SESSION_SECRET` |
| No webhook events arrive | PayPal needs a public https URL: run ngrok, then `uv run python scripts/register_webhook.py https://<host>` and restart the API |

## Built vs planned

| Feature | State |
|---|---|
| Guard check, detection (tiered statistical triggers: Poisson, binomial, CUSUM), scorecard classifier with Gemini explanations and tie-breaks (evidence check, number guard), offline detection eval, policy R0–R5 + RELAX, executor with capped refunds, owner approvals, device block and trust lists, sale windows, reports and evidence pack, alert history + webhook push, system pills and heartbeat | **Built** |
| PayPal button checkout (JS SDK v6), Orders v2, Refunds, webhook verification, labelled simulator (offline + live sandbox) | **Built** |
| Disputes | **Built:** `CUSTOMER.DISPUTE.*` webhooks mark captures as disputed, which blocks automatic refunds and shows in the evidence pack. **Planned:** the Disputes API (we do not read or answer disputes through it) |
| Dashboard on AG Grid + AG Charts (Community) | **Built** |
| AG Studio Investigate page: custom widgets, night theme, Guard analyst agent on Gemini with Studio's built-in agents, auto-investigation of new incidents | **Built** (needs `NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY` to drop the trial watermark) |
| Docker compose, Render Blueprint, CI, daily health check, attacker terminal | **Built** |
| Transaction Search (baseline from real PayPal history instead of the seeded one) | Planned |
| PayPal Card Fields in the shop with server-checked 3-D Secure (`sca_failed` outcome) | **Built**, verified in a real browser against the sandbox (3-D Secure pass captured, fail recorded with no charge) |
| Device/payer graph clustering, and a small trained model once there is labelled real attack data (none today) | Planned |

## Layout

| Folder | What |
|---|---|
| [backend/](backend/) | Python 3.12 + FastAPI: PayPal integration, guard, detection, AI, policy, webhooks, worker, simulator |
| [frontend/](frontend/) | Next.js 16: demo shop and owner dashboard. UI only; `/api/*` is proxied to the backend |
| [docs/](docs/) | Product, architecture, backend and AI design, roadmap, demo plan, sandbox findings |

```bash
cd backend && uv run ruff check . && uv run pytest     # Postgres test DB: checkout_guard_test
cd frontend && pnpm exec tsc --noEmit && pnpm lint .
```

## License

MIT
