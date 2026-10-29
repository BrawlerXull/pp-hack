"""P10: browser card payments through PayPal's hosted Card Fields, with 3-D Secure checked on our server.

The order is created without card data (SCA_ALWAYS while the guard requires 3-D Secure, else
SCA_WHEN_REQUIRED), the browser submits the card straight to PayPal, and the capture endpoint reads the
order's authentication_result before it captures (app/paypal/three_ds.py). PayPal is the respx sandbox
double; no network."""

import dataclasses
import json

import httpx
import pytest

from app.actions.executor import Executor
from app.api.deps import get_paypal
from app.db.models import ActionStatus, CheckoutAttempt, Decision, GuardMode, GuardState, Outcome, Source
from app.db.session import get_session
from app.detection.features import Baseline, card_key, compute
from app.guard.check import invalidate_cache
from app.main import app
from app.paypal.orders import order_payload
from app.paypal.three_ds import CardInfo, capture_allowed, parse_card
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import CheckoutRequest, capture_web_order, place_order
from tests.conftest import SHOP
from tests.test_money_safety import R4, captured, incident_with, row

PASS_CARD = "4868719166101368"  # PayPal sandbox 3-D Secure card: challenge, passes
FAIL_CARD = "4868719181895556"  # PayPal sandbox 3-D Secure card: challenge, fails


def web_card(device: str = "browser-device-1") -> CheckoutRequest:
    """What POST /shop/orders builds for method=card: no card details at all."""
    return CheckoutRequest(
        shop_id=SHOP,
        sku=CHEAPEST_SKU,
        method="card",
        device_id=device,
        user_agent="Mozilla/5.0",
        ip=None,
        email="shopper@example.com",
    )


async def sca_mode(db) -> None:
    gs = await db.get(GuardState, SHOP)
    gs.mode = GuardMode.SCA_ALL
    await db.commit()
    invalidate_cache()


async def _attempt(db, attempt_id: str) -> CheckoutAttempt:
    a = await db.get(CheckoutAttempt, attempt_id, populate_existing=True)
    assert a is not None
    return a


def captures(sandbox, order_id: str) -> list:
    return sandbox.posts(f"/v2/checkout/orders/{order_id}/capture")


# ---- the 3-D Secure table (pure) --------------------------------------------------------------------


def auth(ls: str | None, enrollment: str | None = None, status: str | None = None) -> CardInfo:
    return CardInfo(brand="VISA", last4="1368", liability_shift=ls, enrollment=enrollment, auth_status=status)


@pytest.mark.parametrize(
    ("card", "order_status", "sca_required", "allowed"),
    [
        # The bank verified the cardholder: always captured.
        (auth("POSSIBLE", "Y", "Y"), "APPROVED", True, True),
        (auth("YES", "Y", "Y"), "APPROVED", True, True),
        (auth("POSSIBLE", "Y", "A"), "APPROVED", False, True),
        # The guard required 3-D Secure: anything short of a liability shift is refused.
        (auth("NO", "Y", "N"), "APPROVED", True, False),
        (auth("UNKNOWN", "Y", "U"), "APPROVED", True, False),
        (auth("NO", "N"), "APPROVED", True, False),  # not enrolled: no verification during an attack
        (None, "APPROVED", True, False),  # no result at all
        (auth("NO", "Y", "C"), "PAYER_ACTION_REQUIRED", True, False),  # buyer cancelled the bank window
        # Normal mode (SCA_WHEN_REQUIRED), PayPal's recommended actions.
        (None, "APPROVED", False, True),  # 3-D Secure did not run: the bank decides at capture
        (CardInfo(brand="VISA", last4="1881"), "APPROVED", False, True),
        (auth("NO", "N"), "APPROVED", False, True),  # not enrolled
        (auth("NO", "U"), "APPROVED", False, True),  # issuer unavailable
        (auth("NO", "B"), "APPROVED", False, True),  # bypassed
        (auth("NO"), "APPROVED", False, True),  # bare NO, no three_d_secure block: the bank was not asked
        (auth("NO"), "APPROVED", True, False),  # ...but not while the guard requires 3-D Secure
        (auth("NO", None, "N"), "APPROVED", False, False),  # an authentication that failed
        (auth("NO", "Y", "N"), "APPROVED", False, False),  # challenged and failed
        (auth("NO", "Y", "R"), "APPROVED", False, False),  # rejected by the issuer
        (auth("UNKNOWN", "U"), "APPROVED", False, False),
        (auth("UNKNOWN"), "APPROVED", False, False),
        (None, "PAYER_ACTION_REQUIRED", False, False),  # the bank asked and nobody answered
    ],
)
def test_capture_allowed_follows_the_liability_shift_table(card, order_status, sca_required, allowed):
    assert capture_allowed(card, order_status, sca_required) is allowed


