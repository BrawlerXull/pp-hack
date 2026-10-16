"""Owner dashboard API: live status, attempts, incidents, reports, approvals and the event stream."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.actions.executor import Executor, action_event
from app.api.deps import OwnerShop, get_broadcaster, get_paypal
from app.config import Settings, get_settings
from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    Outcome,
    Report,
)
from app.db.session import get_session
from app.events import Broadcaster, publish
from app.guard.check import invalidate_cache
from app.incidents.service import IncidentService, incident_event, open_incident
from app.paypal.client import PayPalClient
from app.shop.service import attempt_event

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def _incident_or_404(session: AsyncSession, shop_id: str, incident_id: str) -> Incident:
    inc = await session.get(Incident, incident_id)
    if inc is None or inc.shop_id != shop_id:
        raise HTTPException(404, "incident not found")
    return inc


@router.get("/summary")
async def summary(shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=24)
    counts = dict(
        (
            await session.execute(
                select(CheckoutAttempt.outcome, func.count())
                .where(
                    CheckoutAttempt.shop_id == shop_id,
                    CheckoutAttempt.ts > since,
                    CheckoutAttempt.source != "seed",
                )
                .group_by(CheckoutAttempt.outcome)
            )
        ).all()
    )
    refunded = (
        await session.execute(
            select(Action.params["amount"].as_string()).where(
                Action.type == ActionType.REFUND,
                Action.status == ActionStatus.EXECUTED,
                Action.created_at > since,
            )
        )
    ).scalars()
    gs = await session.get(GuardState, shop_id)
    inc = await open_incident(session, shop_id)
    if inc is None:
        status = "recovering" if gs and gs.mode != GuardMode.NORMAL else "quiet"
    else:
        status = {
            "card_testing": "under_attack",
            "account_testing": "under_attack",
            "genuine_rush": "busy_genuine_rush",
            "paypal_outage": "paypal_issue",
        }.get(inc.classification or "", "investigating")
    stopped = sum(counts.get(o, 0) for o in (Outcome.BLOCKED, Outcome.CHALLENGED, Outcome.SCA_REQUIRED))
    return {
        "status": status,
        "open_incident": incident_event(inc) if inc else None,
        "guard_state": {
            "mode": gs.mode if gs else GuardMode.NORMAL,
            "blocked_devices": len(gs.blocked_fingerprints or []) if gs else 0,
            "held_skus": gs.held_skus if gs else [],
        },
        "last_24h": {
            "attempts": sum(counts.values()),
            "captured": counts.get(Outcome.CAPTURED, 0),
            "declined": counts.get(Outcome.DECLINED, 0),
            "stopped_by_guard": stopped,
            "refunded": counts.get(Outcome.REFUNDED, 0),
            "refunded_amount": f"{sum((Decimal(a) for a in refunded if a), Decimal('0')):.2f}",
        },
    }


@router.get("/attempts")
async def attempts(
    shop_id: OwnerShop,
    session: Session,
    minutes: Annotated[int, Query(ge=1, le=60 * 24 * 30)] = 120,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    include_seed: bool = False,
) -> list[dict[str, Any]]:
    q = select(CheckoutAttempt).where(
        CheckoutAttempt.shop_id == shop_id,
        CheckoutAttempt.ts > datetime.now(UTC) - timedelta(minutes=minutes),
    )
    if not include_seed:
        q = q.where(CheckoutAttempt.source != "seed")
    rows = (await session.execute(q.order_by(CheckoutAttempt.ts.desc()).limit(limit))).scalars()
    return [
        attempt_event(a) | {"capture_id": a.capture_id, "refund_id": a.refund_id, "card_last4": a.card_last4}
        for a in rows
    ]


@router.get("/timeseries")
async def timeseries(
    shop_id: OwnerShop, session: Session, minutes: Annotated[int, Query(ge=5, le=1440)] = 60
) -> list[dict[str, Any]]:
    """Per-minute counts by outcome, for the War Room charts."""
    bucket = func.date_trunc("minute", CheckoutAttempt.ts)
    rows = (
        await session.execute(
            select(bucket, CheckoutAttempt.outcome, func.count())
            .where(
                CheckoutAttempt.shop_id == shop_id,
                CheckoutAttempt.ts > datetime.now(UTC) - timedelta(minutes=minutes),
                CheckoutAttempt.source != "seed",
            )
            .group_by(bucket, CheckoutAttempt.outcome)
            .order_by(bucket)
        )
    ).all()
    series: dict[datetime, dict[str, Any]] = {}
    for minute, outcome, n in rows:
        series.setdefault(minute, {"minute": minute})[outcome] = n
    return list(series.values())


@router.get("/incidents")
async def incidents(shop_id: OwnerShop, session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Incident)
            .where(Incident.shop_id == shop_id)
            .order_by(Incident.opened_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [incident_event(i) for i in rows]


@router.get("/incidents/{incident_id}")
async def incident_detail(incident_id: str, shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    inc = await _incident_or_404(session, shop_id, incident_id)
    actions = (
        await session.execute(select(Action).where(Action.incident_id == inc.id).order_by(Action.created_at))
    ).scalars()
    report = await session.get(Report, inc.id)
    timeline = (
        await session.execute(select(AuditLog).where(AuditLog.incident_id == inc.id).order_by(AuditLog.id))
    ).scalars()
    return incident_event(inc) | {
        "verdict": inc.verdict,
        "features": inc.features,
        "signature": inc.signature,
        "model_id": inc.model_id,
        "prompt_version": inc.prompt_version,
        "actions": [action_event(a) for a in actions],
        "report": {
            "alert_text": report.alert_text,
            "report_md": report.report_md,
            "evidence_md": report.evidence_md,
            "model_id": report.model_id,
            "generated_at": report.generated_at,
        }
        if report
        else None,
        "timeline": [{"ts": t.ts, "actor": t.actor, "event": t.event, "data": t.data} for t in timeline],
    }


@router.get("/incidents/{incident_id}/report.md", response_class=PlainTextResponse)
async def report_md(incident_id: str, shop_id: OwnerShop, session: Session) -> str:
    await _incident_or_404(session, shop_id, incident_id)
    report = await session.get(Report, incident_id)
    return report.report_md if report and report.report_md else ""


@router.get("/incidents/{incident_id}/evidence.md", response_class=PlainTextResponse)
async def evidence_md(incident_id: str, shop_id: OwnerShop, session: Session) -> str:
    await _incident_or_404(session, shop_id, incident_id)
    report = await session.get(Report, incident_id)
    return report.evidence_md if report and report.evidence_md else ""


async def _pending_action(session: AsyncSession, inc: Incident, action_id: str) -> Action:
    action = await session.get(Action, action_id)
    if action is None or action.incident_id != inc.id:
        raise HTTPException(404, "action not found")
    if action.status != ActionStatus.AWAITING_APPROVAL:
        raise HTTPException(409, f"action is {action.status}, not awaiting approval")
    return action


@router.post("/incidents/{incident_id}/actions/{action_id}/approve")
async def approve(
    incident_id: str,
    action_id: str,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
) -> dict[str, Any]:
    inc = await _incident_or_404(session, shop_id, incident_id)
    action = await Executor(session, paypal).approve(inc, await _pending_action(session, inc, action_id))
    await session.commit()
    return action_event(action)


@router.post("/incidents/{incident_id}/actions/{action_id}/reject")
async def reject(incident_id: str, action_id: str, shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    inc = await _incident_or_404(session, shop_id, incident_id)
    action = await Executor(session, None).reject(inc, await _pending_action(session, inc, action_id))
    await session.commit()
    return action_event(action)


@router.post("/incidents/{incident_id}/close")
async def close_incident(
    incident_id: str,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Owner stands the guard down manually (restores the checkout to normal)."""
    inc = await _incident_or_404(session, shop_id, incident_id)
    if inc.status == IncidentStatus.CLOSED:
        raise HTTPException(409, "already closed")
    await IncidentService(session, paypal, timedelta(seconds=settings.reassess_interval_s)).close(
        inc, "owner_closed"
    )
    audit(session, "owner", "incident.closed_by_owner", inc.id)
    await session.commit()
    return incident_event(inc)


