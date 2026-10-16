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
from app.paypal.webhooks import verify_webhook

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger()


@router.post("/paypal")
async def paypal_webhook(
    request: Request,
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Verify, store, return 200 fast. The worker applies verified events (webhook_processor.py)."""
    raw_body = (await request.body()).decode()
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
            payload=event,
        )
        .on_conflict_do_nothing(index_elements=["transmission_id"])  # PayPal retries are deduplicated
    )
    await session.execute(stmt)
    await session.commit()
    log.info("paypal_webhook", event_type=event.get("event_type"), verification=verification.value)
    return {"verification": verification.value}
