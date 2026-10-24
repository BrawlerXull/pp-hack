"""Demo shop order flow: guard check → PayPal (or labelled offline simulation) → attempt log.

Every attempt is logged from our own server. That log is the primary attack signal, since the
sandbox may not send webhooks for declined cards (docs/sandbox-findings.md).
"""

import hashlib
import random
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CheckoutAttempt, Decision, Method, Outcome, Source, new_id
from app.events import publish
from app.guard.check import GuardContext, GuardVerdict, check, lock_device
from app.paypal import codes
from app.paypal.client import PayPalClient
from app.paypal.orders import (
    CardDetails,
    OrderResult,
    capture_order,
    create_order,
    get_order,
    order_payload,
    order_request_id,
)
from app.shop.catalog import PRODUCTS


def fingerprint(device_id: str, user_agent: str | None) -> str:
    return hashlib.sha256(f"{device_id}|{user_agent or ''}".encode()).hexdigest()[:32]


def short_hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:32] if value else None


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()[:255] or None


@dataclass
class CheckoutRequest:
    shop_id: str
    sku: str
    method: Literal["card", "wallet"]
    device_id: str
    user_agent: str | None
    ip: str | None
    email: str | None
    challenge_token: str | None = None
    # simulator-only fields
    source: str = Source.WEB
    scenario: str | None = None
    card: CardDetails | None = None
    payer_id: str | None = None
    offline: "OfflinePlan | None" = None


@dataclass
class OfflinePlan:
    """Labelled offline simulation (no PayPal call): what PayPal *would* have returned.
    Used for the eval harness and when the sandbox account can't process cards yet.
    A card decline's code comes from the CCREJECT-* trigger in the card name (as in the sandbox);
    `decline_code` is the 422 issue used when there is no trigger (wallet declines)."""

    outcome: Literal["approve", "decline", "error"]
    decline_code: str = "INSTRUMENT_DECLINED"
    is_bot: bool = True  # bots can't complete a 3-D Secure challenge; real shoppers can


@dataclass
class CheckoutResult:
    attempt_id: str
    decision: Decision
    reason: str
    outcome: Outcome
    order_id: str | None = None
    order_status: str | None = None
    capture_id: str | None = None
    decline_code: str | None = None
    payer_action_url: str | None = None


def classify(res: OrderResult, method: str = Method.CARD) -> codes.Classification:
    return codes.classify(
        http=res.http,
        order_status=res.status,
        capture_status=res.capture_status,
        issue=res.issue,
        processor_code=res.processor_code,
        card=method == Method.CARD,
    )


def apply_result(attempt: CheckoutAttempt, res: OrderResult) -> None:
    """Copy a PayPal (or simulated) order result onto the attempt row. A row that is already CAPTURED or
    REFUNDED is never downgraded: a late failure (timeout, racing request) does not undo a real capture,
    which would drop it from refund matching and show a charged buyer as declined."""
    c = classify(res, attempt.method)
    if attempt.outcome in {Outcome.CAPTURED, Outcome.REFUNDED} and c.outcome != Outcome.CAPTURED:
        return
    attempt.outcome = c.outcome
    attempt.paypal_order_id = res.order_id or attempt.paypal_order_id
    attempt.order_status = res.status
    # Never drop a capture id we already hold (e.g. a later failed re-capture).
    attempt.capture_id = res.capture_id or attempt.capture_id
    attempt.decline_code = c.decline_code
    attempt.decline_category = c.category
    attempt.processor_code = c.processor_code
    attempt.paypal_debug_id = res.debug_id


def offline_result(plan: OfflinePlan, force_sca: bool, method: str, card: CardDetails | None) -> OrderResult:
    oid = f"SIM-{uuid.uuid4().hex[:12].upper()}"
    latency = random.randint(180, 650)
    if plan.outcome == "error":
        return OrderResult(503, None, None, None, None, "SERVICE_UNAVAILABLE", None, latency)
    if force_sca and method == Method.CARD and plan.is_bot:
        return OrderResult(200, oid, "PAYER_ACTION_REQUIRED", None, None, None, None, latency)
    if plan.outcome == "decline":
        trigger = codes.trigger_code(card.name) if card else None
        if trigger:
            # PayPal's capture-decline shape: 201, capture DECLINED + processor code (unconfirmed in sandbox).
            # No simulated capture id: on a declined row it would look like money was taken.
            return OrderResult(
                201, oid, "COMPLETED", None, "DECLINED", None, None, latency, processor_code=trigger
            )
        return OrderResult(422, None, None, None, None, plan.decline_code, None, latency)
    cap = f"SIMCAP-{uuid.uuid4().hex[:10].upper()}"
    return OrderResult(201, oid, "COMPLETED", cap, "COMPLETED", None, None, latency)


