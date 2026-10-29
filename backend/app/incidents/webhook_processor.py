"""Applies stored PayPal webhook events to checkout attempts. Only verified events change state.

An order no checkout of ours created (no custom_id of this shop) is observed instead: it becomes an
OBSERVED attempt (app/incidents/observer.py), so a shop that cannot change its checkout is still watched.

Events are linked to attempts by capture id, order id, or the custom_id we send on every order
("{shop_id}:{attempt_id}"). An event that matches nothing yet stays pending for UNMATCHED_RETRY:
the attempt row is committed only after PayPal's response returns, so a fast webhook can arrive first.
While it waits it is retried every RETRY_EVERY and skipped in between (retry_after), so a pile of
events that will never match (simulator mocks, other integrations, reset demo data) cannot fill every
batch and delay new events.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import CheckoutAttempt, Outcome, PayPalEvent
from app.events import publish
from app.incidents import observer
from app.paypal import codes
from app.paypal.webhooks import TRUSTED
from app.shop.service import attempt_event, short_hash

log = structlog.get_logger()
DISPUTE_EVENTS = frozenset(
    {"CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"}
)
# Resolutions that leave the money with the seller: only these clear `disputed`. Anything else, including a
# missing or unknown outcome code, keeps it set (fail closed: a refund on top of a payout pays twice).
SELLER_KEPT_OUTCOMES = frozenset({"RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"})
UNMATCHED_RETRY = timedelta(minutes=2)
RETRY_EVERY = timedelta(seconds=15)


def _up_link_id(resource: dict[str, Any]) -> str | None:
    for link in resource.get("links", []):
        if link.get("rel") == "up":
            return str(link.get("href", "")).rstrip("/").rsplit("/", 1)[-1]
    return None


def _custom_id(resource: dict[str, Any]) -> tuple[str, str] | None:
    """(shop_id, attempt_id) from custom_id: on the capture resource, or on the order's purchase unit."""
    raw = resource.get("custom_id") or next(
        (u.get("custom_id") for u in resource.get("purchase_units") or [] if u.get("custom_id")), None
    )
    if not isinstance(raw, str) or ":" not in raw:
        return None
    shop_id, attempt_id = raw.rsplit(":", 1)
    return (shop_id, attempt_id) if shop_id and attempt_id else None


async def _attempt(
    session: AsyncSession,
    capture_id: str | None,
    order_id: str | None,
    ref: tuple[str, str] | None = None,
) -> CheckoutAttempt | None:
    conds = []
    if capture_id:
        conds.append(CheckoutAttempt.capture_id == capture_id)
    if order_id:
        conds.append(CheckoutAttempt.paypal_order_id == order_id)
    if ref:
        conds.append(and_(CheckoutAttempt.shop_id == ref[0], CheckoutAttempt.id == ref[1]))
    if not conds:
        return None
    return (await session.execute(select(CheckoutAttempt).where(or_(*conds)).limit(1))).scalar_one_or_none()


def apply_dispute(a: CheckoutAttempt, event_type: str, r: dict[str, Any]) -> None:
    """disputed means "a refund now risks paying twice". It is set while a dispute is open and after any
    resolution except one that left the money with the seller (SELLER_KEPT_OUTCOMES). Disputes are told
    apart by dispute_id: a late CREATED/UPDATED for a resolved dispute never reopens it, but a CREATED for a
    new dispute on the same capture does. Once a resolution paid the buyer, nothing clears it again."""
    dispute_id = str(r.get("dispute_id") or "")[:64] or None
    other = bool(dispute_id and a.dispute_id and dispute_id != a.dispute_id)
    resolved = (a.dispute_status or "").startswith("RESOLVED")
    if event_type == "CUSTOMER.DISPUTE.RESOLVED":
        if other and (not resolved or a.disputed):
            return  # another dispute ending while ours is open, or after ours already paid the buyer
        outcome = str((r.get("dispute_outcome") or {}).get("outcome_code") or "UNKNOWN")
        a.dispute_status = f"RESOLVED:{outcome}"[:40]
        a.dispute_id = dispute_id or a.dispute_id
        a.disputed = outcome not in SELLER_KEPT_OUTCOMES
        return
    if resolved and (not other or a.disputed):
        return
    a.dispute_status = str(r.get("status") or "OPEN")[:40]
    a.dispute_id = dispute_id or a.dispute_id
    a.disputed = True


