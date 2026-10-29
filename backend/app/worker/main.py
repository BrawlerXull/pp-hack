"""Background worker: webhook processing, the detector loop, expiring device blocks, and re-sending
refunds left in flight (crash or lost PayPal answer) with their original PayPal-Request-Id.

With SIMULATOR_ENABLED it also keeps the shared demo usable (app/simulator/janitor.py): re-seeding the
normal history before it ages out, and standing an idle demo down to its defaults.

It also beats a heartbeat (app/worker/heartbeat.py) the dashboard shows on every page.

Run: `uv run python -m app.worker.main`. A Postgres advisory lock makes sure only one worker is
active, so a second instance (e.g. during a deploy) waits instead of double-acting.
"""

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

from app.actions.executor import Executor
from app.actions.holds import Holds
from app.ai import breaker
from app.config import get_settings
from app.db.bootstrap import ensure_shop
from app.db.session import get_engine, get_sessionmaker
from app.disputes import advisor
from app.guard import devices
from app.guard.check import invalidate_cache
from app.incidents import observer
from app.incidents.service import IncidentService, release_assessment_claims
from app.incidents.webhook_processor import process_pending
from app.log_config import configure_logging
from app.paypal.client import PayPalClient
from app.simulator import janitor
from app.worker import heartbeat

LOCK_KEY = 0x0C6E_A12D  # arbitrary, stable advisory-lock id
JANITOR_INTERVAL_S = 60.0
WEBHOOK_BATCH = 100  # process_pending's default batch
WEBHOOK_BATCHES_PER_TICK = 20
log = structlog.get_logger()


async def run() -> None:
    configure_logging()
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    paypal = PayPalClient(settings)
    sessions = get_sessionmaker()
    async with get_engine().connect() as lock_conn:
        log.info("worker_waiting_for_lock")
        await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        await lock_conn.commit()
        log.info("worker_started", shop=settings.shop_id, interval_s=settings.detect_interval_s)
        async with sessions() as s:
            await ensure_shop(s, settings.shop_id)
            await release_assessment_claims(s)
        await breaker.reset_until_restart()
        state: dict[str, Any] = {}
        beating = asyncio.create_task(heartbeat.run(sessions, datetime.now(UTC), state, stop))
        next_janitor = 0.0

        while not stop.is_set():
            try:
                # Drain the webhook queue (bounded): an observer-mode burst can bring hundreds of events.
                for _ in range(WEBHOOK_BATCHES_PER_TICK):
                    async with sessions() as s:
                        if await process_pending(s) < WEBHOOK_BATCH:
                            break
                async with sessions() as s:
                    await observer.enrich(s, paypal, settings.shop_id)  # orders PayPal sent no event for
                async with sessions() as s:
                    await advisor.open_new_cases(s, settings.shop_id)
                async with sessions() as s:
                    if await devices.prune_expired(s, settings.shop_id, datetime.now(UTC)):
                        invalidate_cache(settings.shop_id)  # this process only; the API filters by expiry
                    await s.commit()
                async with sessions() as s:
                    await Executor(s, paypal).sweep_in_flight(settings.shop_id)
                async with sessions() as s:
                    await Holds(Executor(s, paypal)).settle(settings.shop_id)
                async with sessions() as s:
                    svc = IncidentService(s, paypal, timedelta(seconds=settings.reassess_interval_s))
                    await svc.tick(settings.shop_id)
                state["live"] = svc.live
                if settings.simulator_enabled and loop.time() >= next_janitor:
                    next_janitor = loop.time() + JANITOR_INTERVAL_S
                    async with sessions() as s:
                        await janitor.reseed_if_stale(s, settings.shop_id, datetime.now(UTC), settings)
                    async with sessions() as s:
                        await janitor.restore_if_idle(
                            s, paypal, settings.shop_id, datetime.now(UTC), settings
                        )
                state["last_tick_at"] = datetime.now(UTC)
            except Exception as exc:
                log.exception("worker_tick_failed")
                # The heartbeat keeps beating while every pass fails: say so, or the pill stays green.
                state["last_tick_error_at"] = datetime.now(UTC)
                state["last_tick_error"] = type(exc).__name__
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.detect_interval_s)
            except TimeoutError:
                pass

        await beating
    await paypal.aclose()
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
