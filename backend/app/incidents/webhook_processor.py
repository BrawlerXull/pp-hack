"""Applies stored PayPal webhook events to checkout attempts. Only verified events change state."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckoutAttempt, Outcome, PayPalEvent
from app.events import publish
from app.paypal.webhooks import Verification
from app.shop.service import attempt_event, short_hash

log = structlog.get_logger()


def _up_link_id(resource: dict[str, Any]) -> str | None:
    for link in resource.get("links", []):
        if link.get("rel") == "up":
            return str(link.get("href", "")).rstrip("/").rsplit("/", 1)[-1]
    return None


async def _attempt(
    session: AsyncSession, capture_id: str | None, order_id: str | None
) -> CheckoutAttempt | None:
    conds = []
    if capture_id:
        conds.append(CheckoutAttempt.capture_id == capture_id)
    if order_id:
        conds.append(CheckoutAttempt.paypal_order_id == order_id)
    if not conds:
        return None
    return (await session.execute(select(CheckoutAttempt).where(or_(*conds)).limit(1))).scalar_one_or_none()


async def apply_event(session: AsyncSession, ev: PayPalEvent) -> str:
    r: dict[str, Any] = (ev.payload or {}).get("resource") or {}
    et = ev.event_type or ""
    related_order = ((r.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")

    if et == "PAYMENT.CAPTURE.COMPLETED":
        a = await _attempt(session, r.get("id"), related_order)
        if a and a.outcome != Outcome.REFUNDED:
            a.outcome, a.capture_id = Outcome.CAPTURED, r.get("id")
    elif et in {"PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"}:
        a = await _attempt(session, r.get("id"), related_order)
        if a:
            a.outcome, a.decline_code = Outcome.DECLINED, a.decline_code or et.rsplit(".", 1)[-1]
    elif et in {"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"}:
        a = await _attempt(session, _up_link_id(r) or r.get("id"), None)
        if a:
            a.outcome, a.refund_id = Outcome.REFUNDED, a.refund_id or r.get("id")
    elif et == "CHECKOUT.ORDER.APPROVED":
        a = await _attempt(session, None, r.get("id"))
        payer = (r.get("payer") or {}).get("payer_id")
        if a and payer:
            a.payer_id_hash = short_hash(payer)
    elif et.startswith("CUSTOMER.DISPUTE."):
        ids = [t.get("seller_transaction_id") for t in r.get("disputed_transactions", [])]
        a = None
        for cid in filter(None, ids):
            a = await _attempt(session, cid, None)
            if a:
                a.disputed = True
    else:
        return "ignored"
    if a:
        await publish(session, "attempt", attempt_event(a))
        return "applied"
    return "no_matching_attempt"


async def process_pending(session: AsyncSession, limit: int = 100) -> int:
    q = (
        select(PayPalEvent)
        .where(PayPalEvent.processed_at.is_(None))
        .order_by(PayPalEvent.received_at)
        .limit(limit)
    )
    events = list((await session.execute(q)).scalars())
    for ev in events:
        if ev.verified == Verification.SUCCESS:
            result = await apply_event(session, ev)
        else:
            result = "skipped_unverified"
        ev.processed_at = datetime.now(UTC)
        log.info("webhook_processed", event_type=ev.event_type, result=result)
    await session.commit()
    return len(events)