def test_parse_card_keeps_brand_last4_bin_and_3ds_result_but_never_a_number():
    info = parse_card(
        {
            "card": {
                "name": "Card Holder",
                "last_digits": "1368",
                "expiry": "2030-11",
                "brand": "visa",
                "bin_details": {"bin": "486871", "issuing_bank": "Test Bank"},
                "authentication_result": {
                    "liability_shift": "possible",
                    "three_d_secure": {"enrollment_status": "Y", "authentication_status": "Y"},
                },
            }
        }
    )
    assert info == CardInfo("VISA", "1368", "486871", "2030-11", "POSSIBLE", "Y", "Y")
    assert parse_card({"paypal": {"email_address": "x@example.com"}}) is None
    assert parse_card(None) is None
    # Junk in place of digits is dropped, not stored.
    assert parse_card({"card": {"last_digits": "abcd", "bin_details": {"bin": "12"}}}) is None


def test_card_fields_payload_carries_only_the_3ds_method():
    common = dict(
        sku="S", description="d", amount=1, custom_id="c", return_url="https://r", cancel_url="https://c"
    )
    when_required = order_payload(card_fields=True, **common)["payment_source"]["card"]
    always = order_payload(card_fields=True, force_sca=True, **common)["payment_source"]["card"]

    assert when_required["attributes"] == {"verification": {"method": "SCA_WHEN_REQUIRED"}}
    assert always["attributes"] == {"verification": {"method": "SCA_ALWAYS"}}
    for card in (when_required, always):
        assert not {"number", "expiry", "security_code", "name"} & card.keys()
        assert card["experience_context"] == {"return_url": "https://r", "cancel_url": "https://c"}


# ---- create: SCA method by guard decision ------------------------------------------------------------


async def test_web_card_order_asks_for_3ds_only_when_required_in_normal_mode(db, sandbox):
    res = await place_order(db, sandbox.client, web_card())

    assert (res.decision, res.outcome) == (Decision.ALLOW, Outcome.CREATED) and res.order_id
    sent = json.loads(sandbox.bodies[-1])["payment_source"]["card"]
    assert sent["attributes"] == {"verification": {"method": "SCA_WHEN_REQUIRED"}}
    a = await _attempt(db, res.attempt_id)
    assert (a.source, a.paypal_live, a.card_last4, a.card_bin6) == (Source.WEB, True, None, None)


async def test_web_card_order_asks_for_3ds_always_while_the_guard_requires_it(db, sandbox):
    await sca_mode(db)

    res = await place_order(db, sandbox.client, web_card())

    assert (res.decision, res.outcome) == (Decision.ALLOW_WITH_SCA, Outcome.CREATED) and res.order_id
    sent = json.loads(sandbox.bodies[-1])["payment_source"]["card"]
    assert sent["attributes"] == {"verification": {"method": "SCA_ALWAYS"}}


# ---- capture: liability check ------------------------------------------------------------------------


