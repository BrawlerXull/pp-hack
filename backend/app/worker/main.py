"""Background worker: webhook processing + the detector loop.

Run: `uv run python -m app.worker.main`. A Postgres advisory lock makes sure only one worker is
active, so a second instance (e.g. during a deploy) waits instead of double-acting.
"""

import asyncio
import signal
from datetime import timedelta

import structlog
from sqlalchemy import text

from app.ai import breaker
from app.config import get_settings
from app.db.bootstrap import ensure_shop
from app.db.session import get_engine, get_sessionmaker
from app.incidents.service import IncidentService, release_assessment_claims
from app.incidents.webhook_processor import process_pending
from app.log_config import configure_logging
from app.paypal.client import PayPalClient

LOCK_KEY = 0x0C6E_A12D  # arbitrary, stable advisory-lock id
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

        while not stop.is_set():
            try:
                async with sessions() as s:
                    await process_pending(s)
                async with sessions() as s:
                    svc = IncidentService(s, paypal, timedelta(seconds=settings.reassess_interval_s))
                    await svc.tick(settings.shop_id)
            except Exception:
                log.exception("worker_tick_failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.detect_interval_s)
            except TimeoutError:
                pass

    await paypal.aclose()
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(run())
