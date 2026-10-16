"""Runs a scenario through the real shop order flow (in-process for the demo buttons)."""

import asyncio
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from app.db.models import Source
from app.db.session import get_sessionmaker
from app.paypal.client import PayPalClient
from app.paypal.orders import CardDetails
from app.shop.service import CheckoutRequest, OfflinePlan, place_order
from app.simulator.scenarios import OFFLINE_ONLY, SCENARIOS, Mode, SimAttempt

log = structlog.get_logger()


@dataclass
class SimRun:
    id: str
    scenario: str
    mode: Mode
    total: int
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    done: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    error: str | None = None
    cancelled: bool = False


RUNS: dict[str, SimRun] = {}
_TASKS: set[asyncio.Task[None]] = set()  # keep references so runs aren't garbage-collected


def to_request(shop_id: str, scenario: str, a: SimAttempt, mode: Mode) -> CheckoutRequest:
    card = CardDetails(number=a.card_number, name=a.card_name or "Test Buyer") if a.card_number else None
    return CheckoutRequest(
        shop_id=shop_id,
        sku=a.sku,
        method=a.method,
        device_id=a.device_id,
        user_agent=a.user_agent,
        ip=None,
        email=a.email,
        source=Source.SIMULATOR,
        scenario=scenario,
        card=card,
        payer_id=a.payer_id,
        offline=OfflinePlan(a.outcome, a.decline_code, a.is_bot) if mode == "offline" else None,
    )


def start_run(
    shop_id: str,
    scenario: str,
    mode: Mode,
    paypal: PayPalClient | None,
    count: int | None = None,
    speed: float = 1.0,
    seed: int | None = None,
) -> SimRun:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if mode == "live" and scenario in OFFLINE_ONLY:
        raise ValueError(f"{scenario} can only run offline (simulated)")
    kwargs = {"seed": seed, "speed": speed} | ({"count": count} if count else {})
    attempts = list(SCENARIOS[scenario](mode, **kwargs))
    run = SimRun(id=uuid.uuid4().hex[:10], scenario=scenario, mode=mode, total=len(attempts))
    RUNS[run.id] = run
    task = asyncio.create_task(_execute(run, shop_id, attempts, paypal))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return run


async def _execute(
    run: SimRun, shop_id: str, attempts: list[SimAttempt], paypal: PayPalClient | None
) -> None:
    sessions = get_sessionmaker()
    try:
        for a in attempts:
            if run.cancelled:
                break
            await asyncio.sleep(a.delay_s)
            async with sessions() as s:
                res = await place_order(s, paypal, to_request(shop_id, run.scenario, a, run.mode))
            run.done += 1
            run.outcomes[res.outcome] += 1
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
        log.exception("sim_run_failed", run=run.id)
    finally:
        run.finished_at = datetime.now(UTC)
        log.info("sim_run_finished", run=run.id, scenario=run.scenario, outcomes=dict(run.outcomes))
