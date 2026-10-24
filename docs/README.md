# Checkout Guard: Planning Docs

*The night guard for your PayPal checkout.* Entry for the PayPal AI Hackathon 2026. Submissions close **Nov 12, 2026, 12:00 PM PT**, and the demo must keep working until **Dec 15, 2026**.

These are the planning docs, written before we built Checkout Guard; the code has since been built and is now the source of truth. For what exists today (click path, real vs simulated, APIs, AI, policy, run and deploy) read the [top-level README](../README.md) and [backend/README.md](../backend/README.md). Where a plan below differs from the code, the code wins; [02-architecture.md](02-architecture.md) marks what is **built** and what is **planned**.

The docs draw on two input documents: *Checkout Guard: Idea in Simple Words* ("Idea") and *PayPal AI Hackathon: Rules & Requirements* ("Rules"). Facts we checked on PayPal, Devpost and sponsor sites on Oct 11, 2026 are marked "(Web)".

| # | Doc | What's in it |
|---|---|---|
| 1 | [01-product-and-strategy.md](01-product-and-strategy.md) | Problem, product concept, pitch, requirements traceability, prize and sponsor strategy |
| 2 | [02-architecture.md](02-architecture.md) | System architecture (**Python FastAPI backend + Next.js frontend**), diagrams, repo layout, deployment, security |
| 3 | [03-backend-design.md](03-backend-design.md) | Python backend modules, API contract, data model, worker, PayPal integration details |
| 4 | [04-ai-design.md](04-ai-design.md) | Detector, Gemini analyst, policy engine, reports, AG Studio agent, evaluation |
| 5 | [05-roadmap.md](05-roadmap.md) | Phases, dates, owners, definitions of done, fallbacks |
| 6 | [06-demo-and-submission.md](06-demo-and-submission.md) | Demo video script, backup plan, pitch deck, judge Q&A, risk register, compliance checklist |
| 7 | [sandbox-findings.md](sandbox-findings.md) | P1 sandbox spike results + live AI evaluation |

**Labels:** **[F]** confirmed fact · **[A]** assumption · **[R]** recommendation.

## Decisions so far
1. **Separate Python backend.** FastAPI handles all PayPal calls, detection, AI, policy, actions and webhooks. Next.js is **frontend only**: the demo shop's pages and the owner dashboard.
2. **Our own demo shop**, not WooCommerce (that's a stretch goal).
3. **AI:** Google Gemini (`gemini-3.5-flash`, with `gemini-2.5-flash` and `gemini-flash-lite-latest` as fallbacks), using JSON-schema structured output.
4. **Sponsor prizes:**
   - **AG Grid** is the main target: AG Studio dashboard plus Studio Agent Framework.
   - **Render** for hosting.
   - **APIMatic** Context Plugin, used while coding.
5. **Everything runs in the PayPal sandbox.** The attacker is a labelled simulation.

## Open questions (owner → due)
- AG Studio license valid through Dec 15, and what an unlicensed build looks like → ask on Discord (D, Oct 12)
- Reuse terms for `paypaldev/hackathon-paypal-ag-grid-boilerplate`, which has no license file (Web) → Discord (D, Oct 12)
- Does a declined sandbox card send a webhook? → P1 spike (A, Oct 15)
- Message format between AG Studio's `clientToolRunner` and a Python server → P1 spike (C+B, Oct 15)
- Team size and roles → team (today)

## Note on the original scaffold
The first Next.js scaffold now lives in `frontend/`. The TypeScript spike and webhook route were rewritten in Python under `backend/` (`scripts/spike.py`, `app/api/webhooks.py`) and deleted.
