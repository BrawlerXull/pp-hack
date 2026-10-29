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
from app.guard import signals
from app.guard.check import GuardContext, GuardVerdict, check_within_budget
from app.paypal import codes, three_ds
from app.paypal.client import PayPalClient
from app.paypal.orders import (
    CardDetails,
    OrderResult,
    authorize_order,
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

    outcome: Literal["approve", "decline", "error", "pending"]
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
        authorization_status=res.authorization_status,
    )


def apply_result(attempt: CheckoutAttempt, res: OrderResult) -> None:
    """Copy a PayPal (or simulated) order result onto the attempt row. A row that is already CAPTURED or
    REFUNDED is never downgraded: a late failure (timeout, racing request) does not undo a real capture,
    which would drop it from refund matching and show a charged buyer as declined. A held (AUTHORIZED) or
    VOIDED payment is kept the same way: only settling a hold (app/actions/holds.py) moves it on."""
    apply_card(attempt, res.card)
    c = classify(res, attempt.method)
    settled = {Outcome.CAPTURED, Outcome.REFUNDED, Outcome.AUTHORIZED, Outcome.VOIDED}
    if attempt.outcome in settled and c.outcome not in {Outcome.CAPTURED, Outcome.AUTHORIZED}:
        return
    attempt.outcome = c.outcome
    attempt.paypal_order_id = res.order_id or attempt.paypal_order_id
    attempt.order_status = res.status
    # Never drop a capture id we already hold (e.g. a later failed re-capture).
    attempt.capture_id = res.capture_id or attempt.capture_id
    attempt.authorization_id = res.authorization_id or attempt.authorization_id
    attempt.decline_code = c.decline_code
    attempt.decline_category = c.category
    attempt.processor_code = c.processor_code
    attempt.paypal_debug_id = res.debug_id


def apply_card(attempt: CheckoutAttempt, card: three_ds.CardInfo | None) -> None:
    """What PayPal says about the card (brand, last 4, BIN, expiry, 3-D Secure result) onto the attempt, so a
    browser card order feeds detection's card features like a simulated one. Only fields PayPal sent."""
    if card is None:
        return
    attempt.card_brand = card.brand or attempt.card_brand
    attempt.card_last4 = card.last4 or attempt.card_last4
    attempt.card_bin6 = card.bin6 or attempt.card_bin6
    attempt.card_expiry = card.expiry or attempt.card_expiry
    if card.three_ds_ran:
        attempt.sca_liability_shift = card.liability_shift
        attempt.sca_enrollment = card.enrollment
        attempt.sca_auth_status = card.auth_status


def offline_result(
    plan: OfflinePlan, force_sca: bool, method: str, card: CardDetails | None, hold: bool = False
) -> OrderResult:
    oid = f"SIM-{uuid.uuid4().hex[:12].upper()}"
    latency = random.randint(180, 650)
    if plan.outcome == "error":
        return OrderResult(503, None, None, None, None, "SERVICE_UNAVAILABLE", None, latency)
    if force_sca and method == Method.CARD and plan.is_bot:
        return OrderResult(200, oid, "PAYER_ACTION_REQUIRED", None, None, None, None, latency)
    if plan.outcome == "pending":
        # A wallet order the buyer never approves: PayPal leaves it CREATED (an abandoned or failed login).
        return OrderResult(201, oid, "CREATED", None, None, None, None, latency)
    if plan.outcome == "decline":
        trigger = codes.trigger_code(card.name) if card else None
        if trigger and hold:
            return OrderResult(
                201,
                oid,
                "COMPLETED",
                None,
                None,
                None,
                None,
                latency,
                processor_code=trigger,
                authorization_status="DENIED",
            )
        if trigger:
            # PayPal's capture-decline shape: 201, capture DECLINED + processor code (unconfirmed in sandbox).
            # No simulated capture id: on a declined row it would look like money was taken.
            return OrderResult(
                201, oid, "COMPLETED", None, "DECLINED", None, None, latency, processor_code=trigger
            )
        return OrderResult(422, None, None, None, None, plan.decline_code, None, latency)
    if hold:
        auth = f"SIMAUTH-{uuid.uuid4().hex[:10].upper()}"
        return OrderResult(
            201,
            oid,
            "COMPLETED",
            None,
            None,
            None,
            None,
            latency,
            authorization_id=auth,
            authorization_status="CREATED",
        )
    cap = f"SIMCAP-{uuid.uuid4().hex[:10].upper()}"
    return OrderResult(201, oid, "COMPLETED", cap, "COMPLETED", None, None, latency)


