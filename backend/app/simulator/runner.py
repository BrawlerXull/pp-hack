"""Runs a scenario through the real shop order flow (in-process for the demo buttons)."""

import asyncio
import inspect
import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from app.db.models import Outcome, Source
from app.db.session import get_sessionmaker
from app.paypal.client import PayPalClient
from app.paypal.orders import CardDetails
from app.shop.service import CheckoutRequest, OfflinePlan, place_order
from app.simulator import observer_feed
from app.simulator.scenarios import OFFLINE_ONLY, SCENARIOS, Mode, SimAttempt

# "observer": the scenario arrives as PayPal webhooks from a checkout that does not call the guard
# (app/simulator/observer_feed.py), offline and labelled simulated.
RunMode = Mode | Literal["observer"]

log = structlog.get_logger()


@dataclass
class SimRun:
    id: str
    scenario: str
    mode: RunMode
    total: int
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    done: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    error: str | None = None
    cancelled: bool = False
    seed: int | None = None
    # What was already in place when the run started (open incident, 3-D Secure, held items, blocked
    # devices): explains a run whose attempts never reached PayPal.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def stopped_by_guard(self) -> int:
        return self.outcomes[Outcome.BLOCKED] + self.outcomes[Outcome.CHALLENGED]

    @property
    def reached_paypal(self) -> int:
        return self.done - self.stopped_by_guard


RUNS: dict[str, SimRun] = {}
_TASKS: set[asyncio.Task[None]] = set()  # keep references so runs aren't garbage-collected


def active_run() -> SimRun | None:
    """A run still placing orders. A stopped (cancelled) one no longer counts, though its loop may place
    one more order before it notices."""
    return next((r for r in RUNS.values() if r.finished_at is None and not r.cancelled), None)


def to_request(shop_id: str, scenario: str, a: SimAttempt, mode: Mode) -> CheckoutRequest:
    card = (
        CardDetails(number=a.card_number, name=a.card_name or "Test Buyer", expiry=a.card_expiry)
        if a.card_number
        else None
    )
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
    mode: RunMode,
    paypal: PayPalClient | None,
    count: int | None = None,
    speed: float = 1.0,
    seed: int | None = None,
    context: dict[str, Any] | None = None,
) -> SimRun:
    """Demo runs vary: a fresh seed each time (reported, so a run can be replayed exactly with the same
    seed), a +/-30% attempt count unless one is given, and a fresh attacker profile in the scenario."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if mode == "live" and scenario in OFFLINE_ONLY:
        raise ValueError(f"{scenario} can only run offline (simulated)")
    fn = SCENARIOS[scenario]
    seed = seed if seed is not None else random.SystemRandom().randrange(1, 10**6)
    if not count:
        default = inspect.signature(fn).parameters["count"].default
        count = max(1, round(default * random.Random(seed ^ 0x5EED).uniform(0.7, 1.3)))
    attempts = list(
        fn("offline" if mode == "observer" else mode, count=count, seed=seed, speed=speed, vary=True)
    )
    run = SimRun(
        id=uuid.uuid4().hex[:10],
        scenario=scenario,
        mode=mode,
        total=len(attempts),
        seed=seed,
        context=context or {},
    )
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
                if run.mode == "observer":
                    outcome = await observer_feed.deliver(s, a)
                    await s.commit()
                else:
                    outcome = (
                        await place_order(s, paypal, to_request(shop_id, run.scenario, a, run.mode))
                    ).outcome
            run.done += 1
            run.outcomes[outcome] += 1
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
        log.exception("sim_run_failed", run=run.id)
    finally:
        run.finished_at = datetime.now(UTC)
        log.info("sim_run_finished", run=run.id, scenario=run.scenario, outcomes=dict(run.outcomes))
