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
    """One checkout, as the shop's server sees it, before the PayPal order is created."""

    sku: str = Field(max_length=64, description="The item being bought (your product code)")
    method: Literal["card", "wallet"] = Field(description="card: card fields; wallet: the PayPal button")
    fingerprint_hash: str = Field(
        min_length=8,
        max_length=64,
        description="A stable hash of the buyer's device (e.g. SHA-256 of IP + user agent), never the raw "
        "values",
    )
    challenge_token: str | None = Field(
        default=None, description="The token from a passed human check, when retrying after a challenge"
    )
    amount: Decimal | None = Field(default=None, ge=0, le=100000, description="Order total, e.g. 3.00")
    email_domain: str | None = Field(
        default=None, max_length=255, description="Only the domain of the buyer's email, never the address"
    )
    # Kept only as keyed hashes (app/guard/signals.py).
    client_ip: str | None = Field(
        default=None, max_length=64, description="The buyer's IP as your server saw it (stored only hashed)"
    )
    dry_run: bool = Field(
        default=False,
        description="Answer as usual but record nothing: for testing an integration. attempt_id and "
        "custom_id are null.",
    )


class CheckOut(BaseModel):
    """What to do with this checkout."""

    decision: Literal["allow", "allow_with_sca", "challenge", "block"] = Field(
        description="allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. "
        "block: don't create the order"
    )
    reason: str = Field(description="Why, in a few words (for your logs)")
    incident_id: str | None = Field(description="The attack this checkout is part of, if any")
    sca_method: Literal["SCA_ALWAYS"] | None = Field(
        description="Set payment_source.card.attributes.verification.method to this"
    )
    attempt_id: str | None = Field(description="This check, recorded; null if it could not be recorded")
    custom_id: str | None = Field(
        description="Put on the PayPal order (purchase_units[0].custom_id) so its webhooks reach this attempt"
    )


@router.post(
    "/check",
    operation_id="check",
    summary="Check a checkout before creating the PayPal order",
    response_description="What to do with this checkout, and the custom_id to put on the PayPal order",
)
async def guard_check(
    body: CheckIn, shop_id: Annotated[str, Depends(require_shop_key)], session: Session
) -> CheckOut:
    """Call this from your server before creating each PayPal order, and do what `decision` says. Put
    `custom_id` on the order so PayPal's webhooks for it reach this checkout. If no answer comes within about
    a second, go on as allow: the guard is built to fail open."""
    ip = signals.client_ip(None, body.client_ip, 0)
    net = signals.ip_net_hash(ip)
    # Fail-open: a slow guard answers allow (or the last known defences) rather than hold up the shop.
    v = await check_within_budget(
        session,
        GuardContext(shop_id, body.sku, body.method, body.fingerprint_hash, body.challenge_token, net),
        lock=False,
    )
    if body.dry_run:
        return CheckOut(
            decision=v.decision,
            reason=v.reason,
            incident_id=v.incident_id,
            sca_method="SCA_ALWAYS" if v.decision == Decision.ALLOW_WITH_SCA else None,
            attempt_id=None,
            custom_id=None,
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
    """What happened to a checked order."""

    attempt_id: str = Field(max_length=16, description="attempt_id from the check")
    outcome: Literal["captured", "declined", "error", "sca_failed", "abandoned"] = Field(
        description="What PayPal (or the buyer) did"
    )
    decline_code: str | None = Field(default=None, max_length=64, description="PayPal's code when declined")
    paypal_order_id: str | None = Field(default=None, max_length=40, description="The PayPal order id")
    capture_id: str | None = Field(default=None, max_length=40, description="The PayPal capture id")


OUTCOMES = {
    "captured": Outcome.CAPTURED,
    "declined": Outcome.DECLINED,
    "error": Outcome.ERROR,
    "sca_failed": Outcome.SCA_FAILED,
    "abandoned": Outcome.CREATED,
}


class OutcomeOut(BaseModel):
    """The attempt after the report."""

    attempt_id: str = Field(description="The attempt reported on")
    outcome: str = Field(description="The attempt's outcome now (unchanged if a webhook recorded it first)")


@router.post("/outcome", operation_id="reportOutcome", summary="Report what happened to a checked order")
async def guard_outcome(
    body: OutcomeIn, shop_id: Annotated[str, Depends(require_shop_key)], session: Session
) -> OutcomeOut:
    """For what PayPal sends no webhook about (a card refused while creating the order, an error, the buyer
    leaving), or when your PayPal app has no webhooks. Only a checkout still waiting for its outcome changes:
    a capture or refund a webhook already recorded is never overwritten."""
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
    return OutcomeOut(attempt_id=attempt.id, outcome=attempt.outcome)
