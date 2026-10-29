from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import push
from app.api.deps import Member
from app.api.ratelimit import limit
from app.config import get_settings
from app.db.models import PUSH_KINDS, PushDevice
from app.db.session import get_session

router = APIRouter(prefix="/push", tags=["push"])
Session = Annotated[AsyncSession, Depends(get_session)]


class DeviceIn(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    label: str = Field(default="", max_length=120)


class KindsIn(BaseModel):
    kinds: list[str]


@router.get("/config")
async def config(user: Member, session: Session) -> dict:
    """What the browser needs to subscribe (all public values), and this member's devices and choices."""
    web = push.web_config()
    devices = (await session.execute(select(PushDevice).where(PushDevice.user_id == user.id))).scalars().all()
    return {
        "enabled": bool(web and get_settings().firebase_vapid_key and push.configured()),
        "firebase": web,
        "vapid_key": get_settings().firebase_vapid_key or None,
        "kinds": user.push_kinds or list(PUSH_KINDS),
        "devices": [{"id": d.id, "label": d.label, "created_at": d.created_at} for d in devices],
    }


@router.post("/devices")
async def add_device(body: DeviceIn, user: Member, session: Session) -> dict:
    """Upsert by token: a browser that switches account moves its token to the new member."""
    device = (
        await session.execute(select(PushDevice).where(PushDevice.token == body.token))
    ).scalar_one_or_none()
    if device is None:
        device = PushDevice(user_id=user.id, token=body.token, label=body.label)
        session.add(device)
    else:
        device.user_id, device.label, device.last_seen_at = user.id, body.label, datetime.now(UTC)
    await session.commit()
    return {"ok": True, "id": device.id}


@router.post("/devices/remove")
async def remove_device(body: DeviceIn, user: Member, session: Session) -> dict:
    await session.execute(
        delete(PushDevice).where(PushDevice.token == body.token, PushDevice.user_id == user.id)
    )
    await session.commit()
    return {"ok": True}


@router.put("/kinds")
async def set_kinds(body: KindsIn, user: Member, session: Session) -> dict:
    if unknown := set(body.kinds) - set(PUSH_KINDS):
        raise HTTPException(422, f"unknown kinds: {sorted(unknown)}")
    user.push_kinds = [k for k in PUSH_KINDS if k in body.kinds]
    await session.commit()
    return {"kinds": user.push_kinds}


@router.post("/test", dependencies=[Depends(limit("push-test", 5, 60))])
async def test(user: Member) -> dict:
    if not push.configured():
        raise HTTPException(409, "Push isn't set up on the server (FIREBASE_SERVICE_ACCOUNT)")
    out = await push.deliver(
        "test",
        "Checkout Guard notifications are on",
        "You'll be told here when an attack starts, a payment needs you, or a dispute opens.",
        "/guard",
        "test",
        user_ids=[user.id],
    )
    if not out["sent"]:
        raise HTTPException(502, "No device accepted the notification. Turn notifications off and on again.")
    return out