async def place_order(
    session: AsyncSession, paypal: PayPalClient | None, req: CheckoutRequest
) -> CheckoutResult:
    product = PRODUCTS[req.sku]
    fp = fingerprint(req.device_id, req.user_agent)
    # The device lock is held until the attempt row below is committed: parallel requests from one device
    # are counted in turn. Past the time budget the checkout goes on without the guard (fail-open).
    net = signals.ip_net_hash(req.ip)
    verdict: GuardVerdict = await check_within_budget(
        session, GuardContext(req.shop_id, req.sku, req.method, fp, req.challenge_token, net), lock=True
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
        ip_hash=signals.ip_hash(req.ip),
        ip_net_hash=net,
        email_domain=email_domain(req.email),
        payer_id_hash=short_hash(req.payer_id),
        card_bin6=req.card.number[:6] if req.card else None,
        card_last4=req.card.number[-4:] if req.card else None,
        card_expiry=req.card.expiry if req.card else None,
        guard_decision=verdict.decision,
        guard_reason=verdict.reason,
        # Payment hold: authorised now, captured or voided once the incident is judged (app/actions/holds.py).
        hold_reason=verdict.hold_reason,
        hold_incident_id=verdict.incident_id if verdict.hold_reason == "incident" else None,
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
            result = offline_result(req.offline, force_sca, req.method, req.card, hold=verdict.hold)
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
                # A browser card payment: PayPal's Card Fields carry the number, our order carries none.
                card_fields=req.method == Method.CARD and req.card is None,
                # The shop lives at /shop on this branch (the landing page took /).
                return_url=f"{app_url}/shop?3ds=return",
                cancel_url=f"{app_url}/shop?3ds=cancel",
                hold=verdict.hold,
            )
            result = await create_order(paypal, payload, order_request_id(attempt.id))
            # Server-side card orders (simulator) come back APPROVED and are captured (or held) immediately.
            if req.card and result.order_id and result.status == "APPROVED":
                settle = authorize_order if verdict.hold else capture_order
                result = await settle(paypal, result.order_id)
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
    session: AsyncSession, paypal: PayPalClient, shop_id: str, order_id: str, *, record_only: bool = False
) -> CheckoutAttempt | None:
    """Capture a browser order after buyer approval (wallet) or card-fields submit.

    Idempotent: a repeat (double click, browser retry) returns the stored capture without calling
    PayPal. When PayPal's answer is unknown (timeout, 5xx) or says ORDER_ALREADY_CAPTURED, the order
    itself is read and decides. The row is re-read under a short lock only after PayPal answered (no
    lock is held across the call), and apply_result never downgrades a capture that a racing request
    or the webhook committed meanwhile.

    Every browser order is read first. A card submitted to a wallet order is refused: it would skip the
    guard's card decision and 3-D Secure. A browser card order (Card Fields) is captured only once it is
    APPROVED and its 3-D Secure result allows it (app/paypal/three_ds.py): the browser saying "verified"
    is not proof. If the order can't be read, a card order is left as it is (fail closed; the buyer can
    retry). A refusal is stored as SCA_FAILED without a capture call, and is final for that order.

    record_only: the browser reports that the card check failed or was cancelled. Nothing is captured;
    an order with a card on it (or waiting on the bank) is recorded as SCA_FAILED."""
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
    if attempt.outcome in {Outcome.AUTHORIZED, Outcome.VOIDED} and attempt.authorization_id:
        return attempt  # a held payment: settled by app/actions/holds.py, never by the browser
    if attempt.outcome == Outcome.SCA_FAILED:
        return attempt  # refused once: never captured later, whatever the order says now
    card_info: three_ds.CardInfo | None = None
    if attempt.source == Source.WEB:
        done, card_info = await _web_gate(session, paypal, attempt, order_id, record_only)
        if done is not None:
            return done
    if record_only:
        return attempt
    tries = attempt.capture_tries or 0
    # A held order (intent AUTHORIZE) is authorised here, not captured: app/actions/holds.py settles it.
    settle = authorize_order if attempt.hold_reason else capture_order
    captured = await settle(paypal, order_id, tries)
    res: OrderResult | None = captured
    already = captured.issue in {"ORDER_ALREADY_CAPTURED", "ORDER_ALREADY_AUTHORIZED"}
    if already or captured.http == 0 or captured.http >= 500:
        order = await get_order(paypal, order_id)
        if order.http == 200 and (order.capture_status or order.authorization_status):
            res = order
        elif already:
            res = None  # PayPal says captured but we can't read it: leave the row for the webhook
    if res is not None:
        res.card = three_ds.merge(card_info, res.card)
    return await _store(session, attempt.id, res, refused=res is not None and 400 <= res.http < 500)