@pytest.mark.parametrize("liability", ["YES", "POSSIBLE"])
async def test_capture_after_a_passed_3ds_check_stores_the_card_and_result(db, sandbox, liability):
    await sca_mode(db)
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(res.order_id, number=PASS_CARD, liability_shift=liability)

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CAPTURED and a.capture_id
    assert (a.card_brand, a.card_last4, a.card_bin6, a.card_expiry) == ("VISA", "1368", "486871", "2030-11")
    assert (a.sca_liability_shift, a.sca_enrollment, a.sca_auth_status) == (liability, "Y", "Y")
    # The order is read before the capture, and the capture keeps its stable key.
    order_path = f"/v2/checkout/orders/{res.order_id}"
    paths = [(m, p) for m, p, _ in sandbox.calls if p.startswith(order_path)]
    assert paths == [("GET", order_path), ("POST", f"{order_path}/capture")]
    assert [rid for _, _, rid in captures(sandbox, res.order_id)] == [f"cg-capture-{res.order_id}"]


@pytest.mark.parametrize(
    ("liability", "enrollment", "status"),
    [("NO", "Y", "N"), ("UNKNOWN", "Y", "U"), ("NO", "Y", "C"), ("NO", "N", None), (None, None, None)],
)
async def test_failed_or_missing_3ds_under_sca_mode_is_never_captured(
    db, sandbox, liability, enrollment, status
):
    await sca_mode(db)
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(
        res.order_id, number=FAIL_CARD, liability_shift=liability, enrollment=enrollment, auth_status=status
    )

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.SCA_FAILED
    assert a.capture_id is None and a.decline_code is None
    assert captures(sandbox, res.order_id) == []  # no capture call at all
    assert (a.card_brand, a.card_last4) == ("VISA", "5556")
    assert (a.sca_liability_shift, a.sca_enrollment, a.sca_auth_status) == (liability, enrollment, status)


async def test_cancelled_bank_window_in_normal_mode_is_sca_failed_not_a_capture_attempt(db, sandbox):
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(
        res.order_id, liability_shift="NO", enrollment="Y", auth_status="C", status="PAYER_ACTION_REQUIRED"
    )

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.SCA_FAILED and captures(sandbox, res.order_id) == []


async def test_normal_mode_card_without_3ds_is_captured_and_a_bank_decline_keeps_its_code(db, sandbox):
    ok = await place_order(db, sandbox.client, web_card("browser-ok"))
    sandbox.card_fields_submit(
        ok.order_id,
        number="4012888888881881",
        liability_shift=None,
        enrollment=None,
        auth_status=None,
        bin_details=False,
    )
    bad = await place_order(db, sandbox.client, web_card("browser-bad"))
    sandbox.card_fields_submit(
        bad.order_id, name="CCREJECT-IF", liability_shift=None, enrollment=None, auth_status=None
    )

    paid = await capture_web_order(db, sandbox.client, SHOP, ok.order_id)
    declined = await capture_web_order(db, sandbox.client, SHOP, bad.order_id)

    assert paid is not None and paid.outcome == Outcome.CAPTURED
    # No BIN from PayPal: the brand stands in, so the card still has an identity for detection.
    assert (paid.card_bin6, paid.card_brand, paid.card_last4, paid.sca_liability_shift) == (
        None,
        "VISA",
        "1881",
        None,
    )
    assert card_key(paid.card_bin6, paid.card_last4, paid.card_expiry, paid.card_brand) == "VISA*1881:2030-11"
    assert declined is not None and declined.outcome == Outcome.DECLINED
    assert (declined.decline_code, declined.processor_code) == ("INSUFFICIENT_FUNDS", "5120")


async def test_repeat_capture_is_idempotent_and_a_refusal_is_never_captured_later(db, sandbox):
    await sca_mode(db)
    good = await place_order(db, sandbox.client, web_card("browser-good"))
    sandbox.card_fields_submit(good.order_id)
    first = await capture_web_order(db, sandbox.client, SHOP, good.order_id)
    calls = len(sandbox.calls)

    again = await capture_web_order(db, sandbox.client, SHOP, good.order_id)

    assert again is not None and first is not None and again.capture_id == first.capture_id
    assert len(sandbox.calls) == calls  # the stored capture answers: no GET, no second capture

    failed = await place_order(db, sandbox.client, web_card("browser-failed"))
    sandbox.card_fields_submit(failed.order_id, number=FAIL_CARD, liability_shift="NO", auth_status="N")
    for _ in range(2):
        a = await capture_web_order(db, sandbox.client, SHOP, failed.order_id)
        assert a is not None and a.outcome == Outcome.SCA_FAILED
    assert captures(sandbox, failed.order_id) == []


