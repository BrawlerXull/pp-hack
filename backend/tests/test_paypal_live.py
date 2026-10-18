"""The live PayPal path (offline=None, real PayPalClient) against the stateful sandbox double:
order create/capture, both decline shapes, 3-D Secure, idempotent capture and refunds, owner approval
races, and webhook processing. No network: respx fails any request the double does not answer."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.actions.executor import Executor
from app.api.dashboard import _pending_action
from app.db import session as db_session
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    DeclineCategory,
    GuardMode,
    GuardState,
    Incident,
    Outcome,
    PayPalEvent,
    Source,
)
from app.guard.check import invalidate_cache
from app.incidents.webhook_processor import process_pending
from app.paypal.orders import CardDetails, OrderResult, refund_capture
from app.paypal.webhooks import Verification, verify_webhook
from app.policy.engine import PlannedAction
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import CheckoutRequest, capture_web_order, place_order
from app.simulator.runner import to_request
from app.simulator.scenarios import account_testing, flash_sale
from tests.conftest import SHOP
from tests.test_paypal import WEBHOOK_HEADERS


def card_req(name: str = "Test Buyer", device: str = "dev-live-1", method: str = "card") -> CheckoutRequest:
    return CheckoutRequest(
        shop_id=SHOP,
        sku=CHEAPEST_SKU,
        method=method,  # type: ignore[arg-type]
        device_id=device,
        user_agent="pytest",
        ip=None,
        email="buyer@example.com",
        source=Source.SIMULATOR,
        card=CardDetails(number="4012888888881881", name=name, expiry="2031-04")
        if method == "card"
        else None,
    )


async def _attempt(db, attempt_id: str) -> CheckoutAttempt:
    a = await db.get(CheckoutAttempt, attempt_id, populate_existing=True)
    assert a is not None
    return a


async def test_live_card_order_is_captured_with_stable_request_ids(db, sandbox):
    res = await place_order(db, sandbox.client, card_req())

    a = await _attempt(db, res.attempt_id)
    assert (a.outcome, a.paypal_live, a.card_expiry) == (Outcome.CAPTURED, True, "2031-04")
    assert a.capture_id and a.capture_id.startswith("CAP") and a.decline_code is None
    [create_id] = [rid for _, path, rid in sandbox.calls if path == "/v2/checkout/orders"]
    assert create_id == f"cg-order-{a.id}"
    [(_, _, capture_id)] = sandbox.posts(f"/v2/checkout/orders/{a.paypal_order_id}/capture")
    assert capture_id == f"cg-capture-{a.paypal_order_id}"


async def test_capture_declined_shape_stores_canonical_code_and_raw_processor_code(db, sandbox):
    sandbox.decline_shape = "capture"
    res = await place_order(db, sandbox.client, card_req("CCREJECT-SF"))

    a = await _attempt(db, res.attempt_id)
    assert a.outcome == Outcome.DECLINED  # not CAPTURED, although the order itself is COMPLETED
    assert (a.decline_code, a.processor_code, a.decline_category) == ("SUSPECTED_FRAUD", "9500", "fraud")


async def test_create_422_decline_shape_is_a_decline_not_an_error(db, sandbox):
    sandbox.decline_shape = "create_422"
    res = await place_order(db, sandbox.client, card_req("CCREJECT-IF"))

    a = await _attempt(db, res.attempt_id)
    assert (a.outcome, a.decline_code, a.processor_code) == (Outcome.DECLINED, "INSTRUMENT_DECLINED", None)
    assert a.decline_category == DeclineCategory.UNKNOWN_DECLINE


async def test_card_processing_not_enabled_is_an_integration_error(db, sandbox):
    sandbox.card_processing_enabled = False
    res = await place_order(db, sandbox.client, card_req())

    a = await _attempt(db, res.attempt_id)
    assert (a.outcome, a.decline_code, a.decline_category) == (
        Outcome.ERROR,
        "PAYEE_NOT_ENABLED_FOR_CARD_PROCESSING",
        DeclineCategory.PROCESSOR,
    )


async def test_sca_mode_sends_return_urls_and_records_sca_required(db, sandbox):
    gs = await db.get(GuardState, SHOP)
    gs.mode = GuardMode.SCA_ALL
    await db.commit()
    invalidate_cache()

    res = await place_order(db, sandbox.client, card_req())

    assert res.outcome == Outcome.SCA_REQUIRED and res.payer_action_url
    order = sandbox.orders[res.order_id]
    assert order.card["experience_context"] == {
        "return_url": "https://shop.example.test/?3ds=return",
        "cancel_url": "https://shop.example.test/?3ds=cancel",
    }
    # Capturing before the buyer finished 3-D Secure: 422 PAYER_ACTION_REQUIRED is SCA, not a decline.
    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)
    assert a is not None and (a.outcome, a.decline_code) == (Outcome.SCA_REQUIRED, None)


async def test_capture_after_3ds_uses_a_new_key_and_captures(db, sandbox):
    """PayPal replays a cached response for a reused key: the capture after 3-D Secure must be a new
    request, or the buyer would be stuck on the replayed PAYER_ACTION_REQUIRED forever."""
    gs = await db.get(GuardState, SHOP)
    gs.mode = GuardMode.SCA_ALL
    await db.commit()
    invalidate_cache()
    res = await place_order(db, sandbox.client, card_req())
    first = await capture_web_order(db, sandbox.client, SHOP, res.order_id)
    assert first is not None and first.outcome == Outcome.SCA_REQUIRED

    sandbox.complete_3ds(res.order_id)
    again = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert again is not None and again.outcome == Outcome.CAPTURED and again.capture_id
    keys = [rid for _, _, rid in sandbox.posts(f"/v2/checkout/orders/{res.order_id}/capture")]
    assert keys == [f"cg-capture-{res.order_id}", f"cg-capture-{res.order_id}-1"]


async def test_capture_timeout_reuses_the_key_and_reads_the_order(db, sandbox, monkeypatch):
    """A status-0 capture whose request actually went through: the order says CAPTURED, so we store it."""
    from app.shop import service

    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    sandbox.approve(res.order_id)
    real_capture = service.capture_order

    async def lost_response(client, order_id, tries=0):
        await real_capture(client, order_id, tries)  # PayPal captured it...
        return OrderResult(0, order_id, None, None, None, "NETWORK_ERROR", None, 0)  # ...we never heard

    monkeypatch.setattr(service, "capture_order", lost_response)
    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CAPTURED and a.capture_tries == 0
    assert a.capture_id == sandbox.orders[res.order_id].capture["id"]


async def test_racing_failure_never_downgrades_a_committed_capture(db, sandbox, monkeypatch):
    """Request B loaded the row before request A (or the webhook) committed CAPTURED, then got a
    failure and could not read the order: the row stays CAPTURED and in refund matching."""
    from app.shop import service

    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    sandbox.approve(res.order_id)
    sessions = db_session.get_sessionmaker()

    real_capture = service.capture_order

    async def other_request_wins(client, order_id, tries=0):
        monkeypatch.setattr(service, "capture_order", real_capture)
        async with sessions() as other:  # request A captures and commits while B waits on PayPal
            await capture_web_order(other, sandbox.client, SHOP, order_id)
        return OrderResult(0, order_id, None, None, None, "NETWORK_ERROR", None, 0)

    async def get_fails(client, order_id):
        return OrderResult(503, order_id, None, None, None, "SERVICE_UNAVAILABLE", None, 0)

    monkeypatch.setattr(service, "get_order", get_fails)
    monkeypatch.setattr(service, "capture_order", other_request_wins)
    async with sessions() as s2:
        loaded_early = await capture_web_order(s2, sandbox.client, SHOP, res.order_id)

    a = await _attempt(db, res.attempt_id)
    assert loaded_early is not None and a.outcome == Outcome.CAPTURED and a.decline_code is None
    assert a.capture_id == sandbox.orders[res.order_id].capture["id"]


async def test_already_captured_with_unreadable_order_leaves_the_row_alone(db, sandbox, monkeypatch):
    from app.shop import service

    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    sandbox.approve(res.order_id)
    sandbox.capture_out_of_band(res.order_id)

    async def get_fails(client, order_id):
        return OrderResult(0, order_id, None, None, None, "NETWORK_ERROR", None, 0)

    monkeypatch.setattr(service, "get_order", get_fails)
    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    # Not ERROR: PayPal said it is captured. The COMPLETED webhook (or a later retry) fills it in.
    assert a is not None and (a.outcome, a.decline_code) == (Outcome.CREATED, None)


async def test_capture_web_order_twice_captures_once(db, sandbox):
    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    assert res.outcome == Outcome.CREATED
    sandbox.approve(res.order_id)

    first = await capture_web_order(db, sandbox.client, SHOP, res.order_id)
    second = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert first is not None and second is not None
    assert second.outcome == Outcome.CAPTURED and second.capture_id == first.capture_id
    assert len(sandbox.posts(f"/v2/checkout/orders/{res.order_id}/capture")) == 1


async def test_order_already_captured_is_resolved_by_reading_the_order(db, sandbox):
    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    sandbox.approve(res.order_id)
    sandbox.capture_out_of_band(res.order_id)  # PayPal captured it; we never saw the response

    a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)

    assert a is not None and a.outcome == Outcome.CAPTURED and a.decline_code is None
    assert a.capture_id == sandbox.orders[res.order_id].capture["id"]
    assert ("GET", f"/v2/checkout/orders/{res.order_id}", None) in sandbox.calls


async def test_live_refund_uses_a_stable_key_and_never_refunds_twice(db, sandbox):
    res = await place_order(db, sandbox.client, card_req())
    a = await _attempt(db, res.attempt_id)
    incident = Incident(shop_id=SHOP)
    db.add(incident)
    await db.flush()

    params = {"attempt_id": a.id, "capture_id": a.capture_id, "amount": str(a.amount)}
    action = await Executor(db, sandbox.client).execute_refund(incident, a, params, "R4", approved_by="auto")
    await db.commit()

    assert action.status == ActionStatus.EXECUTED and action.paypal_ref == sandbox.refunds[a.capture_id]["id"]
    assert action.params["request_id"] == f"cg-refund-{a.id}"
    assert a.outcome == Outcome.REFUNDED and a.refund_id == action.paypal_ref
    # A repeat with the same key (retry, second incident) gets the same refund back, not a second one.
    again = await refund_capture(sandbox.client, a.capture_id, "again", f"cg-refund-{a.id}")
    assert again.ok and again.json["id"] == action.paypal_ref
    assert len(sandbox.refunds) == 1


async def test_oauth_failure_becomes_an_error_row_not_an_exception(db, sandbox):
    sandbox.token_fails = True
    res = await place_order(db, sandbox.client, card_req())

    a = await _attempt(db, res.attempt_id)
    assert (a.outcome, a.decline_code, a.decline_category) == (Outcome.ERROR, "AUTH_ERROR", "processor")


async def test_webhook_verification_against_the_double(sandbox):
    raw = '{"id":"WH-EVT-1","event_type":"PAYMENT.CAPTURE.COMPLETED"}'
    good = {**WEBHOOK_HEADERS, "paypal-transmission-sig": "valid-sig"}

    assert await verify_webhook(sandbox.client, good, raw, "WH-1") == Verification.SUCCESS
    assert await verify_webhook(sandbox.client, WEBHOOK_HEADERS, raw, "WH-1") == Verification.FAILURE
    assert sandbox.verify_bodies[0]["webhook_event"] == {
        "id": "WH-EVT-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
    }


def _event(
    event_type: str, resource: dict, verified: str, age: timedelta = timedelta(minutes=5)
) -> PayPalEvent:
    return PayPalEvent(
        transmission_id=f"tx-{event_type}-{resource.get('id')}-{verified}-{age.total_seconds()}",
        event_type=event_type,
        resource_id=resource.get("id"),
        verified=verified,
        received_at=datetime.now(UTC) - age,
        payload={"event_type": event_type, "resource": resource},
    )


async def test_process_pending_applies_verified_events_only(db, sandbox):
    wallet = await place_order(db, sandbox.client, card_req(method="wallet", device="dev-w1"))
    other = await place_order(db, sandbox.client, card_req(method="wallet", device="dev-w2"))
    db.add_all(
        [
            # Linked only through custom_id: no capture or order id on the resource.
            _event(
                "PAYMENT.CAPTURE.DECLINED",
                {
                    "id": "CAP-X1",
                    "custom_id": f"{SHOP}:{wallet.attempt_id}",
                    "processor_response": {"response_code": "9520"},
                },
                Verification.SUCCESS,
            ),
            # Unverified (forged) event: must change nothing.
            _event(
                "PAYMENT.CAPTURE.COMPLETED",
                {"id": "CAP-FORGED", "supplementary_data": {"related_ids": {"order_id": other.order_id}}},
                Verification.FAILURE,
            ),
            # A custom_id naming another shop never links.
            _event(
                "PAYMENT.CAPTURE.COMPLETED",
                {"id": "CAP-X2", "custom_id": f"other-shop:{other.attempt_id}"},
                Verification.SUCCESS,
            ),
        ]
    )
    await db.commit()

    assert await process_pending(db) == 3

    declined = await _attempt(db, wallet.attempt_id)
    assert (declined.outcome, declined.decline_code, declined.processor_code, declined.decline_category) == (
        Outcome.DECLINED,
        "LOST_OR_STOLEN",
        "9520",
        "fraud",
    )
    untouched = await _attempt(db, other.attempt_id)
    assert (untouched.outcome, untouched.capture_id) == (Outcome.CREATED, None)
    pending = (
        (await db.execute(select(PayPalEvent).where(PayPalEvent.processed_at.is_(None)))).scalars().all()
    )
    assert pending == []


async def test_unmatched_event_is_retried_until_the_attempt_exists(db, sandbox):
    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    a = await _attempt(db, res.attempt_id)
    resource = {"id": "CAP-LATE", "custom_id": f"{SHOP}:{a.id}"}
    # Arrived before our attempt row existed (simulated by pointing at an id not committed yet).
    early = _event(
        "PAYMENT.CAPTURE.COMPLETED", {**resource, "custom_id": f"{SHOP}:not-yet"}, "SUCCESS", timedelta(0)
    )
    stale = _event("PAYMENT.CAPTURE.COMPLETED", {"id": "CAP-OLD"}, "SUCCESS", timedelta(minutes=10))
    db.add_all([early, stale])
    await db.commit()

    await process_pending(db)
    await db.refresh(early)
    await db.refresh(stale)
    assert early.processed_at is None  # still inside the retry window
    assert early.retry_after is not None  # ...but backed off, so it does not refill every batch
    assert stale.processed_at is not None  # gave up after the window

    early.payload = {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": resource}
    await db.commit()
    assert await process_pending(db) == 0  # skipped until retry_after
    early.retry_after = datetime.now(UTC) - timedelta(seconds=1)  # the back-off has passed
    await db.commit()
    await process_pending(db)
    assert (await _attempt(db, a.id)).outcome == Outcome.CAPTURED


async def test_unmatched_events_do_not_starve_new_ones(db, sandbox):
    res = await place_order(db, sandbox.client, card_req(method="wallet"))
    db.add_all(
        [
            _event("PAYMENT.CAPTURE.COMPLETED", {"id": f"CAP-NOPE-{i}"}, "SUCCESS", timedelta(seconds=30 - i))
            for i in range(5)
        ]
    )
    await db.commit()
    await process_pending(db, limit=5)  # a full batch of never-matching events, all backed off

    db.add(
        _event(
            "PAYMENT.CAPTURE.COMPLETED",
            {"id": "CAP-REAL", "custom_id": f"{SHOP}:{res.attempt_id}"},
            "SUCCESS",
            timedelta(0),
        )
    )
    await db.commit()
    await process_pending(db, limit=5)
    assert (await _attempt(db, res.attempt_id)).outcome == Outcome.CAPTURED


async def test_second_approve_waits_for_the_first_and_gets_409(db):
    res = await place_order(
        db, None, to_request(SHOP, "t", next(flash_sale("offline", count=1, seed=4)), "offline")
    )
    incident = Incident(shop_id=SHOP)
    db.add(incident)
    await db.flush()
    action = Action(
        incident_id=incident.id,
        type=ActionType.REFUND,
        params={"attempt_id": res.attempt_id, "capture_id": res.capture_id, "amount": "3.10"},
        policy_rule="R4",
        status=ActionStatus.AWAITING_APPROVAL,
    )
    db.add(action)
    await db.commit()

    sessions = db_session.get_sessionmaker()
    async with sessions() as s1, sessions() as s2:
        inc1 = await s1.get(Incident, incident.id)
        locked = await _pending_action(s1, inc1, action.id)

        inc2 = await s2.get(Incident, incident.id)
        second = asyncio.create_task(_pending_action(s2, inc2, action.id))
        await asyncio.sleep(0.3)
        assert not second.done()  # blocked on the row lock

        await Executor(s1, None).reject(inc1, locked)
        await s1.commit()
        with pytest.raises(HTTPException) as err:
            await second
        assert err.value.status_code == 409


async def test_two_incidents_never_both_claim_one_refund(db):
    """Incident A's claim (here an approval in progress, not yet committed) keeps incident B's autopilot
    off the capture: the already-handled check is shop-wide, so B records nothing and the refund is
    counted in one report only. (Before, B waited on the attempt lock and re-checked the outcome.)"""
    res = await place_order(
        db, None, to_request(SHOP, "t", next(flash_sale("offline", count=1, seed=4)), "offline")
    )
    a = await _attempt(db, res.attempt_id)
    inc_a, inc_b = Incident(shop_id=SHOP), Incident(shop_id=SHOP)
    inc_b.signature = {
        "top_sku": a.sku,
        "fingerprints": [a.fingerprint_hash],
        "strong_fingerprints": [a.fingerprint_hash],
    }
    inc_b.opened_at = datetime.now(UTC)
    db.add_all([inc_a, inc_b])
    await db.flush()
    action = Action(
        incident_id=inc_a.id,
        type=ActionType.REFUND,
        params={"attempt_id": a.id, "capture_id": a.capture_id, "amount": str(a.amount)},
        policy_rule="R4",
        status=ActionStatus.AWAITING_APPROVAL,
    )
    db.add(action)
    await db.commit()

    sessions = db_session.get_sessionmaker()
    async with sessions() as s1, sessions() as s2:
        approved = await Executor(s1, None).approve(
            await s1.get(Incident, inc_a.id), await s1.get(Action, action.id)
        )
        assert approved.status == ActionStatus.EXECUTED
        planned = PlannedAction(ActionType.REFUND, "R4")
        assert await Executor(s2, None).refund_matched(await s2.get(Incident, inc_b.id), planned) == []
        await s1.commit()
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert [r.incident_id for r in refunds] == [inc_a.id]


# flash_sale seed 3 has both a card (CCREJECT-IF) and a wallet decline; account_testing seed 1 has
# PAYER_ACCOUNT_RESTRICTED declines.
@pytest.mark.parametrize(("gen", "n", "seed"), [(flash_sale, 60, 3), (account_testing, 80, 1)])
async def test_offline_declines_are_stored_as_declined_not_error(db, gen, n, seed):
    """Regression: INSUFFICIENT_FUNDS / PAYER_ACCOUNT_RESTRICTED used to fall through to ERROR, inflating
    error_rate toward a paypal_outage verdict."""
    for a in gen("offline", count=n, seed=seed):
        await place_order(db, None, to_request(SHOP, "test", a, "offline"))
    rows = (await db.execute(select(CheckoutAttempt))).scalars().all()
    assert any(r.outcome == Outcome.DECLINED for r in rows)
    assert not any(r.outcome == Outcome.ERROR for r in rows)
    assert all(r.decline_category for r in rows if r.outcome == Outcome.DECLINED)