async def place_order(
    session: AsyncSession, paypal: PayPalClient | None, req: CheckoutRequest
) -> CheckoutResult:
    product = PRODUCTS[req.sku]
    fp = fingerprint(req.device_id, req.user_agent)
    # Held until the attempt row below is committed: parallel requests from one device are counted in turn.
    await lock_device(session, req.shop_id, fp)
    verdict: GuardVerdict = await check(
        session, GuardContext(req.shop_id, req.sku, req.method, fp, req.challenge_token)
    )

    attempt = CheckoutAttempt(
        id=new_id(),
        shop_id=req.shop_id,
        source=req.source,
        scenario=req.scenario,
        paypal_live=req.offline is None,
        sku=product.sku,
        amount=product.price,
        method=req.method,
        fingerprint_hash=fp,
        ip_hash=short_hash(req.ip),
        email_domain=email_domain(req.email),
        payer_id_hash=short_hash(req.payer_id),
        card_bin6=req.card.number[:6] if req.card else None,
        card_last4=req.card.number[-4:] if req.card else None,
        card_expiry=req.card.expiry if req.card else None,
        guard_decision=verdict.decision,
        guard_reason=verdict.reason,
    )

    result: OrderResult | None = None
    if verdict.decision == Decision.BLOCK:
        attempt.outcome = Outcome.BLOCKED
        attempt.paypal_live = False
    elif verdict.decision == Decision.CHALLENGE:
        attempt.outcome = Outcome.CHALLENGED
        attempt.paypal_live = False
    else:
        # Reserve the device's slot before PayPal answers (0.2-3 s): the next parallel request's velocity
        # count sees this row. Commits, which releases the device lock; no lock is held across the call.
        attempt.outcome = Outcome.CREATED
        session.add(attempt)
        await session.commit()
        force_sca = verdict.decision == Decision.ALLOW_WITH_SCA
        if req.offline is not None:
            result = offline_result(req.offline, force_sca, req.method, req.card)
        else:
            assert paypal is not None
            app_url = paypal.settings.public_app_url.rstrip("/")
            payload = order_payload(
                sku=product.sku,
                description=product.name,
                amount=product.price,
                custom_id=custom_id(req.shop_id, attempt.id),
                card=req.card,
                force_sca=force_sca and req.method == Method.CARD,
                return_url=f"{app_url}/?3ds=return",
                cancel_url=f"{app_url}/?3ds=cancel",
            )
            result = await create_order(paypal, payload, order_request_id(attempt.id))
            # Server-side card orders (simulator) come back APPROVED and are captured immediately.
            if req.card and result.order_id and result.status == "APPROVED":
                result = await capture_order(paypal, result.order_id)
        # Re-read under a short lock: a webhook may have updated the row while PayPal answered.
        locked = await session.get(CheckoutAttempt, attempt.id, with_for_update=True, populate_existing=True)
        assert locked is not None
        attempt = locked
        apply_result(attempt, result)
        attempt.latency_ms = result.latency_ms

    session.add(attempt)
    await session.flush()
    await publish(session, "attempt", attempt_event(attempt))
    await session.commit()

    return CheckoutResult(
        attempt_id=attempt.id,
        decision=verdict.decision,
        reason=verdict.reason,
        outcome=Outcome(attempt.outcome),
        order_id=attempt.paypal_order_id,
        order_status=attempt.order_status,
        capture_id=attempt.capture_id,
        decline_code=attempt.decline_code,
        payer_action_url=result.payer_action_url if result else None,
    )


def custom_id(shop_id: str, attempt_id: str) -> str:
    """Sent as purchase_units[].custom_id; webhooks echo it back (webhook_processor.py)."""
    return f"{shop_id}:{attempt_id}"


async def capture_web_order(
    session: AsyncSession, paypal: PayPalClient, shop_id: str, order_id: str
) -> CheckoutAttempt | None:
    """Capture a browser order after buyer approval (wallet) or card-fields submit.

    Idempotent: a repeat (double click, browser retry) returns the stored capture without calling
    PayPal. When PayPal's answer is unknown (timeout, 5xx) or says ORDER_ALREADY_CAPTURED, the order
    itself is read and decides. The row is re-read under a short lock only after PayPal answered (no
    lock is held across the call), and apply_result never downgrades a capture that a racing request
    or the webhook committed meanwhile."""
    attempt = (
        await session.execute(
            select(CheckoutAttempt).where(
                CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.paypal_order_id == order_id
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        return None
    if attempt.outcome in {Outcome.CAPTURED, Outcome.REFUNDED} and attempt.capture_id:
        return attempt
    tries = attempt.capture_tries or 0
    captured = await capture_order(paypal, order_id, tries)
    res: OrderResult | None = captured
    already = captured.issue == "ORDER_ALREADY_CAPTURED"
    if already or captured.http == 0 or captured.http >= 500:
        order = await get_order(paypal, order_id)
        if order.http == 200 and order.capture_status:
            res = order
        elif already:
            res = None  # PayPal says captured but we can't read it: leave the row for the webhook
    attempt = await session.get(CheckoutAttempt, attempt.id, with_for_update=True, populate_existing=True)
    assert attempt is not None
    if res is not None:
        if 400 <= res.http < 500:
            attempt.capture_tries = tries + 1  # a definitive refusal: the next capture is a new request
        apply_result(attempt, res)
        await publish(session, "attempt", attempt_event(attempt))
    await session.commit()
    return attempt


def attempt_event(a: CheckoutAttempt) -> dict[str, Any]:
    return {
        "id": a.id,
        "ts": a.ts,
        "source": a.source,
        "paypal_live": a.paypal_live,
        "scenario": a.scenario,
        "sku": a.sku,
        "amount": str(a.amount) if isinstance(a.amount, Decimal) else a.amount,
        "method": a.method,
        "fingerprint": (a.fingerprint_hash or "")[:8],
        "email_domain": a.email_domain,
        "decision": a.guard_decision,
        # Why the guard decided that (app/guard/check.py), for the War Room's "Why" column.
        "guard_reason": a.guard_reason,
        "outcome": a.outcome,
        "decline_code": a.decline_code,
        "decline_category": a.decline_category,
    }
