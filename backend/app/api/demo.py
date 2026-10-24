"""Demo controls (judge mode): run labelled simulation scenarios, seed history, reset.

Only mounted when SIMULATOR_ENABLED=true. Everything created here is tagged source=simulator/seed.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import OwnerShop, get_paypal, require_simulator
from app.db.audit import audit
from app.db.models import (
    Action,
    AuditLog,
    CheckoutAttempt,
    DeviceEntry,
    GuardMode,
    GuardState,
    Incident,
    MerchantSettings,
    Report,
    Source,
)
from app.db.session import get_session
from app.events import publish
from app.guard.check import invalidate_cache
from app.paypal.client import PayPalClient
from app.simulator.runner import RUNS, active_run, start_run
from app.simulator.scenarios import OFFLINE_ONLY, SCENARIOS
from app.simulator.seed import seed_history

router = APIRouter(prefix="/demo", tags=["demo"], dependencies=[Depends(require_simulator)])
Session = Annotated[AsyncSession, Depends(get_session)]


class RunIn(BaseModel):
    mode: Literal["live", "offline"] = "offline"
    count: int | None = Field(default=None, ge=1, le=500)
    speed: float = Field(default=1.0, gt=0, le=20)
    seed: int | None = None
    # Announce a sale (a 2-hour window) as part of the run, server-side and only once the run is accepted:
    # a refused run must not leave a sale window behind that softens the policy for the next attack.
    announce_sale: bool = False


SALE_WINDOW = timedelta(hours=2)
SALE_NOTE = "Autumn sale announced on Instagram (simulated)"


@router.get("/scenarios")
def scenarios() -> list[dict]:
    return [{"name": n, "live_supported": n not in OFFLINE_ONLY} for n in SCENARIOS]


@router.post("/run/{scenario}")
async def run(
    scenario: str,
    body: RunIn,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
) -> dict:
    """409 while another run is going: two overlapping scenarios blur into one incident and the demo's
    story (and its numbers) stops making sense."""
    if active := active_run():
        message = f"a {active.scenario} run is still going: stop it or wait for it to finish"
        raise HTTPException(409, {"code": "run_active", "message": message, "run_id": active.id})
    if body.announce_sale:
        if scenario not in SCENARIOS or (body.mode == "live" and scenario in OFFLINE_ONLY):
            raise HTTPException(400, f"cannot run {scenario} ({body.mode})")
        # Only the sale window changes (no read-modify-write of the whole settings: the switches stay as set).
        now = datetime.now(UTC)
        window = {"start": (now - timedelta(minutes=5)).isoformat(), "end": (now + SALE_WINDOW).isoformat()}
        await session.execute(
            update(MerchantSettings)
            .where(MerchantSettings.shop_id == shop_id)
            .values(sale_windows=[window | {"note": SALE_NOTE}])
        )
        audit(session, "owner", "demo.sale_announced", end=window["end"])
        await session.commit()
        invalidate_cache(shop_id)
    try:
        r = start_run(shop_id, scenario, body.mode, paypal, body.count, body.speed, body.seed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": r.id, "scenario": r.scenario, "mode": r.mode, "total": r.total, "simulated": True}


@router.get("/runs")
def runs(_shop: OwnerShop) -> list[dict]:
    return [
        {
            "run_id": r.id,
            "scenario": r.scenario,
            "mode": r.mode,
            "total": r.total,
            "done": r.done,
            "outcomes": dict(r.outcomes),
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "error": r.error,
        }
        for r in sorted(RUNS.values(), key=lambda r: r.started_at, reverse=True)
    ]


@router.post("/runs/{run_id}/stop")
def stop(run_id: str, _shop: OwnerShop) -> dict:
    if run_id not in RUNS:
        raise HTTPException(404, "unknown run")
    RUNS[run_id].cancelled = True
    return {"ok": True}


@router.post("/seed")
async def seed(shop_id: OwnerShop, session: Session, days: int = 28) -> dict:
    n = await seed_history(session, shop_id, days=days)
    return {"seeded_attempts": n, "days": days, "source": "seed (simulated history)"}


@router.post("/reset")
async def reset(shop_id: OwnerShop, session: Session) -> dict:
    """Wipe demo activity (attempts, incidents, actions, reports, device blocks and trust, sale windows)
    and switch the guard back on (kill switch off, autopilot on). Keeps seeded history and webhook events."""
    for r in RUNS.values():
        r.cancelled = True
    incident_ids = list(
        (await session.execute(select(Incident.id).where(Incident.shop_id == shop_id))).scalars()
    )
    if incident_ids:
        await session.execute(delete(Report).where(Report.incident_id.in_(incident_ids)))
        await session.execute(delete(Action).where(Action.incident_id.in_(incident_ids)))
        await session.execute(delete(AuditLog).where(AuditLog.incident_id.in_(incident_ids)))
        await session.execute(delete(Incident).where(Incident.id.in_(incident_ids)))
    await session.execute(
        delete(CheckoutAttempt).where(
            CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.source != Source.SEED
        )
    )
    await session.execute(
        update(GuardState)
        .where(GuardState.shop_id == shop_id)
        .values(mode=GuardMode.NORMAL, held_skus=[], set_by_incident=None)
    )
    await session.execute(delete(DeviceEntry).where(DeviceEntry.shop_id == shop_id))
    await session.execute(
        update(MerchantSettings)
        .where(MerchantSettings.shop_id == shop_id)
        .values(sale_windows=[], kill_switch=False, autopilot=True)
    )
    audit(session, "owner", "demo.reset")
    await publish(session, "demo.reset", {})
    await session.commit()
    invalidate_cache(shop_id)
    return {"ok": True}
