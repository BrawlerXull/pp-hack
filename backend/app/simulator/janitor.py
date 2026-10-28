"""Keeps the shared, hosted demo usable for every visitor until judging ends. Worker job, only with
SIMULATOR_ENABLED=true (a real shop has real history and nobody else's demo runs).

  - Re-seed: the seeded "normal" history is anchored to the day it was made. Left alone it ages: after a
    week the baseline sees a week of empty hours (ordinary traffic starts to look like a spike), and after
    28 days it is gone. When its newest day is DEMO_RESEED_AFTER_H old (or there is none), the same
    28 days are generated again ending yesterday. Same customers (fixed random seed), so the shoppers the
    guard knows stay the same.
  - Idle stand-down: one judge's attack leaves 3-D Secure on, devices blocked and maybe settings changed;
    the next judge's attack is then stopped before PayPal and looks broken. After DEMO_IDLE_RESTORE_MIN
    with no checkout attempt and no owner action, the open incident is closed (its report is kept), the
    defences come off and the settings return to their defaults. Nothing is deleted: that is /demo/reset.
"""

import time
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.audit import audit
from app.db.models import (
    AuditLog,
    CheckoutAttempt,
    DeviceListKind,
    GuardMode,
    GuardState,
    MerchantSettings,
    Source,
)
from app.events import publish
from app.guard import devices
from app.guard.check import invalidate_cache
from app.incidents.service import IncidentService, open_incident
from app.paypal.client import PayPalClient
from app.simulator.seed import seed_history

log = structlog.get_logger()

RESEED_MIN_INTERVAL_S = 6 * 3600  # never re-seed in a loop, whatever the newest seeded row says
IDLE_RESTORE_RULE = "demo_idle_restore"

_last_reseed: dict[str, float] = {}


def _default(column: str) -> object:
    return MerchantSettings.__table__.c[column].default.arg


async def reseed_if_stale(
    session: AsyncSession, shop_id: str, now: datetime, settings: Settings
) -> int | None:
    """Re-seeds and returns the number of rows when the seeded history is missing or too old."""
    if settings.demo_reseed_after_h <= 0:
        return None
    if time.monotonic() - _last_reseed.get(shop_id, -RESEED_MIN_INTERVAL_S) < RESEED_MIN_INTERVAL_S:
        return None
    newest = (
        await session.execute(
            select(func.max(CheckoutAttempt.ts)).where(
                CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.source == Source.SEED
            )
        )
    ).scalar()
    if newest is not None and now - newest < timedelta(hours=settings.demo_reseed_after_h):
        return None
    rows = await seed_history(session, shop_id)  # commits
    _last_reseed[shop_id] = time.monotonic()
    audit(session, "system", "demo.history_reseeded", rows=rows, previous_newest=newest)
    await session.commit()
    log.info("demo_history_reseeded", shop=shop_id, rows=rows, previous_newest=newest)
    return rows


async def last_activity(session: AsyncSession, shop_id: str) -> datetime | None:
    """The latest checkout attempt (not seeded history) or owner action."""
    attempt = (
        await session.execute(
            select(func.max(CheckoutAttempt.ts)).where(
                CheckoutAttempt.shop_id == shop_id, CheckoutAttempt.source != Source.SEED
            )
        )
    ).scalar()
    owner = (await session.execute(select(func.max(AuditLog.ts)).where(AuditLog.actor == "owner"))).scalar()
    return max((t for t in (attempt, owner) if t is not None), default=None)


async def restore_if_idle(
    session: AsyncSession, paypal: PayPalClient, shop_id: str, now: datetime, settings: Settings
) -> list[str]:
    """Stands the demo down after the idle time; returns what it restored (empty: nothing to do). A demo run
    lives in the API process, not here, but a running scenario keeps adding attempts, so it is never idle."""
    if settings.demo_idle_restore_min <= 0:
        return []
    last = await last_activity(session, shop_id)
    if last is not None and now - last < timedelta(minutes=settings.demo_idle_restore_min):
        return []

    restored: list[str] = []
    inc = await open_incident(session, shop_id, lock=True)
    if inc is not None:
        # The normal stand-down: restores the checkout, records DEESCALATE, writes the final report.
        await IncidentService(session, paypal, timedelta(seconds=settings.reassess_interval_s)).close(
            inc, IDLE_RESTORE_RULE
        )
        restored.append("incident closed")

    gs = await session.get(GuardState, shop_id, with_for_update=True, populate_existing=True)
    if gs is not None and (gs.mode != GuardMode.NORMAL or gs.held_skus):
        gs.mode, gs.held_skus, gs.set_by_incident = GuardMode.NORMAL, [], None
        gs.updated_at = now
        restored.append("defences off")
    if await devices.count_blocked(session, shop_id):
        await devices.release_all(session, shop_id, now, (DeviceListKind.BLOCK,))
        restored.append("devices unblocked")

    ms = await session.get(MerchantSettings, shop_id, with_for_update=True, populate_existing=True)
    if ms is not None:
        defaults = {
            "kill_switch": False,
            "autopilot": True,
            "sale_windows": [],
            "max_auto_refund_per_txn": _default("max_auto_refund_per_txn"),
            "max_auto_refund_per_incident": _default("max_auto_refund_per_incident"),
            "max_auto_refund_per_day": _default("max_auto_refund_per_day"),
            "deescalate_after_min": _default("deescalate_after_min"),
        }
        if any(getattr(ms, k) != v for k, v in defaults.items()):
            for k, v in defaults.items():
                setattr(ms, k, v)
            restored.append("settings back to defaults")

    if not restored:
        await session.rollback()
        return []
    audit(session, "system", "demo.idle_restore", restored=restored, idle_min=settings.demo_idle_restore_min)
    await publish(session, "demo.restored", {"restored": restored})
    await session.commit()
    invalidate_cache(shop_id)
    log.info("demo_idle_restore", shop=shop_id, restored=restored, last_activity=last)
    return restored
