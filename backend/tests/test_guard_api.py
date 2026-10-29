"""The public guard API records each check and links its PayPal outcome back (custom_id webhooks, or
POST /v1/guard/outcome), so another shop's checkout gets full detection, not observer mode."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import CheckoutAttempt, Outcome, PayPalEvent, Source
from app.db.session import get_session
from app.incidents.webhook_processor import process_pending
from app.main import app
from tests.conftest import SHOP


@pytest.fixture
async def shop_api(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    headers = {"X-Guard-Key": get_settings().shop_api_key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    ) as c:
        yield c
    app.dependency_overrides.clear()


CHECK = {
    "sku": "TEA-3",
    "method": "card",
    "fingerprint_hash": "f" * 32,
    "amount": "3.00",
    "email_domain": "Gmail.com",
}


async def test_a_check_is_recorded_and_returns_the_custom_id_for_the_order(shop_api, db):
    res = (await shop_api.post("/v1/guard/check", json={**CHECK, "client_ip": "203.0.113.9"})).json()
    assert res["decision"] == "allow" and res["custom_id"] == f"{SHOP}:{res['attempt_id']}"
    a = await db.get(CheckoutAttempt, res["attempt_id"])
    assert (a.source, a.outcome, a.email_domain, a.paypal_live) == (
        Source.API,
        Outcome.CREATED,
        "gmail.com",
        False,
    )
    assert a.ip_hash and a.ip_net_hash and "203.0.113" not in (a.ip_hash + a.ip_net_hash)


async def test_the_orders_webhook_reaches_the_checked_attempt(shop_api, db, monkeypatch):
    res = (await shop_api.post("/v1/guard/check", json=CHECK)).json()
    monkeypatch.setattr(get_settings(), "paypal_webhook_id", "")
    capture = {
        "id": "WH-9",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP-API-1", "status": "COMPLETED", "custom_id": res["custom_id"]},
    }
    await shop_api.post(
        "/webhooks/paypal", content=json.dumps(capture), headers={"paypal-transmission-id": "t9"}
    )
    ev = (await db.execute(select(PayPalEvent))).scalar_one()
    ev.verified = "SUCCESS"
    await db.commit()
    await process_pending(db)
    a = await db.get(CheckoutAttempt, res["attempt_id"], populate_existing=True)
    assert (a.outcome, a.capture_id, a.paypal_live) == (Outcome.CAPTURED, "CAP-API-1", True)
    assert (
        await db.execute(select(CheckoutAttempt).where(CheckoutAttempt.source == Source.OBSERVED))
    ).first() is None


async def test_a_decline_without_a_webhook_is_reported_and_never_overwrites_a_capture(shop_api, db):
    first = (await shop_api.post("/v1/guard/check", json=CHECK)).json()
    out = await shop_api.post(
        "/v1/guard/outcome",
        json={"attempt_id": first["attempt_id"], "outcome": "declined", "decline_code": "CARD_DECLINED"},
    )
    assert out.json()["outcome"] == "declined"
    a = await db.get(CheckoutAttempt, first["attempt_id"], populate_existing=True)
    assert (a.outcome, a.decline_code) == (Outcome.DECLINED, "CARD_DECLINED")
    again = await shop_api.post(
        "/v1/guard/outcome", json={"attempt_id": first["attempt_id"], "outcome": "captured"}
    )
    assert again.json()["outcome"] == "declined"  # only an attempt still waiting changes


async def test_the_api_needs_the_shop_key_and_only_its_own_attempts(shop_api, db):
    assert (
        await shop_api.post("/v1/guard/check", json=CHECK, headers={"X-Guard-Key": "wrong"})
    ).status_code == 401
    missing = await shop_api.post("/v1/guard/outcome", json={"attempt_id": "nope", "outcome": "declined"})
    assert missing.status_code == 404
