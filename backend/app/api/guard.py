"""Public guard API: what another shop's checkout (e.g. a WooCommerce plugin) calls before it creates a PayPal
order. The demo shop uses the same check in-process.

  1. POST /v1/guard/check before creating the order. The answer says allow / allow with 3-D Secure /
     challenge / block, and gives a custom_id. Put it on the order (purchase_units[0].custom_id).
  2. PayPal's webhooks for that order (captured, declined, refunded, disputed) then reach this attempt
     through the custom_id, with the device and network the shop sent here: full detection, not observer
     mode.
  3. POST /v1/guard/outcome for what PayPal sends no webhook for (a card refused when the order was
     created, an error, the buyer leaving) or when the shop has no webhooks set up.
"""

from decimal import Decimal
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_shop_key
from app.db.models import CheckoutAttempt, Decision, Outcome, Source, new_id
from app.db.session import get_session
from app.events import publish
from app.guard import signals
from app.guard.check import GuardContext, check_within_budget
from app.shop.service import attempt_event, custom_id

router = APIRouter(prefix="/v1/guard", tags=["guard"])
log = structlog.get_logger()
Session = Annotated[AsyncSession, Depends(get_session)]


class CheckIn(BaseModel):
    sku: str = Field(max_length=64)
    method: Literal["card", "wallet"]
    fingerprint_hash: str = Field(min_length=8, max_length=64, description="Shop-side hash of device/IP/UA")
    challenge_token: str | None = None
    amount: Decimal | None = Field(default=None, ge=0, le=100000)
    # Only the domain of the buyer's email, never the address.
    email_domain: str | None = Field(default=None, max_length=255)
    # The buyer's IP as the shop's server saw it: kept only as keyed hashes (app/guard/signals.py).
    client_ip: str | None = Field(default=None, max_length=64)


class CheckOut(BaseModel):
    decision: str
    reason: str
    incident_id: str | None
    sca_method: str | None = Field(
        description="Set payment_source.card.attributes.verification.method to this"
    )
    attempt_id: str | None = Field(description="This check, recorded; null if it could not be recorded")
    custom_id: str | None = Field(
        description="Put on the PayPal order (purchase_units[0].custom_id) so its webhooks reach this attempt"
    )


@router.post("/check")
async def guard_check(
    body: CheckIn, shop_id: Annotated[str, Depends(require_shop_key)], session: Session
) -> CheckOut:
    ip = signals.client_ip(None, body.client_ip, 0)
    net = signals.ip_net_hash(ip)
    # Fail-open: a slow guard answers allow (or the last known defences) rather than hold up the shop.
    v = await check_within_budget(
        session,
        GuardContext(shop_id, body.sku, body.method, body.fingerprint_hash, body.challenge_token, net),
        lock=False,
    )
    stopped = {Decision.BLOCK: Outcome.BLOCKED, Decision.CHALLENGE: Outcome.CHALLENGED}
    attempt = CheckoutAttempt(
        id=new_id(),
        shop_id=shop_id,
        source=Source.API,
        sku=body.sku,
        amount=body.amount or Decimal("0"),
        method=body.method,
        fingerprint_hash=body.fingerprint_hash,
        ip_hash=signals.ip_hash(ip),
        ip_net_hash=net,
        email_domain=(body.email_domain or "").strip().lower() or None,
        guard_decision=v.decision,
        guard_reason=v.reason,
        outcome=stopped.get(Decision(v.decision), Outcome.CREATED),
        paypal_live=False,  # set once a PayPal webhook for its order arrives
    )
    recorded: str | None = attempt.id
    try:
        session.add(attempt)
        await session.flush()
        await publish(session, "attempt", attempt_event(attempt))
        await session.commit()
    except Exception:  # the answer matters more than the record: the shop's checkout goes on
        log.warning("guard_check_not_recorded", shop=shop_id)
        await session.rollback()
        recorded = None
    return CheckOut(
        decision=v.decision,
        reason=v.reason,
        incident_id=v.incident_id,
        sca_method="SCA_ALWAYS" if v.decision == Decision.ALLOW_WITH_SCA else None,
        attempt_id=recorded,
        custom_id=custom_id(shop_id, recorded) if recorded else None,
    )


class OutcomeIn(BaseModel):
    attempt_id: str = Field(max_length=16)
    outcome: Literal["captured", "declined", "error", "sca_failed", "abandoned"]
    decline_code: str | None = Field(default=None, max_length=64)
    paypal_order_id: str | None = Field(default=None, max_length=40)
    capture_id: str | None = Field(default=None, max_length=40)


OUTCOMES = {
    "captured": Outcome.CAPTURED,
    "declined": Outcome.DECLINED,
    "error": Outcome.ERROR,
    "sca_failed": Outcome.SCA_FAILED,
    "abandoned": Outcome.CREATED,
}


@router.post("/outcome")
async def guard_outcome(
    body: OutcomeIn, shop_id: Annotated[str, Depends(require_shop_key)], session: Session
) -> dict:
    """What happened to a checked order, for what PayPal sends no webhook about. Only an attempt still waiting
    for its outcome changes: a capture or refund a webhook already recorded is never overwritten."""
    attempt = await session.get(CheckoutAttempt, body.attempt_id, with_for_update=True)
    if attempt is None or attempt.shop_id != shop_id or attempt.source != Source.API:
        raise HTTPException(404, "unknown attempt")
    if attempt.outcome == Outcome.CREATED:
        attempt.outcome = OUTCOMES[body.outcome]
        attempt.decline_code = body.decline_code if body.outcome == "declined" else None
        attempt.paypal_order_id = body.paypal_order_id or attempt.paypal_order_id
        attempt.capture_id = body.capture_id or attempt.capture_id
        await publish(session, "attempt", attempt_event(attempt))
    await session.commit()
    return {"attempt_id": attempt.id, "outcome": attempt.outcome}
