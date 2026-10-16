"""Live event bus over Postgres LISTEN/NOTIFY.

The API and the worker are separate processes; both publish with `publish()` (delivered when the
caller's transaction commits) and the API's SSE endpoint fans events out to dashboards.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

CHANNEL = "cg_events"
log = structlog.get_logger()


async def publish(session: AsyncSession, kind: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"kind": kind, "data": data}, default=str)
    if len(payload) > 7900:  # NOTIFY payload limit is 8000 bytes
        payload = json.dumps({"kind": kind, "data": {"id": data.get("id"), "truncated": True}})
    await session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})


class Broadcaster:
    """One LISTEN connection per API process, fanned out to any number of SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        try:
            self._conn = await asyncpg.connect(dsn)
            await self._conn.add_listener(CHANNEL, self._on_notify)
        except Exception as exc:  # dashboard streaming degrades; the API keeps working
            log.warning("broadcaster_unavailable", error=str(exc))

    async def stop(self) -> None:
        if self._conn:
            await self._conn.close()

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        for q in list(self._subscribers):
            if q.qsize() < 500:
                q.put_nowait(payload)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)
