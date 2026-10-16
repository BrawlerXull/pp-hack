# 04 · AI Design

**Principle: AI judges and explains, deterministic code decides and acts.** The AI is meaningful (Rules › Project requirements) because it does the one thing thresholds can't do well: weigh mixed signals to tell an **attack** from a **real rush** or a **PayPal outage**, and explain the call to a non-technical owner.

## 1. Pipeline
| Step | Type | Module | Input → output |
|---|---|---|---|
| Feature extraction | Deterministic | `detection/features.py` | Attempts in 1/5/15/60 min windows → features (below) |
| Trigger | Deterministic | `detection/triggers.py` | Features vs baseline → open incident when **any**: velocity z-score > 4 · decline rate > 3× baseline with ≥ 10 attempts · cheapest-SKU share > 80% with ≥ 15 attempts · blocklisted fingerprints reappearing |
| Classify | **AI** | `ai/analyst.py` | `AnalystInput` → `Verdict` (schema below) |
| Decide | Deterministic | `policy/engine.py` | Verdict + settings + guard state → allowed actions |
| Act | Deterministic | `actions/executor.py` | Actions → guard state changes, PayPal refunds |
| Re-assess | **AI** | `ai/analyst.py` (reassess mode) | Updated features + actions taken → `continue / escalate / de-escalate` |
| Explain | **AI** | `ai/reporter.py` | Audit log + DB numbers → alert, report, evidence pack |
| Ask the Guard | **AI** | `ai/studio_agent.py` | Owner question → AG Studio tool calls + our read-only data tools |

### Features
- attempts/min
- decline rate and decline-code mix (card vs processor errors)
- PayPal 5xx/timeout rate
- unique cards (BIN+last4) and unique payer IDs per attempt
- cheapest-SKU share
- average ticket
- new-email-domain share (domains not seen in the baseline)
- fingerprint concentration (top fingerprint share, entropy)
- SCA failure rate (when SCA is on)
- whether a sale window is active
- baseline mean/std for this hour of week

## 2. Models
| Job | Model | Why |
|---|---|---|
| Verdict + re-assessment | `gemini-3.5-flash` → fallback `gemini-2.5-flash` → `gemini-flash-lite-latest` | Low thinking budget: 3–10 s per verdict; one call per incident plus re-checks |
| Alert, report, evidence pack | Same chain | Prose only; numbers filled from the DB |
| Studio agent | Same chain (tool use) | Planned for P4 |

- **Structured output:** Pydantic models passed as Gemini `response_schema` (JSON mode).
- **Settings:** low temperature. `model_id` and `prompt_version` are stored on every incident and report.

## 3. Schemas (Pydantic)
```text
AnalystInput
  window_features: {1m,5m,15m,60m: Features}
  baseline: Features (same hour-of-week)
  sale_window_active: bool, sale_note: str|None
  paypal_health: {error_rate_5m, timeout_rate_5m}
  sample_rows: list[MaskedAttempt]   # ≤ 20; email_domain, sku, amount, outcome, decline_code, fp_hash[:8], bin_country
  current_guard_state, actions_so_far

Verdict
  classification: card_testing | account_testing | genuine_rush | paypal_outage | unclear
  confidence: float 0–1
  evidence: list[{feature: FeatureKey, observed: number, baseline: number, why: str}]   # FeatureKey is an enum → no made-up features
  recommended_actions: list[SCA_ON | CHALLENGE_ON | BLOCK_FP | HOLD_SKU | REFUND_MATCHED | NOTIFY | NONE]
  attack_signature: {sku?: str, fingerprint_hashes?: list[str], email_domains?: list[str], window_start, window_end} | None
  uncertainty_notes: str
```

## 4. Policy engine: the hard rules (unit-tested)
1. `kill_switch` → no actions except NOTIFY.
2. `classification in {genuine_rush, paypal_outage}` **or** a sale window is active → no blocking or refunds. A notification only; for an outage, also "don't blame customers".
3. `unclear` or `confidence < 0.7` → `SCA_ON` + `NOTIFY` only.
4. `card_testing | account_testing` with `confidence ≥ 0.7` → `SCA_ON`, `BLOCK_FP` (fingerprints in the signature), `HOLD_SKU` if one SKU is over 80% of the window, `NOTIFY`.
5. `REFUND_MATCHED` is allowed only when `confidence ≥ 0.85` **and** autopilot is on. Captures are picked **by deterministic matching**, never by the model listing IDs:
   - inside the incident window
   - SKU and fingerprint match the signature
   - `source` doesn't matter
   - each ≤ `max_auto_refund_per_txn`
   - running total ≤ `max_auto_refund_per_incident`
   - anything else → `awaiting_approval`
6. The AI can recommend; only policy-approved actions run. Every decision is written to `audit_log` with the rule that allowed it.
7. **De-escalation:** after `deescalate_after_min` with triggers clear, restore the previous `guard_state`.

## 5. Guardrails against hallucination and failure
- Schema-validated output, plus a check that every evidence `feature` exists and its `observed` value matches our computed value (± rounding). If not, retry once, then fall back.
- **Numbers in alerts and reports are filled from the DB.** The model writes the prose around placeholders like `{{refunded_count}}` and `{{refunded_total}}`.
- **Timeout or API error** → rule-based fallback verdict `unclear` (SCA only), with an "AI unavailable" badge. Retries use backoff.
- **Prompt-injection surface:** the only user-controlled text that reaches the model is email domains and SKU names. These are sanitised (charset whitelist, length cap) and passed as data fields, never as instructions.
- **Privacy:** no PAN, names, addresses, full emails, tokens or credentials are ever sent (see doc 02 §7).

## 6. AG Studio "Ask the Guard" agent
- **Runs on:** the Studio Agent Framework's `clientToolRunner` on the frontend → `POST /agent/studio` on the backend → Gemini.
- **Studio's tools** (add or configure widgets, execute queries) are passed through. **Our extra tools are read-only:** `query_attempts(filters)`, `get_incident(id)`, `get_actions(incident_id)`.
- **It cannot take defence actions.** Changes go through the dashboard buttons, which call the policy-gated endpoints.
- **Demo prompts:**
  - "Show tonight's attack by email domain"
  - "Compare declines with last Tuesday"
  - "Build me a refund-status board"

## 7. Evaluation (`eval/`)
Six scenarios (doc 03 §5) × 3 seeds, in two modes:
- **replay:** recorded sandbox events, Gemini live; fast, cheap; run on every AI change
- **live:** full sandbox run; nightly from P5

**Metrics** (published in README and on one slide):

| Metric | Target |
|---|---|
| Classification accuracy across scenarios | ≥ 95% (100% on card_testing vs flash_sale) |
| Attack detection precision / recall | ≥ 0.95 / ≥ 0.95 |
| Median time-to-detect (simulated time) | < 5 min |
| Median time-to-contain | < 6 min |
| Attack captures refunded within caps | 100% of matched |
| **Legitimate attempts blocked** | **0** |
| Evidence-check failures (hallucinated features or numbers) | 0 after retry |
| Cost per incident | Logged (tokens × price) |
