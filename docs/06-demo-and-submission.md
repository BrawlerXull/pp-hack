# 06 · Demo, Submission & Risk

## 1. Demo video (target 2:45; judges may stop at 3:00, Rules › Submission)
| Time | Screen | Narration / expected result |
|---|---|---|
| 0:00–0:20 | Meera's shop at night; overlays of real quotes (HN 291 pts, WooCommerce issues) | The problem: card testing hits small PayPal shops, and nothing warns them |
| 0:20–0:35 | Dashboard "All quiet"; settings (refund cap $10/txn, autopilot on) | Connected to the PayPal sandbox in minutes |
| 0:35–0:50 | **Control:** run `flash_sale` with the owner's sale window on → spike → verdict *Genuine rush, 0.9x* → no defences | It doesn't punish real customers |
| 0:50–1:20 | Run `card_testing` (banner: **SIMULATED ATTACKER, real PayPal sandbox**); bot terminal + War Room charts climbing, declines piling up | The attack is visible |
| 1:20–1:45 | Incident card: *Card-testing, 0.9x*, evidence bullets, policy log ("rule 4 allowed SCA_ON, BLOCK_FP") | AI judgment with reasons; rules decide |
| 1:45–2:10 | A new checkout gets the **3DS challenge** (bot fails); PayPal sandbox dashboard shows **refunds**; phone-style alert "Attack stopped, 6 refunded ($18)" | PayPal is the hands |
| 2:10–2:30 | Morning report + evidence pack; "Ask the Guard: show attempts by email domain" → AG Studio agent builds the widget | AI explains; AG Studio agent |
| 2:30–2:45 | Eval results (TTD, $ saved, 0 false blocks) + architecture strip (PayPal APIs · Python backend · Gemini · AG Studio · Render) | Close: *"Meera slept through it."* |

**Backup plan:**
- Keep a recorded sandbox session (fixtures + screen capture). The app's **"Replay recorded run"** mode shows the banner *"Replay of recorded sandbox session from <date>"*.
- Never mix replay into live footage without the label.
- If Gemini is down, the scorecard still decides; the verdict shows "Explained by: Checkout Guard's own template" and the header pill "AI off · scorecard only".

## 2. Pitch deck (8 slides)
1. Problem
2. Meera's night
3. Why existing tools miss it
4. Checkout Guard: 4 steps + 1 decision
5. Architecture (what PayPal does / what AI does / what rules do)
6. Safety and human oversight
7. Eval results
8. Roadmap (WooCommerce plugin, multi-store) + team

## 3. Evidence to capture
- PayPal sandbox: refunds list, webhook delivery log
- 3DS challenge screen
- War Room + agent-built widget
- Render services page
- eval output
- APIMatic Context Plugin in use while writing the Python PayPal code
- architecture diagram (doc 02)

## 4. Likely judge questions
| Question | Honest answer |
|---|---|
| Why not PayPal Fraud Protection Advanced? | It scores single transactions, and many small shops haven't set it up. We respond to a whole attack (SCA switch, refunds, report) and cover stolen-PayPal-account testing. It complements FPA (Idea › Risks) |
| Will it block real customers? | Soft defences come first; 3DS doesn't stop real cardholders. Flash-sale scenario: 0 false blocks in eval |
| Can the AI refund the wrong order? | No. The AI recommends; deterministic matching + caps + owner approval decide |
| What's real vs simulated? | Doc 02 §8: PayPal sandbox calls are real; the attacker and the "stolen" credentials are simulated and labelled |
| Production path? | `/v1/guard/check` is already an HTTP API, so a WooCommerce/PayPal plugin calls it; PayPal partner onboarding for multi-merchant |
| Does SCA_ALWAYS stop a bot? | A bot with a stolen card can't complete the issuer challenge; shown with the sandbox challenge-fail card |
| Cost? | One LLM call per incident + re-checks, so cents per attack; detection is deterministic |
| Why a separate Python backend? | The guard is a service any checkout can call; the UI is just one client |

## 5. Risk register
| Risk | Sev | Mitigation | Contingency |
|---|---|---|---|
| Sandbox attack not convincing / no decline webhooks | High | P1 spike first; own attempt log is the main signal | `CCREJECT-*` declines; webhooks shown for captures/refunds |
| AG Studio trial lapses before Dec 15 | High | Ask on day 1; time the activation | App still works if watermarked; video as record |
| Python ↔ AG Studio `clientToolRunner` format unclear | Med | P1 round-trip check | Narrow tool set; agent logic in a small Next.js route proxying to Python |
| Python `paypal-server-sdk` gaps | Low-Med | Check in P1 | httpx for those endpoints |
| Render free-tier expiry / spin-down | Med | Paid plans via credits | Local `docker compose` path |
| False positives in demo | High | Flash-sale test; soft-first policy | Autopilot "SCA only" |
| 3DS UI unreliable in sandbox | Med | Test in P1 | Frictionless-fail card |
| LLM inconsistency | Med | Schema, low temp, evidence check, DB-filled numbers | Rule fallback |
| Two codebases slow the team | Med | Generated TS client from OpenAPI; clear role split | Cut dashboard extras before backend scope |
| Sandbox rate limits | Med | Throttle; test in P1 | Compressed timeline |
| Secrets leaked | High | gitleaks, `.env.example` | Rotate keys |
| Scope creep | Med | *(opt)* only after P5 | Cut |

**Top 3 improvements to prioritise:**
1. The flash-sale control.
2. Eval numbers.
3. The AG Studio agent.

## 6. Compliance checklist (Rules › Pre-submission checklist + sponsor conditions)
- [ ] All members eligible (age, country, no PayPal/Devpost/judge ties); registered; invites accepted; Representative named
- [ ] PayPal sandbox integration central; APIs listed in README
- [ ] AI is part of the product; models and their roles named
- [ ] Public repo with all code/assets/instructions; **MIT license shows in About**
- [ ] Hosted demo URL works **and** README local run works from a fresh clone
- [ ] Judge login, sandbox buyer accounts, test cards in README; no real secrets
- [ ] Demo keeps working to **Dec 15**: token refresh, paid DB, AG license, Render credits
- [ ] Video < 3:00, public YouTube, real app, no copyrighted music, simulations labelled
- [ ] Devpost description: features, AI tools and use, PayPal APIs, sponsor tools
- [ ] AG Grid: custom widgets + theming + layout **and** Studio Agent Framework visible
- [ ] Render: `render.yaml` in repo; services live
- [ ] APIMatic described honestly as dev-time use (if claimed)
- [ ] Channel3 / Bryntum not claimed
- [ ] Third-party licenses respected (boilerplate, AG terms)
- [ ] All English
- [ ] **Submitted** (not draft) by Nov 10; hard deadline Nov 12, 12:00 PM PT

## 7. Immediate next steps
1. Team registers on Devpost; Representative creates the project and invites everyone (today).
2. Post the three Discord questions (doc README › Open questions).
3. Create the sandbox business account, REST app, buyer accounts and ngrok webhook.
4. P1: write and run the Python spike + webhook logger; check `clientToolRunner` ↔ Python; fill in `sandbox-findings.md`; go/no-go Oct 15.
5. P2: restructure the repo into `frontend/` + `backend/` and start the nightly baseline seeder.
