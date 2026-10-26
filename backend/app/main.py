from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import breaker, gemini
from app.api import auth, dashboard, demo, guard, settings, shop, studio, webhooks
from app.api.deps import get_paypal
from app.config import get_settings
from app.db.bootstrap import ensure_shop
from app.db.session import get_session, get_sessionmaker
from app.events import Broadcaster
from app.log_config import configure_logging
from app.paypal.client import PayPalClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    cfg = get_settings()
    app.state.paypal = PayPalClient(cfg)
    app.state.broadcaster = Broadcaster()
    async with get_sessionmaker()() as s:
        await ensure_shop(s, cfg.shop_id)
    await breaker.reset_until_restart()
    await app.state.broadcaster.start()
    yield
    await app.state.broadcaster.stop()
    await app.state.paypal.aclose()


app = FastAPI(
    title="Checkout Guard API",
    version="0.2.0",
    description="Detects card-testing attacks on a PayPal checkout and responds within safety limits.",
    lifespan=lifespan,
)
# The frontend normally reaches us through the Next.js /api proxy (same origin); CORS is for local tools.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    auth.router,
    shop.router,
    guard.router,
    webhooks.router,
    dashboard.router,
    settings.router,
    studio.router,
):
    app.include_router(router)
if get_settings().simulator_enabled:
    app.include_router(demo.router)


@app.get("/livez", tags=["ops"])
async def livez(session: Annotated[AsyncSession, Depends(get_session)], response: Response) -> dict[str, str]:
    """Liveness for the platform (Render healthCheckPath, compose healthcheck): the process answers and the
    database is reachable. Deliberately not /healthz: a Gemini quota or a PayPal sandbox outage marks
    /healthz degraded (503), and a platform that restarts or refuses to deploy on that would take down
    a guard that is meant to keep running on its rules fallback."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = 503
        return {"status": "down", "db": f"error: {exc.__class__.__name__}"}
    return {"status": "ok"}


@app.get("/healthz", tags=["ops"])
async def healthz(
    request: Request,
    response: Response,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Readiness of everything the demo needs; 503 when degraded, so an uptime check (the daily GitHub
    Action) can alert on the status code alone."""
    checks = {"status": "ok"}
    try:
        await session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"], checks["status"] = f"error: {exc.__class__.__name__}", "degraded"
    try:
        await paypal.access_token()
        checks["paypal"] = "ok"
    except Exception as exc:
        checks["paypal"], checks["status"] = f"error: {exc.__class__.__name__}", "degraded"
    # Read from the shared breaker table, so this reflects what the worker's calls have seen too. Coarse on
    # purpose: /healthz is public (ngrok), so the model chain and cooldown timings stay on the owner-only
    # /dashboard/summary rather than telling a visitor when the guard is on its rules fallback.
    ai = await gemini.status()
    checks["ai"] = {"ok": "configured", "not_configured": "missing GEMINI_API_KEY"}.get(
        ai["state"], ai["state"]
    )
    if ai["state"] in ("cooling_down", "key_rejected"):
        checks["status"] = "degraded"
    # The dashboard's live stream (LISTEN connection). "not_started" only when no lifespan ran (tests).
    broadcaster: Broadcaster | None = getattr(request.app.state, "broadcaster", None)
    checks["stream"] = broadcaster.state if broadcaster else "not_started"
    if checks["stream"] == "reconnecting":
        checks["status"] = "degraded"
    if checks["status"] != "ok":
        response.status_code = 503
    return checks
