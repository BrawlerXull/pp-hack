"""Dispute sync: read disputes from PayPal's Disputes API, not only from webhooks.

Seen live (Oct 29): a sandbox buyer opened a dispute through PayPal's site; the Disputes API listed it
within a minute, the CUSTOMER.DISPUTE.CREATED webhook arrived about three minutes later. Webhooks can be
late or lost, and a dispute missed is a chargeback answered by no one, so the worker also asks PayPal
(GET /v1/customer/disputes, then each dispute) every SYNC_EVERY_S. Each dispute, whenever it changed, is
stored like a webhook (verified API_SYNC: it came over our own authenticated API call) and goes through
the same processing: the payment is marked disputed and the assistant opens a case.
"""

import time
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PayPalEvent
from app.paypal.client import PayPalClient
from app.paypal.webhooks import Verification, redact, safe_signals

log = structlog.get_logger()

SYNC_EVERY_S = 300.0
PAGE_SIZE = 20

_last_sync: dict[str, float] = {}


async def sync(session: AsyncSession, paypal: PayPalClient | None, shop_id: str, force: bool = False) -> int:
    """Store every dispute PayPal lists that changed since we last stored it. Commits; returns how many."""
    if paypal is None or not paypal.settings.paypal_client_id:
        return 0
    now = time.monotonic()
    if not force and now - _last_sync.get(shop_id, -SYNC_EVERY_S) < SYNC_EVERY_S:
        return 0
    _last_sync[shop_id] = now
    listing = await paypal.request("GET", "/v1/customer/disputes", params={"page_size": PAGE_SIZE})
    if not listing.ok or not isinstance(listing.json, dict):
        log.warning("dispute_sync_failed", status=listing.status)
        return 0
    stored = 0
    for item in listing.json.get("items", []):
        dispute_id = item.get("dispute_id")
        if not dispute_id:
            continue
        detail = await paypal.request("GET", f"/v1/customer/disputes/{dispute_id}")
        if not detail.ok or not isinstance(detail.json, dict):
            continue
        resource: dict[str, Any] = detail.json
        event_type = (
            "CUSTOMER.DISPUTE.RESOLVED"
            if resource.get("status") == "RESOLVED"
            else "CUSTOMER.DISPUTE.UPDATED"
        )
        event = {"id": f"SYNC-{dispute_id}", "event_type": event_type, "resource": resource}
        # One row per dispute and update: a dispute that did not change since the last sync adds nothing.
        version = resource.get("update_time") or resource.get("status") or ""
        res = await session.execute(
            insert(PayPalEvent)
            .values(
                transmission_id=f"sync-{dispute_id}-{version}"[:80],
                event_id=event["id"],
                event_type=event_type,
                resource_id=dispute_id,
                verified=Verification.API_SYNC,
                payload={**redact(event), "_cg": safe_signals(event)},
            )
            .on_conflict_do_nothing(index_elements=["transmission_id"])
        )
        stored += res.rowcount or 0
        await follow(session, dispute_id, resource)
    await session.commit()
    if stored:
        log.info("dispute_sync", stored=stored)
    return stored


# PayPal's dispute status → our case's, once the shop's side is done (answered here, in PayPal itself, or
# PayPal decided). Open and failed cases follow it; a decision the owner made here is never overwritten.
FOLLOWED = {
    "WAITING_FOR_BUYER_RESPONSE": ("contested", "PayPal has the shop's answer and is waiting for the buyer"),
    "RESOLVED": ("resolved", "PayPal resolved the dispute"),
}


async def follow(session: AsyncSession, dispute_id: str, resource: dict[str, Any]) -> None:
    from app.db.models import DisputeCase

    case = await session.get(DisputeCase, dispute_id)
    if case is None or resource.get("status") not in FOLLOWED:
        return
    status, why = FOLLOWED[resource["status"]]
    if resource["status"] == "RESOLVED":
        outcome = (resource.get("dispute_outcome") or {}).get("outcome_code")
        why = f"{why}: {outcome}" if outcome else why
    if case.status in {"open", "failed"} or (status == "resolved" and case.status != "resolved"):
        case.status, case.paypal_result = status, why
