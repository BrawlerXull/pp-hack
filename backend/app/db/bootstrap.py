"""Idempotent first-run setup: the demo shop, its settings and its guard state."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GuardState, MerchantSettings, Shop


async def ensure_shop(session: AsyncSession, shop_id: str, name: str = "Wick & Wax") -> None:
    if await session.get(Shop, shop_id) is None:
        session.add(Shop(id=shop_id, name=name))
        await session.flush()
    if await session.get(MerchantSettings, shop_id) is None:
        session.add(MerchantSettings(shop_id=shop_id, sale_windows=[]))
    if await session.get(GuardState, shop_id) is None:
        session.add(GuardState(shop_id=shop_id, held_skus=[]))
    await session.commit()
