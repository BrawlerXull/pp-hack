"""Shop API keys for the public guard API (X-Guard-Key). A key is `cg_sk_` plus 32 random URL-safe characters;
the database keeps only its SHA-256, so a leaked table gives no usable key. SHA-256 rather than a slow
password hash: the key is long and random (nothing to brute-force), and it is checked on every checkout."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey

PREFIX = "cg_sk_"
SHOWN = len(PREFIX) + 4  # how much of a key the dashboard shows: "cg_sk_Ab3x…"
# last_used_at is written at most this often per key: a busy checkout should not write on every check.
TOUCH_EVERY = timedelta(seconds=30)


def generate() -> str:
    return PREFIX + secrets.token_urlsafe(24)


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def lookup(session: AsyncSession, key: str) -> ApiKey | None:
    """The live (not revoked) key, with last_used_at brought up to date."""
    if not key.startswith(PREFIX):
        return None
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == digest(key), ApiKey.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is not None:
        now = datetime.now(UTC)
        if row.last_used_at is None or now - row.last_used_at > TOUCH_EVERY:
            row.last_used_at = now
            await session.commit()
    return row
