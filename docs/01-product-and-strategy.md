# 01 · Product & Strategy

## 1. The problem (Idea › The problem)
Criminals buy stolen card numbers and PayPal logins in bulk. To find out which ones still work, they point a bot at a small shop's checkout and make many tiny purchases. Each payment that succeeds tells them a credential is live.

The shop owner pays three ways:
- **Disputes:** lost money plus fees.
- **Account risk:** a high dispute rate can get the shop's PayPal account limited.
- **Time:** hundreds of refunds done by hand the next morning.

Small shops and nonprofits have no fraud team. They find out after the damage is done.

**Evidence** (Idea › Why this idea):
- Hacker News thread, 291 points ("PayPal does not seem equipped to deal with this").
- Recurring GitHub issues on WooCommerce's PayPal plugin (800k+ stores).
- 23 attack-related support threads between Feb and Oct 2026, plus a pinned warning.
- Reddit reports of fake "donations" from charities.

**Why existing tools miss it:**
- Each payment looks normal on its own, so per-transaction scoring passes it.
- Nothing responds automatically.
- Stolen *PayPal accounts* aren't covered the same way as cards.
- Most small shops haven't set up PayPal Fraud Protection Advanced (Idea › Risks).

## 2. Product concept
- **Name:** Checkout Guard. **Tagline:** *The night guard for your PayPal checkout.*
- **Vision:** every small PayPal merchant gets fraud response at the level of a big-company security team. Setup takes five minutes, and real customers feel no extra friction.
- **Target users:**
  - small PayPal merchants (fewer than 50 orders a day) and nonprofits taking donations
  - later: agencies and plugin vendors that look after many stores
- **USP: it responds, not just scores.**
  - It recognises an attack across the whole traffic pattern.
  - It acts within owner-set safety limits through PayPal's own controls: 3-D Secure, refunds.
  - It covers stolen PayPal accounts as well as cards.
  - It tells a bot apart from the owner's own sale.

### How it works: four steps and one decision
1. **Watch.** Every checkout attempt goes through a guard check before the PayPal order is created. PayPal webhooks report every capture, refund and dispute.
2. **Judge** (the decision). A deterministic detector notices the anomaly. Gemini then decides whether it is a **card-testing attack, an account-testing attack, a genuine rush or a PayPal outage**, and gives its reasons.
3. **Act.** A deterministic policy engine applies graduated defences. In order: force 3-D Secure (`SCA_ALWAYS`), pause traffic that looks like the bot, then refund matched attack captures up to a cap.
4. **Explain.** One plain-English alert at the time, then a morning report and an evidence pack the owner can send to PayPal.
- **Then stand down:** the defences relax automatically once the attack stops.

### Three refinements to the original idea [R]
1. **The flash-sale control is the centrepiece.** The demo shows that the guard does *not* fire on a real sale. This answers the judges' first worry, false positives.
2. **Two layers.** A cheap deterministic detector runs all the time. The LLM is called only when an incident opens. A fixed policy engine has the final say on any action.
3. **Measured results.** An evaluation harness reports accuracy, time-to-detect, dollars saved and false blocks.

### 30-second pitch
> "Last night, a bot hit Meera's candle shop 300 times with stolen cards. 80 went through, and each looked like a normal $3 order, so nothing stopped it. She found out at breakfast, along with 80 future disputes. Checkout Guard is a night guard for your PayPal checkout. It watches every payment. AI tells an attack apart from a real sales rush. Within minutes it switches on 3-D Secure, blocks the bot and refunds the test charges before they become disputes. Meera wakes up to one message: 'Attack stopped. $18 refunded. Nothing to do.'"

## 3. Requirements traceability

