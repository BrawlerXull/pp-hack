"""Idempotent first-run setup: the demo shop, its settings, its guard state and its demo owner account."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import GuardState, MerchantSettings, Role, Shop, User


async def ensure_shop(session: AsyncSession, shop_id: str, name: str = "Wick & Wax") -> None:
    if await session.get(Shop, shop_id) is None:
        session.add(Shop(id=shop_id, name=name))
        await session.flush()
    if await session.get(MerchantSettings, shop_id) is None:
        session.add(MerchantSettings(shop_id=shop_id, sale_windows=[]))
    if await session.get(GuardState, shop_id) is None:
        session.add(GuardState(shop_id=shop_id, held_skus=[]))
    demo_email = get_settings().demo_email.lower()
    if (await session.execute(select(User.id).where(User.email == demo_email))).first() is None:
        # No password hash: the demo account signs in with ADMIN_PASSWORD (app/api/auth.py).
        session.add(User(shop_id=shop_id, email=demo_email, name="Demo owner", role=Role.OWNER, is_demo=True))
    await session.commit()
