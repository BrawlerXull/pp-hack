"""Observer mode (app/incidents/observer.py): a shop whose checkout does not call the guard is still watched
through its PayPal webhooks, and an attack seen that way is detected, judged and refunded."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import Action, ActionType, CheckoutAttempt, Outcome, PayPalEvent, Source
from app.db.session import get_session
from app.incidents.service import IncidentService
from app.incidents.webhook_processor import process_pending
from app.main import app
from app.simulator import observer_feed
from app.simulator.scenarios import card_testing, flash_sale
from app.simulator.seed import seed_history
from tests.conftest import SHOP
from tests.test_pipeline import mock_ai  # noqa: F401  (fixture)


@pytest.fixture
async def api(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "paypal_webhook_id", "")  # stored as NO_WEBHOOK_ID (not processed)

    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def foreign_order(order_id: str, email: str = "jo@burner.example") -> dict:
    """CHECKOUT.ORDER.APPROVED from a hosted button: no custom_id of ours."""
    return {
        "id": "WH-1",
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {
            "id": order_id,
            "status": "APPROVED",
            "create_time": "2026-10-29T10:00:00Z",
            "purchase_units": [
                {"reference_id": "default", "amount": {"currency_code": "USD", "value": "3.00"}}
            ],
            "payer": {"email_address": email, "payer_id": "PAYERX1", "name": {"given_name": "Jo"}},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "4242", "expiry": "2030-01"}},
        },
    }


async def test_a_foreign_order_becomes_an_observed_attempt_with_only_safe_signals(api, db):
    res = await api.post("/webhooks/paypal", content=json.dumps(foreign_order("ORD-EXT-1")))
    assert res.status_code == 200
    ev = (await db.execute(select(PayPalEvent))).scalar_one()
    assert ev.payload["_cg"] == {
        "email_domain": "burner.example",
        "payer_id_hash": ev.payload["_cg"]["payer_id_hash"],
        "card_brand": "VISA",
        "card_last4": "4242",
        "card_expiry": "2030-01",
    }
    assert "jo@burner.example" not in json.dumps(ev.payload)
    ev.verified = "SUCCESS"  # as if PayPal's signature had checked out
    await db.commit()

    await process_pending(db)
    a = (await db.execute(select(CheckoutAttempt))).scalar_one()
    assert (a.source, a.outcome, a.guard_reason) == (Source.OBSERVED, Outcome.CREATED, "observed")
    assert (a.email_domain, a.card_last4, a.method, a.sku) == ("burner.example", "4242", "card", "price 3.00")
    assert a.paypal_live is True

    # The capture for that order completes the same attempt.
    capture = {
        "id": "WH-2",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-EXT-1",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "3.00"},
            "supplementary_data": {"related_ids": {"order_id": "ORD-EXT-1"}},
        },
    }
    await api.post("/webhooks/paypal", content=json.dumps(capture), headers={"paypal-transmission-id": "t2"})
    await db.execute(PayPalEvent.__table__.update().values(verified="SUCCESS"))
    await db.commit()
    await process_pending(db)
    assert (await db.execute(select(func.count()).select_from(CheckoutAttempt))).scalar() == 1
    a = (await db.execute(select(CheckoutAttempt))).scalar_one()
    assert (a.outcome, a.capture_id) == (Outcome.CAPTURED, "CAP-EXT-1")


async def test_unverified_foreign_events_change_nothing(api, db):
    await api.post("/webhooks/paypal", content=json.dumps(foreign_order("ORD-EXT-2")))
    await process_pending(db)  # NO_WEBHOOK_ID: never applied
    assert (await db.execute(select(func.count()).select_from(CheckoutAttempt))).scalar() == 0


async def _observe_scenario(db, gen, n: int) -> None:
    for a in list(gen("offline", count=n, seed=1)):
        await observer_feed.deliver(db, a)
    await db.commit()
    while await process_pending(db, limit=500):
        pass


async def test_an_attack_seen_only_through_webhooks_is_detected_and_its_refunds_are_queued(db, mock_ai):  # noqa: F811
    mock_ai("card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await _observe_scenario(db, card_testing, 80)
    observed = (
        await db.execute(
            select(func.count()).select_from(CheckoutAttempt).where(CheckoutAttempt.source == "observed")
        )
    ).scalar()
    assert observed and observed > 0

    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    assert incident is not None and incident.classification == "card_testing"
    # The captures the attack got through are queued for refund by the attack's pattern (no device to tie
    # them to it in observer mode), so they always wait for the owner instead of refunding on their own.
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert refunds, "the attack's observed captures are queued for refund"
    assert {(r.status, r.policy_rule) for r in refunds} == {
        ("awaiting_approval", "R5_weak_match_needs_owner")
    }
    assert all(r.params["matched_on"] == ["attack_pattern"] for r in refunds)

    # The incident page says the payments were seen through webhooks only (no blocking, no 3-D Secure).
    from app.api.dashboard import seen_via

    seen = await seen_via(db, incident)
    assert seen["observed"] == observed and seen["guarded"] == 0


async def test_a_flash_sale_seen_through_webhooks_is_not_an_attack(db, mock_ai):  # noqa: F811
    mock_ai("genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    await _observe_scenario(db, flash_sale, 60)
    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    assert incident is None or incident.classification != "card_testing"


async def test_a_demo_reset_drops_simulated_webhooks_still_queued(db):
    from app.api.demo import reset

    for a in list(card_testing("offline", count=5, seed=2)):
        await observer_feed.deliver(db, a)
    db.add(
        PayPalEvent(
            transmission_id="real-1", event_type="PAYMENT.CAPTURE.COMPLETED", verified="SUCCESS", payload={}
        )
    )
    await db.commit()
    await reset(SHOP, db)
    left = (await db.execute(select(PayPalEvent).where(PayPalEvent.processed_at.is_(None)))).scalars().all()
    assert [e.transmission_id for e in left] == ["real-1"]  # a real PayPal event is never dropped