| # | Requirement | Source | M/O | Planned implementation | Validation |
|---|---|---|---|---|---|
| R1 | Working app on PayPal developer platform via **free sandbox** | Rules › Project requirements | M | Python backend calls Orders v2, Payments (refunds), Webhooks, Transaction Search, Disputes; frontend uses JS SDK v6 | E2E against `api-m.sandbox.paypal.com` |
| R2 | PayPal integration **central** | Rules › Project req.; Judging Stage 1 | M | The product exists to defend a PayPal checkout; every action is a PayPal API call | README table "PayPal APIs used" |
| R3 | **Meaningful AI in the product**, not only in coding | Rules › Project req. + note | M | Gemini verdict, re-assessment, alert, report, evidence pack, AG Studio agent | Eval harness; README "AI tools used" |
| R4 | Working demo; mockups fail | Rules › Submission | M | Hosted on Render + `docker compose up` locally | Fresh-clone test |
| R5 | Documented for judges | Rules › Project req. | M | README + these docs | Peer review |
| R6 | Runs reliably as shown | Rules › Project req. | M | Seeded data, fixed-seed simulator, health checks, reset button | Daily smoke run from Nov 5 |
| R7 | Text description | Rules › Submission | M | Devpost write-up | Checklist |
| R8 | AI tools named + explained | Rules › Submission | M | Model IDs and roles listed | Checklist |
| R9 | Public repo + **OSS license in About** | Rules › Submission | M | GitHub, MIT | Visual check |
| R10 | Video < 3:00, public YouTube, real app, no copyrighted music | Rules › Submission | M | Script in doc 06 | Runtime ≤ 2:50 |
| R11 | Judge access; creds in README | Rules › Testing access | M | Demo URL, judge login, sandbox buyer accounts | Incognito run-through |
| R12 | Free and working **until Dec 15**; tokens refresh; trials don't lapse | Rules › Testing access | M | Token auto-refresh; paid Render Postgres; AG license plan | Reminders Dec 1/8; uptime monitor |
| R13 | English | Rules › Language | M | — | — |
| R14 | **Submitted** (not draft) before Nov 12, 12:00 PM PT | Rules › Checklist | M | Submit Nov 10 | Devpost status |
| R15 | Team eligibility; all registered + invited | Rules › Eligibility, Teams | M | Self-check; Representative named | Checklist |
| R16 | Third-party terms and OSS licenses respected | Rules › Other conditions | M | License inventory; boilerplate reuse confirmed or rewritten | Review |
| R17 | Each claimed sponsor tool used **meaningfully** | Rules › Sponsor conditions | M per prize | AG Studio + agent; Render deploy; APIMatic (dev-time) | Shown in video/README |
| R18 | AG Grid: custom widgets, theming, layout **and** Studio Agent Framework | Rules › Sponsor conditions; Web | M for AG prize | War Room + custom widgets + "Ask the Guard" agent | Video segment |
| R19 | No unlicensed trademarks or music | Rules › Submission | M | "for PayPal merchants" wording; no music or royalty-free only | Review |
| R20 | At most 1 Grand/HM + 1 Sponsor prize | Rules › Multiple prizes | Constraint | Strategy below | — |

**Not stated in either document (not invented):**
- team size
- how APIMatic use is scored
- what "Best Use of Agentic Commerce" means
- whether the AG Studio trial watermarks after expiry
- whether sandbox card declines send webhooks

## 4. Prize & sponsor strategy

**The rules (Rules › Prizes, Multiple prizes):**
- One project wins at most **one Grand Prize or Honorable Mention, plus one Sponsor Prize**.
- We are considered for every sponsor whose tool we use, but can win only one.

### Main prizes
| Prize | Fit | Why |
|---|---|---|
| Grand 1st/2nd/3rd ($12k/$8k/$5k) | High | Strong across all 5 criteria if end-to-end |
| HM Best Use of PayPal + AI ($5k) | **High** | Seven PayPal capabilities + AI that decides and explains |
| HM Most Impactful ($5k) | **High** | Real harm to small merchants, public evidence |
| HM Best Demo Delivery ($5k) | Med-high | "Attack starts → AI explains → defences on" |
| HM Most Creative ($5k) | Medium | — |
| HM Best Use of Agentic Commerce ($5k) | Low-med | Undefined (Web); ours is a defending agent, not a buying one |

### Sponsor matrix
| Sponsor | Eligibility | Integration | Feature | Effort | Proof | Risks | Priority |
|---|---|---|---|---|---|---|---|
| **AG Grid** ($5k/$2k/3×$1k) | Any AG Grid tool. Judges look at "polish (custom widgets, theming, layout tools) and use of the Studio Agent Framework" (Rules). The sponsor page lists an "anomaly-alert agent… flagging suspicious refund spikes or failed payments" (Web) | `ag-studio-react` in the frontend; Studio Agent Framework via `clientToolRunner` → Python `/agent/studio` → Gemini | **Incident War Room** + **"Ask the Guard"** agent that builds widgets on request | M–L | Video + README | 45-day trial expires before Dec 15 unless extended; Python ↔ clientToolRunner format still to be checked | **High: primary** |
| **Render** (credits $1k/$750/$500) | Any Render tool | Web service (FastAPI), Background Worker, Static/Node service (Next.js), paid Postgres; `render.yaml` | Live judge demo; always-on detection | S | Demo URL + blueprint | Free Postgres expires after 30 days; free services spin down (Web) → paid via $50 credits | **High** |
| **APIMatic** (3×$1k + Business) | Any APIMatic tool; perks for those who "used the Context Plugin to build their PayPal integration" (Web) | Context Plugin in Claude Code while writing the **Python** PayPal SDK code | Dev-time only | XS | README "How we built it" + screenshot | Scoring of dev-time use unknown | Optional hedge |
| Bryntum | Any Bryntum tool | — | Weak fit | M | — | Dilutes the UI | Not recommended |
| Channel3 | Must call the API/MCP meaningfully (Rules) | — | No fit | — | — | Forced fit | Not recommended |
| Elastic, Zapier, KERNEL, Postman, Astropods | No prize | — | — | — | — | — | Not recommended |

**Target combination:** a Grand Prize or HM (Best Use of PayPal + AI / Most Impactful) **+ the AG Grid Sponsor Prize**.