async def test_order_captured_elsewhere_is_read_not_captured_again(db, sandbox):
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(res.order_id)
    sandbox.capture_out_of_band(res.order_id)

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CAPTURED
    assert a.capture_id == sandbox.orders[res.order_id].capture["id"]
    assert captures(sandbox, res.order_id) == []


async def test_unreadable_order_under_sca_mode_is_left_alone(db, sandbox, monkeypatch):
    from app.paypal.orders import OrderResult
    from app.shop import service

    await sca_mode(db)
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(res.order_id)

    async def get_fails(client, order_id):
        return OrderResult(503, order_id, None, None, None, "SERVICE_UNAVAILABLE", None, 0)

    monkeypatch.setattr(service, "get_order", get_fails)
    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CREATED and captures(sandbox, res.order_id) == []


async def test_no_card_number_ever_leaves_our_server_for_a_browser_card_payment(db, sandbox):
    await sca_mode(db)
    for device, number in (("browser-pass", PASS_CARD), ("browser-fail", FAIL_CARD)):
        res = await place_order(db, sandbox.client, web_card(device))
        sandbox.card_fields_submit(
            res.order_id, number=number, liability_shift="POSSIBLE" if number == PASS_CARD else "NO"
        )
        await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    sent = b"".join(sandbox.bodies)
    assert sent  # we did send the order bodies
    for secret in (PASS_CARD.encode(), FAIL_CARD.encode(), b'"number"', b"security_code"):
        assert secret not in sent


# ---- detection and refunds ---------------------------------------------------------------------------


def test_sca_failed_counts_as_stopped_by_3ds_and_never_as_a_decline():
    from datetime import UTC, datetime

    from tests.test_money_safety import sig_of

    rows = [row("human", Outcome.SCA_FAILED), row("bot", Outcome.SCA_REQUIRED), row("buyer")]
    f = compute(rows, 5, datetime.now(UTC), Baseline())

    assert (f.sca_required, f.declined, f.captured) == (2, 0, 1)
    # Failing or cancelling the bank's window is something a real shopper does: never flags the device.
    assert sig_of([row("human", Outcome.SCA_FAILED) for _ in range(4)]).fingerprints == []


async def test_a_card_the_bank_verified_in_normal_mode_is_never_auto_refunded(db):
    a = await captured(db, "bot", source="web", sca_liability_shift="YES")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})

    [action] = await Executor(db, None).refund_matched(inc, R4)

    assert (action.status, action.policy_rule) == (
        ActionStatus.AWAITING_APPROVAL,
        "R5_sca_passed_needs_owner",
    )
    assert action.params["sca_passed"] is True and a.id == action.params["attempt_id"]


async def test_liability_possible_in_normal_mode_is_still_auto_refunded(db):
    """POSSIBLE usually means attempts processing: nobody was challenged, so a bot can get it."""
    a = await captured(db, "bot", source="web", sca_liability_shift="POSSIBLE", sca_auth_status="A")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})

    [action] = await Executor(db, None).refund_matched(inc, R4)

    assert action.status == ActionStatus.EXECUTED and action.params["sca_passed"] is False
    assert action.params["attempt_id"] == a.id


# ---- capture: fail closed, method mismatch, browser-reported failures --------------------------------


def web_wallet(device: str = "browser-wallet-1") -> CheckoutRequest:
    return dataclasses.replace(web_card(device), method="wallet")


async def test_a_card_submitted_to_a_wallet_order_is_refused_without_a_capture(db, sandbox):
    """A card tester asks for a wallet order (no 3-D Secure attribute) and submits a card to it."""
    await sca_mode(db)
    res = await place_order(db, sandbox.client, web_wallet())
    assert res.decision == Decision.ALLOW and res.order_id
    sandbox.card_fields_submit(res.order_id, number=FAIL_CARD, liability_shift="NO", enrollment="N")

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and (a.outcome, a.method, a.card_last4) == (Outcome.SCA_FAILED, "card", "5556")
    assert captures(sandbox, res.order_id) == []


