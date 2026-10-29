import hashlib
import json
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_paypal
from app.config import Settings, get_settings
from app.db.models import PayPalEvent
from app.db.session import get_session
from app.paypal.client import PayPalClient
from app.paypal.webhooks import redact, safe_signals, verify_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger()

# PayPal events are a few KB (a dispute with messages maybe tens). Read at most this much: a huge body is not
# PayPal and must not be buffered whole.
MAX_WEBHOOK_BYTES = 256 * 1024


async def _body_within_limit(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="webhook body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="webhook body too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/paypal")
async def paypal_webhook(
    request: Request,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Verify, store, return 200 fast. The worker applies verified events (webhook_processor.py). The
    signature is checked against the body exactly as received; what is stored has the buyer's personal
    data (names, emails, addresses, phones) redacted."""
    try:
        raw_body = (await _body_within_limit(request)).decode()
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid encoding") from exc
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    verification = await verify_webhook(paypal, dict(request.headers), raw_body, settings.paypal_webhook_id)
    transmission_id = request.headers.get("paypal-transmission-id") or (
        "no-tx-" + hashlib.sha256(raw_body.encode()).hexdigest()[:40]
    )
    stmt = (
        insert(PayPalEvent)
        .values(
            transmission_id=transmission_id,
            event_id=event.get("id"),
            event_type=event.get("event_type"),
            resource_id=(event.get("resource") or {}).get("id"),
            verified=verification.value,
            # Buyer data redacted; the few signals detection needs kept under "_cg" (safe_signals).
            payload={**redact(event), "_cg": safe_signals(event)},
        )
        .on_conflict_do_nothing(index_elements=["transmission_id"])  # PayPal retries are deduplicated
    )
    await session.execute(stmt)
    await session.commit()
    log.info("paypal_webhook", event_type=event.get("event_type"), verification=verification.value)
    return {"verification": verification.value}
