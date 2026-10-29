"""Webhook hardening, per-client rate limits and one open incident per shop."""

import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_paypal
from app.api.webhooks import MAX_WEBHOOK_BYTES
from app.config import SANDBOX_API_BASE, get_settings
from app.db.models import Incident, IncidentStatus, PayPalEvent
from app.db.session import get_session
from app.main import app
from app.paypal.webhooks import REDACTED, Verification, cert_url_ok, redact, verify_webhook
from tests.conftest import SHOP
from tests.test_paypal import WEBHOOK_HEADERS, make_client, mock_token

# ---- webhooks -----------------------------------------------------------------------------------------


def test_only_https_paypal_certificates_are_accepted():
    assert cert_url_ok("https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1")
    assert cert_url_ok("https://api.paypal.com/cert.pem")
    assert not cert_url_ok("http://api.paypal.com/cert.pem")  # not https
    assert not cert_url_ok("https://paypal.com.evil.example/cert.pem")
    assert not cert_url_ok("https://evilpaypal.com/cert.pem")
    assert not cert_url_ok(None)


@respx.mock(base_url=SANDBOX_API_BASE, assert_all_called=False)
async def test_a_foreign_certificate_is_refused_without_asking_paypal(respx_mock):
    mock_token(respx_mock)
    postback = respx_mock.post("/v1/notifications/verify-webhook-signature").respond(
        200, json={"verification_status": "SUCCESS"}
    )
    headers = {**WEBHOOK_HEADERS, "paypal-cert-url": "https://attacker.example/cert.pem"}
    assert await verify_webhook(make_client(), headers, "{}", "WH-1") == Verification.BAD_CERT_URL
    assert not postback.called


def test_buyer_personal_data_is_redacted_but_ids_amounts_and_statuses_are_kept():
    event = {
        "id": "WH-EV-1",
        "resource": {
            "id": "CAP-1",
            "status": "COMPLETED",
            "amount": {"value": "3.00", "currency_code": "USD"},
            "payer": {
                "payer_id": "PAYER123",
                "email_address": "jo@example.com",
                "name": {"given_name": "Jo", "surname": "Doe"},
                "address": {"country_code": "US"},
            },
            "purchase_units": [{"custom_id": "shop:abc", "shipping": {"name": {"full_name": "Jo Doe"}}}],
        },
    }
    out = redact(event)
    payer = out["resource"]["payer"]
    assert payer["payer_id"] == "PAYER123"
    assert payer["email_address"] == payer["name"] == payer["address"] == REDACTED
    assert out["resource"]["purchase_units"][0] == {"custom_id": "shop:abc", "shipping": REDACTED}
    assert out["resource"]["amount"] == event["resource"]["amount"]
    assert "jo@example.com" not in json.dumps(out)


@pytest.fixture
async def api(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_the_stored_webhook_has_no_buyer_email(api, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "paypal_webhook_id", "")  # NO_WEBHOOK_ID: stored, not verified
    body = {
        "id": "EV-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "C1", "payer": {"email_address": "jo@example.com"}},
    }
    res = await api.post(
        "/webhooks/paypal", content=json.dumps(body), headers={"paypal-transmission-id": "tx-9"}
    )
    assert res.status_code == 200
    stored = (await db.execute(select(PayPalEvent))).scalar_one()
    assert stored.payload["resource"]["payer"]["email_address"] == REDACTED and stored.resource_id == "C1"


async def test_an_oversized_webhook_is_refused_before_it_is_read_whole(api, db):
    huge = "{" + '"x": "' + "a" * (MAX_WEBHOOK_BYTES + 10) + '"}'
    res = await api.post("/webhooks/paypal", content=huge)
    assert res.status_code == 413
    assert (await db.execute(select(PayPalEvent))).first() is None


# ---- rate limits --------------------------------------------------------------------------------------


async def test_password_guessing_is_slowed_to_ten_tries_per_five_minutes(api):
    for _ in range(10):
        assert (await api.post("/auth/login", json={"password": "wrong"})).status_code == 401
    res = await api.post("/auth/login", json={"password": "wrong"})
    assert res.status_code == 429 and int(res.headers["Retry-After"]) > 0
    assert res.json()["detail"]["code"] == "rate_limited"


async def test_successful_logins_do_not_use_up_the_allowance(api):
    password = get_settings().admin_password
    for _ in range(12):
        assert (await api.post("/auth/login", json={"password": password})).status_code == 200


async def test_a_typed_forwarded_for_does_not_buy_a_fresh_allowance(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 0)
    for i in range(10):
        await api.post("/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": f"10.0.0.{i}"})
    res = await api.post("/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": "10.9.9.9"})
    assert res.status_code == 429


async def test_clients_behind_a_trusted_proxy_get_their_own_allowance(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    for _ in range(10):
        await api.post("/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": "203.0.113.1"})
    blocked = await api.post(
        "/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": "203.0.113.1"}
    )
    other = await api.post(
        "/auth/login", json={"password": "wrong"}, headers={"x-forwarded-for": "198.51.100.2"}
    )
    assert blocked.status_code == 429 and other.status_code == 401


async def test_limits_can_be_switched_off_for_load_tests(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limits_enabled", False)
    for _ in range(15):
        assert (await api.post("/auth/login", json={"password": "wrong"})).status_code == 401


# ---- one open incident --------------------------------------------------------------------------------


async def test_a_second_open_incident_is_refused_by_the_database(db):
    db.add(Incident(shop_id=SHOP))
    await db.commit()
    db.add(Incident(shop_id=SHOP))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(Incident(shop_id=SHOP, status=IncidentStatus.CLOSED))  # closed ones are unlimited
    await db.commit()
