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
from app.guard.check import GuardContext, GuardVerdict, check
from app.paypal.client import PayPalClient
from app.paypal.orders import (
    DECLINE_ISSUES,
    CardDetails,
    OrderResult,
    capture_order,
    create_order,
    order_payload,
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
    Used for the eval harness and when the sandbox account can't process cards yet."""

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


def _outcome_from_paypal(res: OrderResult) -> Outcome:
    if res.is_processor_error:
        return Outcome.ERROR
    if res.status == "PAYER_ACTION_REQUIRED":
        return Outcome.SCA_REQUIRED
    if res.capture_status == "COMPLETED" or res.status == "COMPLETED":
        return Outcome.CAPTURED
    if res.capture_status == "DECLINED" or (res.issue and res.issue in DECLINE_ISSUES):
        return Outcome.DECLINED
    if res.http >= 400:
        # Other 4xx: integration problems (e.g. PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING) count as errors.
        return Outcome.ERROR
    if res.status in {"CREATED", "APPROVED", "SAVED"}:
        return Outcome.CREATED
    return Outcome.ERROR


def _offline_result(plan: OfflinePlan, force_sca: bool, method: str) -> OrderResult:
    oid = f"SIM-{uuid.uuid4().hex[:12].upper()}"
    latency = random.randint(180, 650)
    if plan.outcome == "error":
        return OrderResult(503, None, None, None, None, "SERVICE_UNAVAILABLE", None, latency)
    if force_sca and method == Method.CARD and plan.is_bot:
        return OrderResult(200, oid, "PAYER_ACTION_REQUIRED", None, None, None, None, latency)
    if plan.outcome == "decline":
        return OrderResult(422, None, None, None, None, plan.decline_code, None, latency)
    cap = f"SIMCAP-{uuid.uuid4().hex[:10].upper()}"
    return OrderResult(201, oid, "COMPLETED", cap, "COMPLETED", None, None, latency)


async def place_order(
    session: AsyncSession, paypal: PayPalClient | None, req: CheckoutRequest
) -> CheckoutResult:
    product = PRODUCTS[req.sku]
    fp = fingerprint(req.device_id, req.user_agent)
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
        force_sca = verdict.decision == Decision.ALLOW_WITH_SCA
        if req.offline is not None:
            result = _offline_result(req.offline, force_sca, req.method)
        else:
            assert paypal is not None
            payload = order_payload(
                sku=product.sku,
                description=product.name,
                amount=product.price,
                custom_id=f"{req.shop_id}:{attempt.id}",
                card=req.card,
                force_sca=force_sca and req.method == Method.CARD,
            )
            result = await create_order(paypal, payload)
            # Server-side card orders (simulator) come back APPROVED and are captured immediately.
            if req.card and result.order_id and result.status == "APPROVED":
                result = await capture_order(paypal, result.order_id)
        attempt.outcome = _outcome_from_paypal(result)
        attempt.paypal_order_id = result.order_id
        attempt.order_status = result.status
        attempt.capture_id = result.capture_id
        attempt.decline_code = result.issue if attempt.outcome in {Outcome.DECLINED, Outcome.ERROR} else None
        attempt.paypal_debug_id = result.debug_id
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


async def capture_web_order(
    session: AsyncSession, paypal: PayPalClient, shop_id: str, order_id: str
) -> CheckoutAttempt | None:
    """Capture a browser order after buyer approval (wallet) or card-fields submit."""
    attempt = (
        await session.execute(
            select(CheckoutAttempt).where(
                CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.paypal_order_id == order_id
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        return None
    res = await capture_order(paypal, order_id)
    attempt.outcome = _outcome_from_paypal(res)
    attempt.order_status = res.status
    attempt.capture_id = res.capture_id
    attempt.decline_code = res.issue if attempt.outcome in {Outcome.DECLINED, Outcome.ERROR} else None
    attempt.paypal_debug_id = res.debug_id
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
        "outcome": a.outcome,
        "decline_code": a.decline_code,
    }
