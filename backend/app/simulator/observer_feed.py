"""Observer-mode simulation: a scenario delivered as the PayPal webhooks a shop's own checkout would cause.

Each simulated attempt becomes the events PayPal sends for an order made outside Checkout Guard (a hosted
button): CHECKOUT.ORDER.APPROVED, then PAYMENT.CAPTURE.COMPLETED or PAYMENT.CAPTURE.DECLINED. An order the
buyer never approves, or a PayPal error, sends nothing, as in reality. The events are written straight into
the webhook table as SIMULATED (never through the HTTP endpoint, which always verifies) and the worker turns
them into OBSERVED attempts (app/incidents/observer.py), like real webhooks from a connected account.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Outcome, PayPalEvent
from app.paypal import codes
from app.paypal.webhooks import Verification, redact, safe_signals
from app.shop.catalog import PRODUCTS
from app.simulator.scenarios import SimAttempt


def _payer(device_id: str) -> str:
    return "SIMPAYER" + hashlib.sha256(device_id.encode()).hexdigest()[:8].upper()


def events_for(a: SimAttempt) -> list[tuple[str, dict[str, Any]]]:
    if a.outcome in ("pending", "error"):
        return []
    product = PRODUCTS[a.sku]
    now = datetime.now(UTC).isoformat()
    order_id = f"SIMOBS-{uuid.uuid4().hex[:12].upper()}"
    amount = {"currency_code": "USD", "value": f"{product.price:.2f}"}
    order: dict[str, Any] = {
        "id": order_id,
        "status": "APPROVED",
        "create_time": now,
        "purchase_units": [
            {
                "reference_id": a.sku,
                "amount": amount,
                "items": [{"name": product.name, "sku": a.sku, "quantity": "1"}],
            }
        ],
        "payer": {
            "email_address": a.email,
            "payer_id": a.payer_id or _payer(a.device_id),
        },
    }
    if a.method == "card" and a.card_number:
        order["payment_source"] = {
            "card": {
                "brand": "VISA",
                "last_digits": a.card_number[-4:],
                "expiry": a.card_expiry,
                "bin_details": {"bin": a.card_number[:6]},
            }
        }
    capture: dict[str, Any] = {
        "id": f"SIMOBSCAP-{uuid.uuid4().hex[:10].upper()}",
        "amount": amount,
        "create_time": now,
        "supplementary_data": {"related_ids": {"order_id": order_id}},
    }
    if a.outcome == "approve":
        capture["status"] = "COMPLETED"
        return [("CHECKOUT.ORDER.APPROVED", order), ("PAYMENT.CAPTURE.COMPLETED", capture)]
    capture["status"] = "DECLINED"
    code = codes.trigger_code(a.card_name)
    if code:
        capture["processor_response"] = {"response_code": code}
    return [("CHECKOUT.ORDER.APPROVED", order), ("PAYMENT.CAPTURE.DECLINED", capture)]


INTENDED: dict[str, Outcome] = {
    "approve": Outcome.CAPTURED,
    "decline": Outcome.DECLINED,
    "pending": Outcome.CREATED,
    "error": Outcome.ERROR,
}


async def deliver(session: AsyncSession, a: SimAttempt) -> Outcome:
    """Write the attempt's simulated webhooks; returns the outcome PayPal would report. Caller commits."""
    for event_type, resource in events_for(a):
        event = {"id": f"WH-SIM-{uuid.uuid4().hex[:16]}", "event_type": event_type, "resource": resource}
        session.add(
            PayPalEvent(
                transmission_id=f"sim-{uuid.uuid4().hex}",
                event_id=event["id"],
                event_type=event_type,
                resource_id=resource["id"],
                verified=Verification.SIMULATED,
                payload={**redact(event), "_cg": safe_signals(event)},
            )
        )
    return INTENDED[a.outcome]