async def test_a_web_wallet_order_is_read_then_captured(db, sandbox):
    res = await place_order(db, sandbox.client, web_wallet("browser-wallet-2"))
    sandbox.approve(res.order_id)

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and (a.outcome, a.method) == (Outcome.CAPTURED, "wallet")
    assert len(captures(sandbox, res.order_id)) == 1


async def test_unreadable_card_order_in_normal_mode_is_never_captured(db, sandbox, monkeypatch):
    from app.paypal.orders import OrderResult
    from app.shop import service

    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(res.order_id, number=FAIL_CARD, liability_shift="NO", auth_status="N")

    async def get_fails(client, order_id):
        return OrderResult(503, order_id, None, None, None, "SERVICE_UNAVAILABLE", None, 0)

    monkeypatch.setattr(service, "get_order", get_fails)
    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CREATED and captures(sandbox, res.order_id) == []


async def test_capture_of_an_unsubmitted_card_order_makes_no_capture_call(db, sandbox):
    res = await place_order(db, sandbox.client, web_card())

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and (a.outcome, a.capture_tries or 0) == (Outcome.CREATED, 0)
    assert captures(sandbox, res.order_id) == []


@pytest.mark.parametrize("status", ["APPROVED", "CREATED"])
async def test_browser_reported_cancel_is_recorded_as_sca_failed_and_never_captured(db, sandbox, status):
    """Normal mode, the shopper closed the bank window and PayPal left no authentication_result."""
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(
        res.order_id, liability_shift=None, enrollment=None, auth_status=None, status=status
    )

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id, record_only=True)
    later = await capture_web_order(db, sandbox.client, SHOP, res.order_id)  # a scripted capture after

    assert a is not None and a.outcome == Outcome.SCA_FAILED
    assert later is not None and later.outcome == Outcome.SCA_FAILED
    assert captures(sandbox, res.order_id) == []


async def test_browser_reported_failure_before_the_card_reached_paypal_leaves_the_row(db, sandbox):
    res = await place_order(db, sandbox.client, web_card())

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id, record_only=True)

    assert a is not None and a.outcome == Outcome.CREATED and captures(sandbox, res.order_id) == []


# ---- the HTTP endpoints ------------------------------------------------------------------------------


@pytest.fixture
async def shop_api(db, sandbox):
    async def session():
        yield db

    app.dependency_overrides.update({get_paypal: lambda: sandbox.client, get_session: session})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_card_checkout_over_http_reports_a_failed_bank_check(db, sandbox, shop_api):
    await sca_mode(db)
    created = await shop_api.post(
        "/shop/orders", json={"sku": CHEAPEST_SKU, "method": "card", "device_id": "browser-http-1"}
    )
    assert created.status_code == 200
    body = created.json()
    assert (body["decision"], body["outcome"]) == ("allow_with_sca", "created") and body["order_id"]

    sandbox.card_fields_submit(body["order_id"], number=FAIL_CARD, liability_shift="NO", auth_status="N")
    res = await shop_api.post(f"/shop/orders/{body['order_id']}/capture")

    assert res.status_code == 200
    assert res.json() | {"order_id": None} == {
        "order_id": None,
        "outcome": "sca_failed",
        "capture_id": None,
        "decline_code": None,
        "decline_category": None,
        "liability_shift": "NO",
    }


async def test_card_result_endpoint_records_a_failed_check_without_capturing(db, sandbox, shop_api):
    created = await shop_api.post(
        "/shop/orders", json={"sku": CHEAPEST_SKU, "method": "card", "device_id": "browser-http-3"}
    )
    order_id = created.json()["order_id"]
    sandbox.card_fields_submit(order_id, number=FAIL_CARD, liability_shift="NO", auth_status="N")

    res = await shop_api.post(f"/shop/orders/{order_id}/card-result")

    assert res.status_code == 200 and res.json()["outcome"] == "sca_failed"
    assert captures(sandbox, order_id) == []
    assert (await shop_api.post("/shop/orders/NOPE/card-result")).status_code == 404


