"""AG Studio (Investigate page): the dashboard's data tables and the Studio Agent Framework's LLM turns.

GET /studio/data returns three related tables (checkout attempts, incidents, actions) for AG Studio's
in-browser data engine. Each attempt carries the incident whose window it fell in, so a widget that selects an
incident cross-filters the attempts. POST /studio/agent/turn runs one LLM turn on Gemini
(app/ai/studio_agent.py).
Both are owner-only and read-only.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gemini, studio_agent
from app.api.deps import OwnerShop
from app.config import get_settings
from app.db.models import Action, ActionType, CheckoutAttempt, Incident
from app.db.session import get_session
from app.detection.features import reached_paypal

router = APIRouter(prefix="/studio", tags=["studio"])
Session = Annotated[AsyncSession, Depends(get_session)]

# An attempt belongs to an incident if it landed between shortly before the incident opened (the burst that
# opened it) and the incident closing.
LEAD_IN = timedelta(minutes=2)
MAX_ATTEMPTS = 5000
MAX_TURN_BYTES = 1_500_000


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _num(v: Any) -> float | None:
    try:
        return float(Decimal(str(v))) if v is not None else None
    except ArithmeticError:
        return None


@router.get("/data")
async def studio_data(
    shop_id: OwnerShop,
    session: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 30)] = 48,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    incidents = list(
        (
            await session.execute(
                select(Incident)
                .where(
                    Incident.shop_id == shop_id,
                    or_(Incident.opened_at >= since, Incident.closed_at.is_(None)),
                )
                .order_by(Incident.opened_at.desc())
                .limit(200)
            )
        ).scalars()
    )
    ids = [i.id for i in incidents]
    actions = list(
        (
            await session.execute(
                select(Action).where(Action.incident_id.in_(ids)).order_by(Action.created_at)
            )
        ).scalars()
        if ids
        else []
    )
    attempts = list(
        (
            await session.execute(
                select(CheckoutAttempt)
                .where(CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.ts > since)
                .order_by(CheckoutAttempt.ts.desc())
                .limit(MAX_ATTEMPTS)
            )
        ).scalars()
    )

    windows = sorted(((i.opened_at - LEAD_IN, i.closed_at or now, i.id) for i in incidents), reverse=True)

    def incident_of(ts: datetime) -> str | None:
        return next((iid for start, end, iid in windows if start <= ts <= end), None)

    refunds: dict[str, list[Action]] = {}
    for a in actions:
        if a.type == ActionType.REFUND and a.status == "executed":
            refunds.setdefault(a.incident_id, []).append(a)

    settings = get_settings()
    return {
        "as_of": now.isoformat(),
        "hours": hours,
        "ai": {
            "available": bool(settings.ai_enabled and settings.gemini_api_key),
            "model": gemini.models(settings)[0],
        },
        "attempts": [
            {
                "id": a.id,
                "ts": _iso(a.ts),
                "source": a.source,
                "scenario": a.scenario,
                "paypal_live": a.paypal_live,
                "sku": a.sku,
                "amount": _num(a.amount),
                "method": a.method,
                "device": (a.fingerprint_hash or "")[:8],
                "email_domain": a.email_domain,
                "decision": a.guard_decision,
                "guard_reason": a.guard_reason,
                "outcome": a.outcome,
                "decline_code": a.decline_code,
                "decline_category": a.decline_category,
                "reached_paypal": reached_paypal(a.outcome, a.decline_code),
                "incident_id": incident_of(a.ts),
            }
            for a in attempts
        ],
        "incidents": [
            {
                "id": i.id,
                "opened_at": _iso(i.opened_at),
                "closed_at": _iso(i.closed_at),
                "status": i.status,
                "classification": i.classification,
                "confidence": i.confidence,
                "model_id": i.model_id,
                "ai_available": i.ai_available,
                "classifier_version": i.classifier_version,
                "triggers": ", ".join(i.triggers or []),
                "rules_hint": i.rules_hint,
                "dropped_evidence": i.dropped_evidence,
                "refunds": len(refunds.get(i.id, [])),
                "refunded_total": sum((_num(a.params.get("amount")) or 0.0) for a in refunds.get(i.id, [])),
            }
            for i in incidents
        ],
        "actions": [
            {
                "id": a.id,
                "incident_id": a.incident_id,
                "created_at": _iso(a.created_at),
                "type": a.type,
                "status": a.status,
                "rule": a.policy_rule,
                "amount": _num(a.params.get("amount")) if a.type == ActionType.REFUND else None,
                "paypal_ref": a.paypal_ref,
                "approved_by": a.approved_by,
            }
            for a in actions
        ],
    }


@router.post("/agent/turn")
async def agent_turn(request: Request, _shop: OwnerShop) -> Any:
    """One Studio Agent Framework LLM turn on Gemini. Errors come back as {error: {code, message}}, which the
    browser adapter shows in the chat instead of failing silently."""
    body = await request.body()
    if len(body) > MAX_TURN_BYTES:
        return JSONResponse(
            {"error": {"code": "too_large", "message": "conversation too long"}}, status_code=413
        )
    try:
        payload = await request.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": {"code": "bad_request", "message": "body must be a JSON object"}}, status_code=400
        )
    try:
        return await studio_agent.run_turn(payload)
    except studio_agent.TurnError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
