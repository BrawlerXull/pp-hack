"""The owner's answer to a dispute, sent through PayPal's Disputes API (only after the owner decides).

accept  → POST /v1/customer/disputes/{id}/accept-claim       (the buyer gets the money back now)
contest → POST /v1/customer/disputes/{id}/provide-evidence   (multipart: the evidence and the note)
dismiss → nothing is sent; the owner answers in PayPal themselves

A case on an offline-simulated payment is answered without a PayPal call, labelled simulated.
"""

import json
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import DisputeCase
from app.disputes.advisor import CONTEST_EVIDENCE, now
from app.events import publish
from app.paypal.client import PayPalClient

log = structlog.get_logger()
Decision = Literal["accept", "contest", "dismiss"]


def case_event(c: DisputeCase) -> dict:
    return {
        "id": c.id,
        "attempt_id": c.attempt_id,
        "incident_id": c.incident_id,
        "reason": c.reason,
        "amount": str(c.amount),
        "response_due": c.response_due,
        "status": c.status,
        "recommendation": c.recommendation,
        "rule": c.rule,
        "evidence_type": c.evidence_type,
        "buyer_says": (c.evidence or {}).get("buyer_says"),
        "contradicts_reason": bool((c.evidence or {}).get("contradicts_reason")),
        "note": c.note,
        "note_by": c.note_by,
        "paypal_live": c.paypal_live,
        "created_at": c.created_at,
        "decided_at": c.decided_at,
        "paypal_result": c.paypal_result,
    }


async def respond(
    session: AsyncSession,
    paypal: PayPalClient | None,
    case: DisputeCase,
    decision: Decision,
    note: str | None,
) -> DisputeCase:
    """Send the owner's decision. Caller holds the case row lock and commits."""
    if note:
        case.note = note[:2000]
        case.note_by = "owner"
    if decision == "dismiss":
        case.status, case.paypal_result = "dismissed", "answered in PayPal by the owner"
    elif not case.paypal_live or paypal is None:
        case.status = "accepted" if decision == "accept" else "contested"
        case.paypal_result = "simulated: no PayPal call"
    else:
        request_id = f"cg-dispute-{decision}-{case.id}"
        if decision == "accept":
            res = await paypal.request(
                "POST",
                f"/v1/customer/disputes/{case.id}/accept-claim",
                json={"note": case.note},
                request_id=request_id,
            )
        else:
            evidence_type = case.evidence_type or CONTEST_EVIDENCE.get((case.reason or "").upper(), "OTHER")
            evidence = {"evidence_type": evidence_type, "notes": case.note}
            info = {k: v for k, v in (case.evidence or {}).items() if k == "refund_ids" and v}
            if info:
                evidence["evidence_info"] = info
            body = json.dumps({"evidences": [evidence]})
            res = await paypal.request(
                "POST",
                f"/v1/customer/disputes/{case.id}/provide-evidence",
                files={"input": (None, body, "application/json")},
                request_id=request_id,
            )
        if res.ok:
            case.status = "accepted" if decision == "accept" else "contested"
            case.paypal_result = f"HTTP {res.status}"
        else:
            case.status = "failed"
            case.paypal_result = str(res.error_summary())[:500]
            log.warning("dispute_response_failed", dispute=case.id, status=res.status)
    case.decided_at = now()
    audit(session, "owner", f"dispute.{decision}", case.incident_id, dispute_id=case.id, status=case.status)
    await publish(session, "dispute", case_event(case))
    return case