async def _web_gate(
    session: AsyncSession, paypal: PayPalClient, attempt: CheckoutAttempt, order_id: str, record_only: bool
) -> tuple[CheckoutAttempt | None, three_ds.CardInfo | None]:
    """Read a browser order before capturing it. Returns (attempt, _) when the caller must stop there
    (captured already, refused, or not capturable yet), else (None, the card PayPal reported)."""
    is_card = attempt.method == Method.CARD
    if record_only and not is_card:
        return attempt, None
    order = await get_order(paypal, order_id)
    if order.http == 200 and (order.capture_status or order.authorization_status):
        # Already captured or held (racing request or retry).
        return await _store(session, attempt.id, order), None
    if order.http != 200:
        # A card order is never captured blind (fail closed; the buyer can retry). A wallet order keeps
        # the old path: the capture's own answer, then the order read, decide.
        return (attempt if is_card else None), None
    card = order.card
    if not is_card:
        if card is not None:
            # Created as a wallet order (no card decision, no 3-D Secure), then paid by card: refused.
            return await _store(session, attempt.id, None, sca_failed=order, as_card=True), None
        return None, None
    if order.status == "CREATED" and card is None:
        return attempt, None  # no card submitted yet: nothing to capture or record
    sca_required = attempt.guard_decision == Decision.ALLOW_WITH_SCA
    browser_gave_up = record_only and (card is not None or order.status == "PAYER_ACTION_REQUIRED")
    if browser_gave_up or not three_ds.capture_allowed(card, order.status, sca_required):
        return await _store(session, attempt.id, None, sca_failed=order), None
    if record_only:
        return attempt, None
    # Card Fields: after a 3-D Secure challenge the sandbox leaves the order CREATED (not APPROVED) with the
    # card and its authentication_result attached (seen live, Oct 25). The 3-D Secure rule above has already
    # passed, and PayPal itself refuses a capture the card wasn't authorised for, so CREATED-with-a-card is
    # capturable. Anything else (no card yet, PAYER_ACTION_REQUIRED, ...) still waits.
    if order.status != "APPROVED" and not (order.status == "CREATED" and card is not None):
        return attempt, None
    return None, card


async def _store(
    session: AsyncSession,
    attempt_id: str,
    res: OrderResult | None,
    *,
    refused: bool = False,
    sca_failed: OrderResult | None = None,
    as_card: bool = False,
) -> CheckoutAttempt:
    """Apply a capture result (or a 3-D Secure refusal) to the attempt under a short row lock."""
    attempt = await session.get(CheckoutAttempt, attempt_id, with_for_update=True, populate_existing=True)
    assert attempt is not None
    if sca_failed is not None:
        apply_card(attempt, sca_failed.card)
        if as_card:
            attempt.method = Method.CARD  # record the instrument PayPal saw, not the one the browser claimed
        if attempt.outcome not in {Outcome.CAPTURED, Outcome.REFUNDED}:
            attempt.outcome = Outcome.SCA_FAILED
            attempt.order_status = sca_failed.status
            attempt.decline_code = attempt.decline_category = attempt.processor_code = None
            attempt.paypal_debug_id = sca_failed.debug_id
        await publish(session, "attempt", attempt_event(attempt))
    elif res is not None:
        if refused:
            # A definitive refusal: the next capture is a new request.
            attempt.capture_tries = (attempt.capture_tries or 0) + 1
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
        # What PayPal said about a card (owner-only stream): brand, last 4, the bank's 3-D Secure answer.
        "card_brand": a.card_brand,
        "card_last4": a.card_last4,
        "sca_liability_shift": a.sca_liability_shift,
    }