@router.post("/incidents/{incident_id}/report/regenerate")
async def regenerate_report(
    incident_id: str,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    inc = await _incident_or_404(session, shop_id, incident_id)
    report = await IncidentService(
        session, paypal, timedelta(seconds=settings.reassess_interval_s)
    ).write_report(inc)
    await session.commit()
    return {"alert_text": report.alert_text, "model_id": report.model_id}


@router.post("/guard/reset")
async def reset_guard(shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    """Clear all defences (unblock devices, release SKUs, SCA off)."""
    gs = await session.get(GuardState, shop_id, with_for_update=True)
    assert gs is not None
    gs.mode, gs.blocked_fingerprints, gs.held_skus, gs.set_by_incident = GuardMode.NORMAL, [], [], None
    gs.updated_at = datetime.now(UTC)
    audit(session, "owner", "guard.reset")
    await publish(session, "guard.reset", {})
    await session.commit()
    invalidate_cache(shop_id)
    return {"ok": True}


@router.get("/stream")
async def stream(
    request: Request, _shop: OwnerShop, broadcaster: Annotated[Broadcaster, Depends(get_broadcaster)]
) -> EventSourceResponse:
    """Server-sent events: attempt, incident.opened/updated/closed, action, alert, guard.reset."""

    async def events():
        async with broadcaster.subscribe() as q:
            yield {"event": "hello", "data": "{}"}
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "message", "data": payload}

    return EventSourceResponse(events())
