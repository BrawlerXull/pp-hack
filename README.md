# Checkout Guard

**The night guard for your PayPal checkout.** Checkout Guard watches a small shop's PayPal checkout. It uses AI to tell a card-testing bot attack apart from a real sales rush or a PayPal outage. When it sees an attack, it responds within minutes: it forces 3-D Secure, blocks the bot, and refunds the test charges before they turn into disputes. Then it explains what happened in plain English.

Built for the PayPal AI Hackathon 2026. Everything runs in the **PayPal sandbox**. The attacker is a script, and the "stolen" cards are sandbox test cards.

> Status: working end to end. Demo shop with real PayPal (JS SDK v6) checkout, owner dashboard (AG Grid + AG Charts, live updates), and Python backend (guard, detection, Gemini verdicts, policy, refunds, reports, webhooks). See [docs/](docs/README.md) for the plan.

## Layout

| Folder | What |
|---|---|
| [backend/](backend/) | Python 3.12 + FastAPI: PayPal integration, guard, detection, AI, policy, webhooks |
| [frontend/](frontend/) | Next.js: demo shop UI and owner dashboard. UI only; `/api/*` is proxied to the backend |
| [docs/](docs/) | Product, architecture, backend and AI design, roadmap, demo plan |

## Quick start

```bash
cp .env.example .env            # PayPal sandbox app credentials + GEMINI_API_KEY
createdb checkout_guard && createdb checkout_guard_test
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000      # API  (docs at http://localhost:8000/docs)
uv run python -m app.worker.main             # worker (separate terminal)
cd ../frontend && pnpm install && pnpm dev   # http://localhost:3000
```

| URL | What |
|---|---|
| http://localhost:3000 | **Checkout Guard** product page: what it does and how, for judges |
| http://localhost:3000/shop | **Wick & Wax** demo shop: PayPal checkout, protected by the guard |
| http://localhost:3000/guard | **Checkout Guard** dashboard (password `guard-demo`): War Room, incidents, settings, demo lab |
| http://localhost:8000/docs | Backend API docs |

In the dashboard: **Demo lab → Seed 28 days of normal history** once, then use **Card-testing attack** / **Flash sale** in the simulation bar at the bottom of every dashboard page.

PayPal webhooks, enabling card payments in the sandbox, and demo scenarios are covered in [backend/README.md](backend/README.md).

## Development

```bash
cd backend && uv run pytest && uv run ruff check .
cd frontend && pnpm dev              # http://localhost:3000, proxies /api/* to BACKEND_URL (default :8000)
```

## License

MIT
