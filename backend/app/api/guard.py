"""Public guard API: what any shop's checkout (e.g. a future WooCommerce plugin) calls before
creating a PayPal order. The demo shop uses the same check in-process."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_shop_key
from app.db.session import get_session
from app.guard.check import GuardContext, check

router = APIRouter(prefix="/v1/guard", tags=["guard"])


class CheckIn(BaseModel):
    sku: str
    method: Literal["card", "wallet"]
    fingerprint_hash: str = Field(min_length=8, max_length=64, description="Shop-side hash of device/IP/UA")
    challenge_token: str | None = None


class CheckOut(BaseModel):
    decision: str
    reason: str
    incident_id: str | None
    sca_method: str | None = Field(
        description="Set payment_source.card.attributes.verification.method to this"
    )


@router.post("/check")
async def guard_check(
    body: CheckIn,
    shop_id: Annotated[str, Depends(require_shop_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CheckOut:
    v = await check(
        session, GuardContext(shop_id, body.sku, body.method, body.fingerprint_hash, body.challenge_token)
    )
    return CheckOut(
        decision=v.decision,
        reason=v.reason,
        incident_id=v.incident_id,
        sca_method="SCA_ALWAYS" if v.decision == "allow_with_sca" else None,
    )
