"""Group 2: trustworthy device and network signals, the Turnstile human check, a neutral refund note and a
daily refund cap across incidents."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import update

from app.actions.executor import REFUND_NOTE, Executor
from app.api.shop import TURNSTILE_VERIFY_URL
from app.config import get_settings
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    Decision,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
)
from app.db.session import get_session
from app.guard import signals
from app.guard.check import invalidate_cache
from app.main import app
from app.shop.service import CheckoutRequest, OfflinePlan, fingerprint, place_order
from tests.conftest import SHOP
from tests.test_money_safety import R4, captured, incident_with

# ---- client IP and keyed hashes (pure) ---------------------------------------------------------------


def test_the_client_ip_is_counted_from_the_right_so_a_typed_header_does_not_win():
    # The client typed "6.6.6.6"; our one trusted proxy appended the address it really saw.
    assert signals.client_ip("6.6.6.6, 203.0.113.9", "10.0.0.1", hops=1) == "203.0.113.9"
    assert signals.client_ip("6.6.6.6, 203.0.113.9, 10.1.1.1", "10.0.0.2", hops=2) == "203.0.113.9"
    assert signals.client_ip("6.6.6.6", "10.0.0.1", hops=0) == "10.0.0.1"  # header ignored
    assert signals.client_ip(None, "198.51.100.4", hops=1) == "198.51.100.4"
    assert signals.client_ip("not-an-ip", None, hops=1) is None


def test_ips_are_stored_as_keyed_hashes_and_grouped_by_network():
    import hashlib

    ip = "203.0.113.9"
    assert signals.ip_hash(ip) != hashlib.sha256(ip.encode()).hexdigest()[:32]  # not a plain, reversible hash
    assert signals.network(ip) == "203.0.113.0/24"
    assert signals.network("2001:db8:1234:5678::1") == "2001:db8:1234::/48"
    assert signals.ip_net_hash("203.0.113.9") == signals.ip_net_hash("203.0.113.200")
    assert signals.ip_net_hash("203.0.113.9") != signals.ip_net_hash("203.0.114.9")
    assert signals.ip_hash(None) is None and signals.ip_net_hash(None) is None


# ---- the network rule ---------------------------------------------------------------------------------


def _req(device: str, ip: str | None, outcome: str = "approve", token: str | None = None) -> CheckoutRequest:
    return CheckoutRequest(
        shop_id=SHOP,
        sku="SKU-TEALIGHT-3",
        method="card",
        device_id=device,
        user_agent="bot/1.0",
        ip=ip,
        email="x@burner.example",
        challenge_token=token,
        offline=OfflinePlan(outcome),
    )


async def test_a_bot_rotating_device_ids_on_one_network_gets_the_human_check(db):
    for i in range(6):  # six "new devices", three of them declined, all from one /24
        res = await place_order(
            db, None, _req(f"rot-{i}", f"203.0.113.{i + 1}", "decline" if i % 2 else "approve")
        )
        assert res.decision == Decision.ALLOW
        invalidate_cache()
    nxt = await place_order(db, None, _req("rot-new", "203.0.113.77"))
    assert (nxt.decision, nxt.reason) == (Decision.CHALLENGE, "network_velocity")
    # Another network, or a caller that knows no IP (the simulator), is not affected.
    assert (await place_order(db, None, _req("rot-new", "198.51.100.7"))).decision == Decision.ALLOW
    assert (await place_order(db, None, _req("rot-other", None))).decision == Decision.ALLOW


async def test_a_person_behind_that_network_passes_with_the_human_check(db):
    from app.guard.check import issue_challenge_token

    for i in range(6):
        outcome = "decline" if i % 2 else "approve"
        await place_order(db, None, _req(f"rot-{i}", f"203.0.113.{i + 1}", outcome))
    token = issue_challenge_token(fingerprint("person", "bot/1.0"))
    assert (
        await place_order(db, None, _req("person", "203.0.113.50", token=token))
    ).decision == Decision.ALLOW


async def test_a_busy_network_without_declines_is_left_alone(db):
    for i in range(10):  # an office: many devices, nobody declined
        await place_order(db, None, _req(f"office-{i}", f"203.0.113.{i + 1}"))
    assert (await place_order(db, None, _req("office-new", "203.0.113.99"))).decision == Decision.ALLOW


# ---- device cookie and Turnstile over HTTP -----------------------------------------------------------


@pytest.fixture
async def shop(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


ORDER = {"sku": "SKU-TEALIGHT-3", "method": "card"}


async def test_the_first_device_id_is_pinned_in_a_signed_cookie(shop, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "")
    first = await shop.post("/shop/challenge", json={"device_id": "browser-one"})
    assert first.status_code == 200 and first.json()["verified_by"] == "click_through"
    assert "cg_dev" in shop.cookies
    # A later call claiming another id is still the first device: its token is for that fingerprint.
    pre = await shop.post(
        "/shop/precheck",
        json={**ORDER, "device_id": "rotated-id", "challenge_token": first.json()["challenge_token"]},
    )
    assert pre.status_code == 200 and pre.json()["decision"] == "allow"
    # A tampered cookie is ignored: the claimed id is used and pinned again.
    shop.cookies.clear()
    again = await shop.post(
        "/shop/challenge", json={"device_id": "browser-two"}, headers={"cookie": "cg_dev=forged.value"}
    )
    assert again.status_code == 200 and "cg_dev=browser-two." in again.headers["set-cookie"]


async def test_turnstile_token_is_verified_with_cloudflare(shop, monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "real-secret")
    monkeypatch.setattr(get_settings(), "turnstile_site_key", "0x4AAA-real-site-key")
    with respx.mock(assert_all_called=False) as cf:
        route = cf.post(TURNSTILE_VERIFY_URL).respond(json={"success": True})
        ok = await shop.post("/shop/challenge", json={"device_id": "person-1", "turnstile_token": "tok"})
        assert ok.status_code == 200 and ok.json()["verified_by"] == "turnstile"
        sent = dict(httpx.QueryParams(route.calls.last.request.content.decode()))
        assert sent["secret"] == "real-secret" and sent["response"] == "tok"

        cf.post(TURNSTILE_VERIFY_URL).respond(
            json={"success": False, "error-codes": ["invalid-input-response"]}
        )
        bad = await shop.post("/shop/challenge", json={"device_id": "bot-one", "turnstile_token": "fake"})
        assert bad.status_code == 403 and bad.json()["detail"]["code"] == "human_check_failed"
    missing = await shop.post("/shop/challenge", json={"device_id": "bot-two"})
    assert missing.status_code == 403  # no token at all: no Cloudflare call needed


async def test_shop_config_tells_the_page_how_to_check_and_whom_to_contact(shop):
    cfg = (await shop.get("/shop/config")).json()
    assert cfg["turnstile_site_key"] == "1x00000000000000000000AA" and cfg["turnstile_test_keys"] is True
    assert cfg["contact_email"]


# ---- refunds: neutral note, daily cap ----------------------------------------------------------------


def test_the_refund_note_does_not_accuse_the_cardholder():
    note = REFUND_NOTE.lower()
    assert "card testing" not in note and "unauthorised" not in note and "fraud" not in note
    assert "contact" in note and len(REFUND_NOTE) <= 255  # PayPal's note_to_payer limit


async def test_the_daily_cap_holds_refunds_across_incidents_for_the_owner(db):
    await db.execute(update(MerchantSettings).values(max_auto_refund_per_day=Decimal("5.00")))
    earlier = Incident(
        shop_id=SHOP, opened_at=datetime.now(UTC) - timedelta(hours=3), status=IncidentStatus.CLOSED
    )
    db.add(earlier)
    await db.flush()
    db.add(
        Action(
            incident_id=earlier.id,
            type=ActionType.REFUND,
            policy_rule="R4_attack",
            status=ActionStatus.EXECUTED,
            params={"amount": "3.10"},
        )
    )
    await db.commit()
    bot = await captured(db, "bot")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})
    (action,) = await Executor(db, None).refund_matched(inc, R4)
    # 3.10 earlier today + 3.10 now > 5.00: within the per-transaction and per-incident caps, over the day's.
    assert (action.status, action.policy_rule) == (
        ActionStatus.AWAITING_APPROVAL,
        "R5_over_daily_cap_needs_owner",
    )
    row = await db.get(CheckoutAttempt, bot.id, populate_existing=True)
    assert row is not None and row.outcome == Outcome.CAPTURED


async def test_refunds_older_than_a_day_do_not_count_against_the_cap(db):
    await db.execute(update(MerchantSettings).values(max_auto_refund_per_day=Decimal("5.00")))
    old = Incident(
        shop_id=SHOP, opened_at=datetime.now(UTC) - timedelta(days=2), status=IncidentStatus.CLOSED
    )
    db.add(old)
    await db.flush()
    db.add(
        Action(
            incident_id=old.id,
            type=ActionType.REFUND,
            policy_rule="R4_attack",
            status=ActionStatus.EXECUTED,
            params={"amount": "3.10"},
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    await db.commit()
    await captured(db, "bot")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})
    (action,) = await Executor(db, None).refund_matched(inc, R4)
    assert action.status == ActionStatus.EXECUTED
