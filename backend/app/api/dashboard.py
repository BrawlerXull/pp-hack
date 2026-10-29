"""Owner dashboard API: live status, attempts, incidents, reports, approvals and the event stream."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app import visitor
from app.actions.executor import DisputedCapture, Executor, action_event
from app.ai import gemini
from app.api.deps import OwnerShop, get_broadcaster, get_paypal
from app.config import Settings, get_settings
from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    DeviceListKind,
    DisputeCase,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Method,
    Outcome,
    Report,
    Source,
    SystemStatus,
)
from app.db.session import get_session
from app.detection import normal
from app.disputes.respond import case_event, respond
from app.events import Broadcaster, publish
from app.guard import devices
from app.guard.check import invalidate_cache
from app.incidents.service import IncidentService, active_sale_window, incident_event, open_incident
from app.paypal.client import PayPalClient
from app.shop.service import attempt_event
from app.worker import heartbeat

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
Session = Annotated[AsyncSession, Depends(get_session)]


async def _incident_or_404(session: AsyncSession, shop_id: str, incident_id: str) -> Incident:
    inc = await session.get(Incident, incident_id)
    if inc is None or inc.shop_id != shop_id:
        raise HTTPException(404, "incident not found")
    return inc


CARD_EVIDENCE_WINDOW = timedelta(days=7)


async def _card_capture_seen(session: AsyncSession, shop_id: str, now: datetime) -> bool:
    """A real sandbox card payment was captured lately (simulated rows have paypal_live=False)."""
    q = select(
        exists().where(
            CheckoutAttempt.shop_id == shop_id,
            CheckoutAttempt.paypal_live.is_(True),
            CheckoutAttempt.method == Method.CARD,
            CheckoutAttempt.capture_id.is_not(None),
            CheckoutAttempt.ts > now - CARD_EVIDENCE_WINDOW,
        )
    )
    return bool((await session.execute(q)).scalar())


@router.get("/summary")
async def summary(
    shop_id: OwnerShop, session: Session, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
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
    ms = await session.get(MerchantSettings, shop_id)
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
    stopped = sum(
        counts.get(o, 0)
        for o in (Outcome.BLOCKED, Outcome.CHALLENGED, Outcome.SCA_REQUIRED, Outcome.SCA_FAILED)
    )
    now = datetime.now(UTC)
    ai = await gemini.status()
    sale = active_sale_window(ms, now) if ms else None
    return {
        "status": status,
        "open_incident": incident_event(inc) if inc else None,
        # Gemini circuit-breaker state (shared with the worker), so "AI unavailable" has a visible cause.
        "ai": ai,
        "system": heartbeat.system_block(
            await session.get(SystemStatus, heartbeat.WORKER, populate_existing=True),
            ai,
            settings,
            now,
            card_capture_seen=await _card_capture_seen(session, shop_id, now),
            assessing=inc is not None and inc.assessing_since is not None,
        ),
        "switches": {
            "kill_switch": bool(ms and ms.kill_switch),
            "autopilot": bool(ms and ms.autopilot),
        },
        "sale_window": {"end": sale.get("end"), "note": sale.get("note") or ""} if sale else None,
        "guard_state": {
            "mode": gs.mode if gs else GuardMode.NORMAL,
            "blocked_devices": await devices.count_blocked(session, shop_id),
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
        "demo_activity": await demo_activity(session, now) if settings.simulator_enabled else None,
    }


@router.get("/disputes")
async def disputes(shop_id: OwnerShop, session: Session) -> list[dict[str, Any]]:
    """Dispute cases (app/disputes/advisor.py): waiting for the owner first, then the latest answered."""
    rows = (
        await session.execute(
            select(DisputeCase)
            .where(DisputeCase.shop_id == shop_id)
            .order_by((DisputeCase.status == "open").desc(), DisputeCase.created_at.desc())
            .limit(50)
        )
    ).scalars()
    return [case_event(c) for c in rows]


class DisputeAnswerIn(BaseModel):
    note: str | None = None  # the owner's edit of the drafted note


@router.post("/disputes/{dispute_id}/{decision}")
async def answer_dispute(
    dispute_id: str,
    decision: Literal["accept", "contest", "dismiss"],
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    body: DisputeAnswerIn | None = None,
) -> dict[str, Any]:
    """The owner's answer, sent to PayPal's Disputes API (dismiss: answered in PayPal instead)."""
    case = await session.get(DisputeCase, dispute_id, with_for_update=True, populate_existing=True)
    if case is None or case.shop_id != shop_id:
        raise HTTPException(404, "dispute not found")
    if case.status != "open":
        raise HTTPException(409, f"dispute is {case.status}")
    await respond(session, paypal, case, decision, (body.note or "").strip() if body else None)
    await session.commit()
    return case_event(case)


@router.get("/network-check")
def network_check(
    request: Request, _shop: OwnerShop, settings: Annotated[Settings, Depends(get_settings)]
) -> dict:
    """How this deploy derives the client IP (TRUSTED_PROXY_HOPS): compare client_ip with your own address.
    Owner-only: it echoes request headers."""
    from app.api.shop import client_ip

    return {
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "peer": request.client.host if request.client else None,
        "trusted_proxy_hops": settings.trusted_proxy_hops,
        "client_ip": client_ip(request),
    }


async def seen_via(session: AsyncSession, inc: Incident) -> dict[str, int]:
    """Attempts in the incident's window that went through a guard check vs. ones only PayPal's webhooks
    showed (observer mode): blocking, 3-D Secure and holds act only on the first kind."""
    rows = (
        await session.execute(
            select(CheckoutAttempt.source, func.count())
            .where(
                CheckoutAttempt.shop_id == inc.shop_id,
                CheckoutAttempt.ts >= inc.opened_at - timedelta(minutes=15),
                CheckoutAttempt.ts <= (inc.closed_at or datetime.now(UTC)),
                CheckoutAttempt.source != Source.SEED,
            )
            .group_by(CheckoutAttempt.source)
        )
    ).all()
    observed = sum(n for source, n in rows if source == Source.OBSERVED)
    return {"guarded": sum(n for _, n in rows) - observed, "observed": observed}


DEMO_ACTIVITY_WINDOW = timedelta(minutes=30)


async def demo_activity(session: AsyncSession, now: datetime) -> dict[str, Any] | None:
    """The shared demo's latest change, when this visitor did not make it: another visitor's action (everyone
    logs in with the same password) or the janitor's idle stand-down. None when the latest is this visitor's
    own, or there is none in 30 min."""
    me = visitor.current()
    rows = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.ts > now - DEMO_ACTIVITY_WINDOW,
                (AuditLog.actor == "owner") | (AuditLog.event == "demo.idle_restore"),
            )
            .order_by(AuditLog.ts.desc())
            .limit(50)
        )
    ).scalars()
    for row in rows:
        by = (row.data or {}).get("visitor")
        if row.event == "demo.idle_restore":
            return {"kind": "idle_restore", "event": row.event, "at": row.ts, "data": row.data}
        if by and by == me:
            return None  # you acted since: whatever came before is already on your screen
        if by:
            data = {k: v for k, v in (row.data or {}).items() if k in ("scenario", "mode")}
            return {"kind": "other_visitor", "event": row.event, "at": row.ts, "data": data}
    return None


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
    return [attempt_event(a) | {"capture_id": a.capture_id, "refund_id": a.refund_id} for a in rows]


BUCKET_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
MAX_BUCKETS = 1440


@router.get("/timeseries")
async def timeseries(
    shop_id: OwnerShop,
    session: Session,
    minutes: Annotated[int, Query(ge=5, le=1440)] = 60,
    bucket_s: Annotated[int, Query(ge=5, le=3600)] = 60,
) -> list[dict[str, Any]]:
    """Counts by outcome per bucket (default a minute; the War Room uses 10 s to show an attack's first
    seconds). Buckets start on multiples of bucket_s since a fixed UTC origin, so the frontend can rebuild
    the empty ones. Each point carries its bucket start as `t` (and `minute`, the older name)."""
    if minutes * 60 // bucket_s > MAX_BUCKETS:
        raise HTTPException(422, f"too many buckets: at most {MAX_BUCKETS} per request")
    bucket = func.date_bin(timedelta(seconds=bucket_s), CheckoutAttempt.ts, BUCKET_ORIGIN)
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
    for t, outcome, n in rows:
        series.setdefault(t, {"t": t, "minute": t})[outcome] = n
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


@router.get("/alerts")
async def alerts(
    shop_id: OwnerShop,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    incident_id: Annotated[str | None, Query(max_length=16)] = None,
) -> list[dict[str, Any]]:
    """Alert history, newest first: every alert the owner was sent (audit `alert.sent`), with its text.
    incident_id narrows it to one incident (the incident page's phone)."""
    q = (
        select(AuditLog)
        .join(Incident, Incident.id == AuditLog.incident_id)
        .where(AuditLog.event == "alert.sent", Incident.shop_id == shop_id)
    )
    if incident_id:
        q = q.where(AuditLog.incident_id == incident_id)
    rows = (await session.execute(q.order_by(AuditLog.id.desc()).limit(limit))).scalars()
    return [
        {
            "id": r.id,
            "ts": r.ts,
            "incident_id": r.incident_id,
            "text": r.data.get("text"),
            "model_id": r.data.get("model_id"),
        }
        for r in rows
    ]


DETECTION_LOOKBACK = timedelta(minutes=5)  # the velocity trigger's window: a burst starts within it


async def detection_delay_s(session: AsyncSession, inc: Incident) -> int | None:
    """Seconds from the burst's first attempt to the incident opening. The first attempt is the earliest
    one from a device flagged in the signature within DETECTION_LOOKBACK before opening; with no flagged
    device (a rush, an outage) the earliest attempt in that window. Seeded history never counts."""
    sig = inc.signature or {}
    flagged = sorted(set(sig.get("fingerprints") or []) | set(sig.get("weak_fingerprints") or []))
    window = (
        CheckoutAttempt.shop_id == inc.shop_id,
        CheckoutAttempt.source != Source.SEED,
        CheckoutAttempt.ts >= inc.opened_at - DETECTION_LOOKBACK,
        CheckoutAttempt.ts <= inc.opened_at,
    )
    first = None
    if flagged:
        first = (
            await session.execute(
                select(func.min(CheckoutAttempt.ts)).where(
                    *window, CheckoutAttempt.fingerprint_hash.in_(flagged)
                )
            )
        ).scalar()
    if first is None:
        first = (await session.execute(select(func.min(CheckoutAttempt.ts)).where(*window))).scalar()
    return None if first is None else int((inc.opened_at - first).total_seconds())


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
        "verdict_at": inc.verdict_at,
        "ai_refresh_failures": inc.ai_refresh_failures,
        # ×N vs normal for each evidence item, from our stored features and baseline (never the AI's).
        # Uses the features the verdict was checked against: `features` moves on with every re-check.
        "evidence_vs_normal": normal.evidence_vs_normal(inc.verdict, inc.verdict_features or {}),
        "detected_in_s": await detection_delay_s(session, inc),
        # How the incident's payments were seen: through a guard check (shop, API) or only PayPal's webhooks.
        "seen_via": await seen_via(session, inc),
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
    """Row-locked until commit, so a second approve/reject (double click, two tabs) waits for the first
    and then sees it is no longer awaiting approval (409) instead of refunding or flipping it again."""
    action = await session.get(Action, action_id, with_for_update=True, populate_existing=True)
    if action is None or action.incident_id != inc.id:
        raise HTTPException(404, "action not found")
    if action.status != ActionStatus.AWAITING_APPROVAL:
        raise HTTPException(409, f"action is {action.status}, not awaiting approval")
    return action


class ApproveIn(BaseModel):
    # The owner read the double-loss warning and is settling the dispute with this refund on purpose.
    override_dispute: bool = False


@router.post("/incidents/{incident_id}/actions/{action_id}/approve")
async def approve(
    incident_id: str,
    action_id: str,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    body: ApproveIn | None = None,
) -> dict[str, Any]:
    """409 for a disputed capture unless override_dispute is sent: see executor.DISPUTE_DOUBLE_LOSS."""
    inc = await _incident_or_404(session, shop_id, incident_id)
    pending = await _pending_action(session, inc, action_id)
    try:
        action = await Executor(session, paypal).approve(
            inc, pending, override_dispute=bool(body and body.override_dispute)
        )
    except DisputedCapture as exc:
        await session.rollback()
        raise HTTPException(
            409, {"code": "disputed", "message": str(exc), "dispute_status": exc.dispute_status}
        ) from exc
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
    # Locked and re-read: the detector may be closing it too, or applying a verdict that just came back.
    await session.refresh(inc, with_for_update=True)
    if inc.status == IncidentStatus.CLOSED:
        raise HTTPException(409, "already closed")
    audit(session, "owner", "incident.closed_by_owner", inc.id)
    # close() commits the restored checkout before writing the report, so the request never holds the
    # GuardState lock while Gemini drafts.
    await IncidentService(session, paypal, timedelta(seconds=settings.reassess_interval_s)).close(
        inc, "owner_closed"
    )
    return incident_event(inc)


REGENERATE_MIN_INTERVAL = timedelta(seconds=60)  # each regenerate is a Gemini call on the shared quota


async def claim_regenerate_slot(session: AsyncSession, incident_id: str) -> bool:
    """Atomic check-and-set of Report.generated_at before Gemini is called, so two concurrent regenerates
    (double click, two tabs) cannot both pass the limit: the second one's UPDATE waits for the first's
    row lock, then sees the fresh timestamp. No report yet means the first one is still being written."""
    res = await session.execute(
        update(Report)
        .where(
            Report.incident_id == incident_id,
            Report.generated_at < func.now() - REGENERATE_MIN_INTERVAL,
        )
        .values(generated_at=func.now())
        .returning(Report.incident_id)
        .execution_options(synchronize_session=False)
    )
    return res.scalar_one_or_none() is not None


@router.post("/incidents/{incident_id}/report/regenerate")
async def regenerate_report(
    incident_id: str,
    shop_id: OwnerShop,
    session: Session,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    inc = await _incident_or_404(session, shop_id, incident_id)
    if not await claim_regenerate_slot(session, inc.id):
        current = await session.get(Report, inc.id, populate_existing=True)
        if current is None:
            raise HTTPException(409, "the first report is still being written")
        wait = current.generated_at + REGENERATE_MIN_INTERVAL - datetime.now(UTC)
        seconds = max(int(wait.total_seconds()) + 1, 1)
        raise HTTPException(
            429,
            f"report was written moments ago; try again in {seconds} s",
            headers={"Retry-After": str(seconds)},
        )
    await session.commit()
    report = await IncidentService(
        session, paypal, timedelta(seconds=settings.reassess_interval_s)
    ).write_report(inc, notify_owner=False)  # the owner asked: no push, not a new alert
    if report is None:
        raise HTTPException(409, "the incident was closed meanwhile; its final report is being written")
    return {"alert_text": report.alert_text, "model_id": report.model_id}


@router.post("/guard/reset")
async def reset_guard(shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    """Clear all defences (unblock devices, release SKUs, SCA off). Trusted devices stay trusted."""
    gs = await session.get(GuardState, shop_id, with_for_update=True)
    assert gs is not None
    gs.mode, gs.held_skus, gs.set_by_incident = GuardMode.NORMAL, [], None
    gs.updated_at = datetime.now(UTC)
    await devices.release_all(session, shop_id, gs.updated_at, (DeviceListKind.BLOCK,))
    audit(session, "owner", "guard.reset")
    await publish(session, "guard.reset", {})
    await session.commit()
    invalidate_cache(shop_id)
    return {"ok": True}


@router.get("/devices")
async def list_devices(shop_id: OwnerShop, session: Session) -> dict[str, list[dict[str, Any]]]:
    """Blocked and owner-trusted devices that are in force, with why, which incident, and until when."""
    rows = await devices.listing(session, shop_id)
    out: dict[str, list[dict[str, Any]]] = {"blocked": [], "trusted": []}
    for d in rows:
        key = "blocked" if d.kind == DeviceListKind.BLOCK else "trusted"
        out[key].append(
            {
                "fingerprint": d.fingerprint,
                "reason": d.reason,
                "incident_id": d.incident_id,
                "created_at": d.created_at,
                "expires_at": d.expires_at,
            }
        )
    return out


@router.post("/devices/{fingerprint}/unblock")
async def unblock_device(fingerprint: str, shop_id: OwnerShop, session: Session) -> dict[str, Any]:
    """Owner vouches for a device: its block is released and it is trusted, so the next enforcement pass
    neither blocks it again nor refunds its payments (it is left out of the signature and refund matching).
    Under the GuardState lock, like every other write to the device lists."""
    if len(fingerprint) > 64:
        raise HTTPException(422, "not a device fingerprint")
    gs = await session.get(GuardState, shop_id, with_for_update=True)
    assert gs is not None
    now = datetime.now(UTC)
    was_blocked = await devices.trust(session, shop_id, fingerprint, now, reason="unblocked by the owner")
    audit(session, "owner", "device.unblocked", fingerprint=fingerprint, was_blocked=was_blocked)
    await publish(session, "device.unblocked", {"fingerprint": fingerprint})
    await session.commit()
    invalidate_cache(shop_id)
    return {"fingerprint": fingerprint, "was_blocked": was_blocked, "trusted": True}


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
