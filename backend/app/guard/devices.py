"""Device block and trust lists (table device_entries).

Blocks expire after BLOCK_TTL: a fingerprint is a hash of a client-supplied device id and user agent, so a
shared or reused one must not stay locked out for good, and a bot that comes back after a day is caught
again by detection anyway. While an incident still sees the device attacking, enforcement re-blocks it.

Trust is the owner's "this device is a real shopper" (POST /dashboard/devices/{fp}/unblock). A trusted
device is left out of the attack signature and of refund matching, so the next enforcement pass (every few
seconds) does not block it again or refund its purchases.

Writers (executor, unblock, reset) hold the GuardState row lock, so check-then-insert never races.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import DeviceEntry, DeviceListKind, Incident

BLOCK_TTL = timedelta(hours=24)
TRUST_TTL = timedelta(days=30)


def _active(shop_id: str, kind: DeviceListKind, now: datetime | None = None):
    return (
        DeviceEntry.shop_id == shop_id,
        DeviceEntry.kind == kind,
        DeviceEntry.released_at.is_(None),
        DeviceEntry.expires_at > (now if now is not None else func.now()),
    )


async def active(
    session: AsyncSession, shop_id: str, kind: DeviceListKind, now: datetime | None = None
) -> frozenset[str]:
    q = select(DeviceEntry.fingerprint).where(*_active(shop_id, kind, now))
    return frozenset((await session.execute(q)).scalars())


async def blocked(session: AsyncSession, shop_id: str, now: datetime | None = None) -> frozenset[str]:
    return await active(session, shop_id, DeviceListKind.BLOCK, now)


async def trusted(session: AsyncSession, shop_id: str, now: datetime | None = None) -> frozenset[str]:
    return await active(session, shop_id, DeviceListKind.TRUST, now)


async def blocked_as_strong(
    session: AsyncSession, shop_id: str, now: datetime, exclude_incident_id: str
) -> frozenset[str]:
    """Blocked devices whose block is strong evidence on its own: put there by ANOTHER incident whose
    signature saw them as unmistakable bots (strong_fingerprints). An incident's own blocks never count:
    under an attack verdict it blocks every flagged device, weak flags included, and blocking one must not
    by itself make its payments refundable without the owner."""
    q = (
        select(DeviceEntry.fingerprint, Incident.signature["strong_fingerprints"])
        .join(Incident, Incident.id == DeviceEntry.incident_id)
        .where(*_active(shop_id, DeviceListKind.BLOCK, now), DeviceEntry.incident_id != exclude_incident_id)
    )
    return frozenset(fp for fp, strong in (await session.execute(q)).all() if fp in (strong or []))


async def count_blocked(session: AsyncSession, shop_id: str) -> int:
    q = select(func.count()).select_from(DeviceEntry).where(*_active(shop_id, DeviceListKind.BLOCK))
    return int((await session.execute(q)).scalar_one())


async def listing(session: AsyncSession, shop_id: str) -> list[DeviceEntry]:
    q = (
        select(DeviceEntry)
        .where(
            DeviceEntry.shop_id == shop_id,
            DeviceEntry.released_at.is_(None),
            DeviceEntry.expires_at > func.now(),
        )
        .order_by(DeviceEntry.created_at.desc())
    )
    return list((await session.execute(q)).scalars())


async def prune_expired(session: AsyncSession, shop_id: str, now: datetime) -> list[str]:
    """Release entries past their expiry, auditing each expired block (block.expired, per incident).
    Caller commits. Run before adding entries, so the one-open-entry index only ever sees live ones."""
    rows = (
        await session.execute(
            update(DeviceEntry)
            .where(
                DeviceEntry.shop_id == shop_id,
                DeviceEntry.released_at.is_(None),
                DeviceEntry.expires_at <= now,
            )
            .values(released_at=now)
            .returning(DeviceEntry.fingerprint, DeviceEntry.kind, DeviceEntry.incident_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    by_incident: dict[str | None, list[str]] = defaultdict(list)
    for fp, kind, incident_id in rows:
        if kind == DeviceListKind.BLOCK:
            by_incident[incident_id].append(fp)
    for incident_id, fps in by_incident.items():
        audit(session, "system", "block.expired", incident_id, fingerprints=sorted(fps), count=len(fps))
    return sorted(fp for fps in by_incident.values() for fp in fps)


async def block(
    session: AsyncSession,
    shop_id: str,
    fingerprints: list[str],
    incident_id: str | None,
    reasons: dict[str, str],
    now: datetime,
) -> list[str]:
    """Block the given devices for BLOCK_TTL; returns the ones newly blocked. Trusted and already-blocked
    devices are skipped. Caller holds the GuardState lock and commits."""
    await prune_expired(session, shop_id, now)
    skip = await blocked(session, shop_id, now) | await trusted(session, shop_id, now)
    new = sorted(set(fingerprints) - skip)
    session.add_all(
        DeviceEntry(
            shop_id=shop_id,
            fingerprint=fp,
            kind=DeviceListKind.BLOCK,
            incident_id=incident_id,
            reason=reasons.get(fp),
            created_at=now,
            expires_at=now + BLOCK_TTL,
        )
        for fp in new
    )
    await session.flush()
    return new


async def trust(session: AsyncSession, shop_id: str, fingerprint: str, now: datetime, reason: str) -> bool:
    """Release the device's block (if any) and trust it. Returns whether it was blocked. Caller holds the
    GuardState lock and commits."""
    await prune_expired(session, shop_id, now)
    released = (
        await session.execute(
            update(DeviceEntry)
            .where(DeviceEntry.fingerprint == fingerprint, *_active(shop_id, DeviceListKind.BLOCK, now))
            .values(released_at=now)
            .returning(DeviceEntry.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    if fingerprint not in await trusted(session, shop_id, now):
        session.add(
            DeviceEntry(
                shop_id=shop_id,
                fingerprint=fingerprint,
                kind=DeviceListKind.TRUST,
                reason=reason,
                created_at=now,
                expires_at=now + TRUST_TTL,
            )
        )
        await session.flush()
    return bool(released)


async def release_all(session: AsyncSession, shop_id: str, now: datetime, kinds: tuple[str, ...]) -> None:
    await session.execute(
        update(DeviceEntry)
        .where(
            DeviceEntry.shop_id == shop_id,
            DeviceEntry.kind.in_(kinds),
            DeviceEntry.released_at.is_(None),
        )
        .values(released_at=now)
        .execution_options(synchronize_session=False)
    )