async def test_card_details_in_the_order_request_are_ignored(db, sandbox, shop_api):
    """The shop API has no card fields: a client that sends a card number anyway never forwards it."""
    created = await shop_api.post(
        "/shop/orders",
        json={
            "sku": CHEAPEST_SKU,
            "method": "card",
            "device_id": "browser-http-2",
            "card": {"number": PASS_CARD, "security_code": "123"},
        },
    )
    assert created.status_code == 200
    assert PASS_CARD.encode() not in b"".join(sandbox.bodies)


async def test_reports_count_a_failed_bank_check_as_stopped_by_3ds(db):
    from app.ai import reporter
    from tests.test_reporting import attack_incident, capture

    inc = await attack_incident(db)
    for fp, outcome in (
        ("bot-a", Outcome.SCA_REQUIRED),
        ("human", Outcome.SCA_FAILED),
        ("bot-b", Outcome.DECLINED),
    ):
        await capture(db, fp, outcome=outcome, capture_id="-")
    await db.commit()

    f = await reporter.gather_facts(db, inc)

    assert (f["sca_required"], f["declined"], f["stopped"]) == ("2", "1", "3")


def test_a_failed_bank_check_is_not_quiet_for_de_escalation():
    from datetime import UTC, datetime

    from app.detection.triggers import quiet_since

    now = datetime.now(UTC)
    rows = [row("x", Outcome.SCA_FAILED, ts=now), row("y", Outcome.SCA_FAILED, ts=now)]
    assert not quiet_since(rows, now, 5)
    assert quiet_since(rows[:1], now, 5)


def test_attempt_event_carries_brand_last4_and_liability_but_no_number():
    from app.shop.service import attempt_event

    a = CheckoutAttempt(
        id="a1",
        sku=CHEAPEST_SKU,
        amount=3,
        method="card",
        fingerprint_hash="f" * 32,
        card_brand="VISA",
        card_last4="5556",
        card_bin6="486871",
        sca_liability_shift="NO",
        outcome=Outcome.SCA_FAILED,
    )
    ev = attempt_event(a)
    assert (ev["card_brand"], ev["card_last4"], ev["sca_liability_shift"]) == ("VISA", "5556", "NO")
    assert "card_bin6" not in ev and "486871" not in json.dumps(ev, default=str)


def test_a_capture_answer_without_the_3ds_result_keeps_the_one_read_before():
    from app.paypal.three_ds import merge

    before = CardInfo("VISA", "1368", "486871", "2030-11", "POSSIBLE", "Y", "Y")
    after = CardInfo(brand="VISA", last4="1368")
    assert merge(before, after) == before
    assert merge(None, after) == after and merge(before, None) == before
    assert merge(before, CardInfo(liability_shift="YES")).liability_shift == "YES"


async def test_a_created_order_with_a_passing_3ds_result_is_captured(db, sandbox):
    """Live sandbox, Oct 25: after the bank challenge passed, the order was still CREATED (not APPROVED)
    with liability_shift POSSIBLE, enrollment Y, authentication Y, and capturing it succeeded. Requiring
    APPROVED refused a correctly verified card."""
    res = await place_order(db, sandbox.client, web_card())
    sandbox.card_fields_submit(
        res.order_id, liability_shift="POSSIBLE", enrollment="Y", auth_status="Y", status="CREATED"
    )

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CAPTURED and a.capture_id
    assert a.sca_liability_shift == "POSSIBLE"


async def test_the_checkout_pages_fraudnet_id_reaches_paypal_with_the_order(db, sandbox):
    req = web_card("browser-fraudnet")
    req.client_metadata_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    await place_order(db, sandbox.client, req)
    sent = [h["paypal-client-metadata-id"] for h in sandbox.headers if "paypal-client-metadata-id" in h]
    assert sent == ["a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"]  # on the create-order call only
