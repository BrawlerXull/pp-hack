"""Live event bus over Postgres LISTEN/NOTIFY.

The API and the worker are separate processes; both publish with `publish()` (delivered when the
caller's transaction commits) and the API's SSE endpoint fans events out to dashboards.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

import asyncpg
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

CHANNEL = "cg_events"
# not_started: no lifespan ran (tests); listening: events flow; reconnecting: lost, retrying with backoff.
StreamState = Literal["not_started", "listening", "reconnecting", "stopped"]
log = structlog.get_logger()


async def publish(session: AsyncSession, kind: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"kind": kind, "data": data}, default=str)
    if len(payload) > 7900:  # NOTIFY payload limit is 8000 bytes
        payload = json.dumps({"kind": kind, "data": {"id": data.get("id"), "truncated": True}})
    await session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})


class Broadcaster:
    """One LISTEN connection per API process, fanned out to any number of SSE subscribers.

    The connection is supervised: a termination listener notices Postgres closing it (restart, failover,
    idle kill) and a periodic ping notices a silently dead socket; either way it reconnects with backoff.
    Without that the dashboard would stay "live" but frozen until the API restarted. Events published
    while disconnected are lost (NOTIFY is not queued), so subscribers get a `stream.resumed` event and
    the dashboard refetches.
    """

    PING_EVERY_S = 30.0
    PING_TIMEOUT_S = 5.0
    BACKOFF_FIRST_S = 1.0
    BACKOFF_MAX_S = 30.0

    def __init__(self, connect: Callable[[str], Awaitable[Any]] | None = None) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._conn: Any = None  # asyncpg.Connection
        self._connect = connect or asyncpg.connect
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.state: StreamState = "not_started"
        self.reconnects = 0
        self._connected_before = False

    async def start(self) -> None:
        """One attempt now (so /healthz is right from the first request), then the supervisor keeps it up."""
        try:
            await self._open()
        except Exception as exc:  # dashboard streaming degrades; the API keeps working
            self.state = "reconnecting"
            log.warning("broadcaster_unavailable", error=str(exc))
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        self.state = "stopped"
        if self._task:
            self._task.cancel()  # it may be waiting on a connect attempt
            with suppress(asyncio.CancelledError):
                await self._task
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close()

    async def _open(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await self._connect(dsn)
        try:
            await conn.add_listener(CHANNEL, self._on_notify)
        except BaseException:
            conn.terminate()
            raise
        conn.add_termination_listener(self._on_terminated)
        resumed, self._connected_before = self._connected_before, True
        self._conn, self.state = conn, "listening"
        if resumed:
            self.reconnects += 1
            log.info("broadcaster_reconnected", reconnects=self.reconnects)
            self._fan_out(json.dumps({"kind": "stream.resumed", "data": {}}))

    def _drop(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            self._conn.terminate()
        self._conn = None
        if not self._stopping:
            self.state = "reconnecting"

    def _on_terminated(self, conn: Any) -> None:
        if self._stopping or conn is not self._conn:
            return
        log.warning("broadcaster_connection_lost")
        self._drop()
        self._wake.set()

    async def _supervise(self) -> None:
        delay = self.BACKOFF_FIRST_S
        while not self._stopping:
            # Cleared before the work, not after: a termination during the ping must still wake the wait.
            self._wake.clear()
            timeout = self.PING_EVERY_S
            if self._conn is None:
                try:
                    await self._open()
                    delay = self.BACKOFF_FIRST_S
                except Exception as exc:
                    log.warning("broadcaster_reconnect_failed", error=str(exc)[:200], retry_in_s=delay)
                    timeout, delay = delay, min(delay * 2, self.BACKOFF_MAX_S)
            else:
                try:
                    await asyncio.wait_for(self._conn.fetchval("SELECT 1"), self.PING_TIMEOUT_S)
                except Exception as exc:
                    log.warning("broadcaster_ping_failed", error=type(exc).__name__)
                    self._drop()
                    continue
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        self._fan_out(payload)

    def _fan_out(self, payload: str) -> None:
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
