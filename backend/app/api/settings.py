from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import OwnerShop
from app.db.audit import audit
from app.db.models import MerchantSettings
from app.db.session import get_session
from app.events import publish
from app.guard.check import invalidate_cache

router = APIRouter(prefix="/settings", tags=["settings"])


class SaleWindow(BaseModel):
    start: datetime
    end: datetime
    note: str = Field(default="", max_length=200)

    @field_validator("start", "end")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return v


class SettingsBody(BaseModel):
    autopilot: bool
    kill_switch: bool
    max_auto_refund_per_txn: Decimal = Field(ge=0, le=1000)
    max_auto_refund_per_incident: Decimal = Field(ge=0, le=10000)
    # Optional for older clients: across incidents, rolling 24 h.
    max_auto_refund_per_day: Decimal = Field(default=Decimal("250.00"), ge=0, le=100000)
    deescalate_after_min: int = Field(ge=1, le=240)
    # Payment hold for first-time shoppers (app/actions/holds.py); 0 captures at once. Optional.
    review_hold_min: int = Field(default=10, ge=0, le=1440)
    sale_windows: list[SaleWindow] = []


def _out(ms: MerchantSettings) -> SettingsBody:
    return SettingsBody(
        autopilot=ms.autopilot,
        kill_switch=ms.kill_switch,
        max_auto_refund_per_txn=ms.max_auto_refund_per_txn,
        max_auto_refund_per_incident=ms.max_auto_refund_per_incident,
        max_auto_refund_per_day=ms.max_auto_refund_per_day,
        deescalate_after_min=ms.deescalate_after_min,
        review_hold_min=ms.review_hold_min,
        sale_windows=[SaleWindow.model_validate(w) for w in ms.sale_windows or []],
    )


@router.get("")
async def get_settings_(
    shop_id: OwnerShop, session: Annotated[AsyncSession, Depends(get_session)]
) -> SettingsBody:
    ms = await session.get(MerchantSettings, shop_id)
    assert ms is not None
    return _out(ms)


@router.put("")
async def put_settings(
    body: SettingsBody, shop_id: OwnerShop, session: Annotated[AsyncSession, Depends(get_session)]
) -> SettingsBody:
    ms = await session.get(MerchantSettings, shop_id)
    assert ms is not None
    ms.autopilot = body.autopilot
    ms.kill_switch = body.kill_switch
    ms.max_auto_refund_per_txn = body.max_auto_refund_per_txn
    ms.max_auto_refund_per_incident = body.max_auto_refund_per_incident
    ms.max_auto_refund_per_day = body.max_auto_refund_per_day
    ms.deescalate_after_min = body.deescalate_after_min
    ms.review_hold_min = body.review_hold_min
    ms.sale_windows = [w.model_dump(mode="json") for w in body.sale_windows]
    audit(session, "owner", "settings.updated", **body.model_dump(mode="json"))
    await session.commit()
    invalidate_cache(shop_id)
    return _out(ms)


class SwitchesBody(BaseModel):
    """The War Room toggles: change one switch without re-sending (and racing) the whole settings form."""

    autopilot: bool | None = None
    kill_switch: bool | None = None


@router.patch("/switches")
async def patch_switches(
    body: SwitchesBody, shop_id: OwnerShop, session: Annotated[AsyncSession, Depends(get_session)]
) -> SettingsBody:
    ms = await session.get(MerchantSettings, shop_id, with_for_update=True)
    assert ms is not None
    changed = body.model_dump(exclude_none=True)
    for field, value in changed.items():
        setattr(ms, field, value)
    if changed:
        audit(session, "owner", "settings.switches", **changed)
        await publish(session, "settings.updated", changed)
    await session.commit()
    invalidate_cache(shop_id)
    return _out(ms)
