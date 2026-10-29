"""The hosted demo stays usable for every judge: the guard fails open when it is slow, the seeded history
is renewed before it ages out, an idle demo stands down to its defaults, and one visitor's changes are
labelled for the next."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select, update

from app import visitor
from app.api.deps import get_paypal, issue_session, require_owner
from app.config import get_settings
from app.db import session as db_session
from app.db.audit import audit
from app.db.models import (
    AuditLog,
    CheckoutAttempt,
    Decision,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
    Source,
)
from app.db.session import get_session
from app.guard import check as guard
from app.guard import devices
from app.main import app
from app.shop.service import CheckoutRequest, OfflinePlan, fingerprint, place_order
from app.simulator import janitor
from app.simulator.seed import seed_history
from tests.conftest import SHOP, blocked


def _order(device: str = "slow-1", method: str = "card") -> CheckoutRequest:
    return CheckoutRequest(
        shop_id=SHOP,
        sku="SKU-PILLAR-18",
        method=method,
        device_id=device,
        user_agent="Mozilla/5.0",
        ip=None,
        email="jo@gmail.com",
        offline=OfflinePlan("approve", is_bot=False),
    )


# ---- fail-open ---------------------------------------------------------------------------------------


async def test_a_stuck_guard_check_lets_the_payment_through_and_says_so(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "guard_check_budget_ms", 200)
    fp = fingerprint("slow-1", "Mozilla/5.0")
    # Another transaction holds this device's lock: the check would wait for it indefinitely.
    async with db_session.get_sessionmaker()() as other:
        await other.execute(select(func.pg_advisory_xact_lock(func.hashtext(SHOP), func.hashtext(fp))))
        res = await place_order(db, None, _order())
        await other.rollback()
    assert res.decision == Decision.ALLOW and res.outcome == Outcome.CAPTURED
    row = await db.get(CheckoutAttempt, res.attempt_id, populate_existing=True)
    assert row is not None and row.guard_reason == "guard_unavailable"


async def test_fail_open_keeps_the_defences_it_last_saw(db):
    guard.invalidate_cache()
    ctx = guard.GuardContext(SHOP, "SKU-PILLAR-18", "card", "f" * 64)
    assert guard.fail_open_verdict(ctx).reason == "guard_unavailable"  # nothing seen yet: allow
    gs = await db.get(GuardState, SHOP)
    gs.mode = GuardMode.SCA_ALL
    await devices.block(db, SHOP, ["b" * 64], None, {}, datetime.now(UTC))
    await db.commit()
    await guard.check(db, ctx)  # loads the state into this process's cache
    assert guard.fail_open_verdict(ctx).decision == Decision.ALLOW_WITH_SCA
    blocked_ctx = guard.GuardContext(SHOP, "SKU-PILLAR-18", "card", "b" * 64)
    assert guard.fail_open_verdict(blocked_ctx).decision == Decision.BLOCK
    wallet = guard.GuardContext(SHOP, "SKU-PILLAR-18", "wallet", "f" * 64)
    assert guard.fail_open_verdict(wallet).reason == "guard_unavailable"


# ---- re-seeding --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_janitor(monkeypatch):
    monkeypatch.setattr(janitor, "_last_reseed", {})
    monkeypatch.setattr(get_settings(), "demo_reseed_after_h", 36)
    monkeypatch.setattr(get_settings(), "demo_idle_restore_min", 30)


async def _newest_seed(db) -> datetime | None:
    q = select(func.max(CheckoutAttempt.ts)).where(CheckoutAttempt.source == Source.SEED)
    return (await db.execute(q)).scalar()


async def test_missing_history_is_seeded_and_fresh_history_is_left_alone(db):
    now = datetime.now(UTC)
    assert await janitor.reseed_if_stale(db, SHOP, now, get_settings())
    assert now - await _newest_seed(db) < timedelta(hours=36)
    janitor._last_reseed.clear()
    assert await janitor.reseed_if_stale(db, SHOP, now, get_settings()) is None


async def test_aged_history_is_renewed_to_end_yesterday(db):
    await seed_history(db, SHOP, days=28)
    await db.execute(update(CheckoutAttempt).values(ts=CheckoutAttempt.ts - timedelta(days=10)))
    await db.commit()
    now = datetime.now(UTC)
    assert now - await _newest_seed(db) > timedelta(days=10)
    rows = await janitor.reseed_if_stale(db, SHOP, now, get_settings())
    assert rows and now - await _newest_seed(db) < timedelta(hours=36)
    assert (
        await db.execute(select(func.count()).where(CheckoutAttempt.source == Source.SEED))
    ).scalar() == rows
    # Right after a re-seed it does not run again, whatever the rows say.
    assert await janitor.reseed_if_stale(db, SHOP, now + timedelta(days=5), get_settings()) is None


# ---- idle stand-down ---------------------------------------------------------------------------------


async def _leave_the_demo_messy(db, when: datetime) -> Incident:
    inc = Incident(shop_id=SHOP, opened_at=when, status=IncidentStatus.OPEN, classification="card_testing")
    db.add(inc)
    gs = await db.get(GuardState, SHOP)
    gs.mode, gs.held_skus = GuardMode.SCA_ALL, ["SKU-TEALIGHT-3"]
    await devices.block(db, SHOP, ["b" * 64], None, {}, when)
    await db.execute(
        update(MerchantSettings).values(
            kill_switch=True, autopilot=False, max_auto_refund_per_txn=Decimal("1")
        )
    )
    audit(db, "owner", "settings.updated", visitor="judge-a")
    await db.commit()
    await db.execute(update(AuditLog).values(ts=when))
    await db.commit()
    return inc


async def test_an_idle_demo_stands_down_to_its_defaults(db, monkeypatch):
    monkeypatch.setattr(janitor.IncidentService, "write_report", _no_report)
    inc = await _leave_the_demo_messy(db, datetime.now(UTC) - timedelta(minutes=45))
    restored = await janitor.restore_if_idle(db, None, SHOP, datetime.now(UTC), get_settings())
    # Closing the incident already restores the checkout (its DEESCALATE), so no separate "defences off".
    assert restored == ["incident closed", "devices unblocked", "settings back to defaults"]
    await db.refresh(inc)
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    ms = await db.get(MerchantSettings, SHOP, populate_existing=True)
    assert inc.status == IncidentStatus.CLOSED  # closed, not deleted: the report stays
    assert gs.mode == GuardMode.NORMAL and gs.held_skus == [] and await blocked(db) == []
    assert not ms.kill_switch and ms.autopilot and ms.max_auto_refund_per_txn == Decimal("10.00")
    # Nothing left to restore: a second pass does nothing.
    assert await janitor.restore_if_idle(db, None, SHOP, datetime.now(UTC), get_settings()) == []


async def test_defences_left_without_an_incident_are_switched_off_too(db):
    # conftest turns the review window off; here the settings start at their defaults.
    await db.execute(update(MerchantSettings).values(review_hold_min=10))
    gs = await db.get(GuardState, SHOP)
    gs.mode = GuardMode.SCA_ALL
    await db.commit()
    assert await janitor.restore_if_idle(db, None, SHOP, datetime.now(UTC), get_settings()) == [
        "defences off"
    ]


async def test_a_demo_in_use_is_left_alone(db, monkeypatch):
    monkeypatch.setattr(janitor.IncidentService, "write_report", _no_report)
    await _leave_the_demo_messy(db, datetime.now(UTC) - timedelta(minutes=5))
    assert await janitor.restore_if_idle(db, None, SHOP, datetime.now(UTC), get_settings()) == []


async def _no_report(self, incident, notify_owner=True):
    return None


# ---- another visitor's changes -----------------------------------------------------------------------


@pytest.fixture
async def client(db):
    async def session():
        yield db

    async def owner():
        return SHOP

    app.dependency_overrides.update({get_paypal: lambda: None, get_session: session, require_owner: owner})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_each_login_is_its_own_visitor_and_their_actions_are_tagged(db):
    settings = get_settings()
    a, b = issue_session(settings), issue_session(settings)

    class Req:
        def __init__(self, token):
            self.cookies = {"cg_session": token}

    await require_owner(Req(a), settings)  # type: ignore[arg-type]
    first = visitor.current()
    await require_owner(Req(b), settings)  # type: ignore[arg-type]
    assert first and visitor.current() and first != visitor.current()
    audit(db, "owner", "demo.reset")
    audit(db, "system", "incident.closed")
    await db.commit()
    rows = {r.event: r.data for r in (await db.execute(select(AuditLog))).scalars()}
    assert rows["demo.reset"]["visitor"] == visitor.current() and "visitor" not in rows["incident.closed"]


async def test_the_summary_names_another_visitors_change_but_not_your_own(client, db):
    visitor.set_current("judge-a")
    audit(db, "owner", "demo.run_started", scenario="card_testing", mode="offline")
    await db.commit()

    visitor.set_current("judge-b")
    other = (await client.get("/dashboard/summary")).json()["demo_activity"]
    assert other["kind"] == "other_visitor" and other["event"] == "demo.run_started"
    assert other["data"] == {"scenario": "card_testing", "mode": "offline"} and "visitor" not in other["data"]

    visitor.set_current("judge-a")
    assert (await client.get("/dashboard/summary")).json()["demo_activity"] is None

    # Once judge-b acts, judge-a's older run is no longer news to judge-b.
    visitor.set_current("judge-b")
    audit(db, "owner", "demo.reset")
    await db.commit()
    assert (await client.get("/dashboard/summary")).json()["demo_activity"] is None
    visitor.set_current("judge-a")
    assert (await client.get("/dashboard/summary")).json()["demo_activity"]["event"] == "demo.reset"