async def apply_event(session: AsyncSession, ev: PayPalEvent) -> str:
    r: dict[str, Any] = (ev.payload or {}).get("resource") or {}
    et = ev.event_type or ""
    related_order = ((r.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id")
    ref = _custom_id(r)
    if et in observer.OBSERVABLE and observer.is_foreign(ref, get_settings().shop_id):
        order_id = r.get("id") if et in observer.ORDER_EVENTS else related_order
        ours = await _attempt(session, r.get("id") if et in observer.CAPTURE_EVENTS else None, order_id)
        if ours is None or ours.source == "observed":
            a = await observer.observe(session, ev, et, r)
            await publish(session, "attempt", attempt_event(a))
            return "observed"

    if et == "PAYMENT.CAPTURE.COMPLETED":
        a = await _attempt(session, r.get("id"), related_order, ref)
        if a and a.outcome != Outcome.REFUNDED:
            a.outcome, a.capture_id = Outcome.CAPTURED, r.get("id")
            a.decline_code = a.decline_category = a.processor_code = None
    elif et in {"PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"}:
        a = await _attempt(session, r.get("id"), related_order, ref)
        if a:
            processor_code = (r.get("processor_response") or {}).get("response_code")
            a.outcome = Outcome.DECLINED
            if processor_code or not a.decline_code:
                # The processor code is the real reason; the event name is only a fallback.
                c = codes.decline_for_webhook(processor_code, et.rsplit(".", 1)[-1])
                a.decline_code, a.decline_category, a.processor_code = (
                    c.decline_code,
                    c.category,
                    processor_code,
                )
    elif et in {"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"}:
        a = await _attempt(session, _up_link_id(r) or r.get("id"), None)
        if a:
            a.outcome, a.refund_id = Outcome.REFUNDED, a.refund_id or r.get("id")
    elif et == "CHECKOUT.ORDER.APPROVED":
        a = await _attempt(session, None, r.get("id"), ref)
        payer = (r.get("payer") or {}).get("payer_id")
        if a and payer:
            a.payer_id_hash = short_hash(payer)
    elif et in DISPUTE_EVENTS:
        ids = [t.get("seller_transaction_id") for t in r.get("disputed_transactions", [])]
        a = None
        for cid in filter(None, ids):
            a = await _attempt(session, cid, None)
            if a:
                apply_dispute(a, et, r)
    else:
        return "ignored"
    if a:
        if a.source == "api" and ev.verified == "SUCCESS":
            a.paypal_live = True  # another shop's checked order: PayPal itself now vouches for it
        await publish(session, "attempt", attempt_event(a))
        return "applied"
    return "no_matching_attempt"


async def process_pending(session: AsyncSession, limit: int = 100) -> int:
    now = datetime.now(UTC)
    q = (
        select(PayPalEvent)
        .where(
            PayPalEvent.processed_at.is_(None),
            or_(PayPalEvent.retry_after.is_(None), PayPalEvent.retry_after <= now),
        )
        .order_by(PayPalEvent.received_at)
        .limit(limit)
    )
    events = list((await session.execute(q)).scalars())
    for ev in events:
        if ev.verified in TRUSTED:
            result = await apply_event(session, ev)
        else:
            result = "skipped_unverified"
        if result == "no_matching_attempt" and ev.received_at and now - ev.received_at < UNMATCHED_RETRY:
            ev.retry_after = now + RETRY_EVERY  # the attempt may not be committed yet: try again soon
            continue
        ev.processed_at = now
        log.info("webhook_processed", event_type=ev.event_type, result=result)
    await session.commit()
    return len(events)
