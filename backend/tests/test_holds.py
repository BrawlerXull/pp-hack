"""Payment holds (app/actions/holds.py) and targeted 3-D Secure (app/guard/check.py): during an attack a
first-time shopper's payment is authorised, not captured; the attack's holds are voided (no fee, nothing
settles), everyone else's are captured; returning customers skip 3-D Secure and the hold."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.actions.executor import Executor
from app.actions.holds import Holds
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    Decision,
    GuardMode,
    GuardState,
    Incident,
    IncidentStatus,
    Outcome,
    Source,
)
from app.guard.check import invalidate_cache
from app.policy.engine import PlannedAction
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import CheckoutRequest, OfflinePlan, capture_web_order, fingerprint, place_order
from tests.conftest import SHOP

R4 = PlannedAction(ActionType.REFUND, "R4_attack")


def order(
    device: str, method: str = "card", outcome: str = "approve", sku: str = CHEAPEST_SKU
) -> CheckoutRequest:
    return CheckoutRequest(
        shop_id=SHOP,
        sku=sku,
        method=method,
        device_id=device,
        user_agent="ua",
        ip=None,
        email="x@gmail.com",
        offline=OfflinePlan(outcome, is_bot=False),
    )


async def attack_defences(
    db, classification: str = "card_testing", confidence: float = 0.95, **sig
) -> Incident:
    """An open attack incident whose defences (3-D Secure for every card) are on."""
    inc = Incident(
        shop_id=SHOP,
        opened_at=datetime.now(UTC),
        classification=classification,
        confidence=confidence,
        signature={"top_sku": CHEAPEST_SKU, **sig},
    )
    db.add(inc)
    await db.flush()
    gs = await db.get(GuardState, SHOP)
    gs.mode, gs.set_by_incident, gs.updated_at = GuardMode.SCA_ALL, inc.id, datetime.now(UTC)
    await db.commit()
    invalidate_cache()
    return inc


async def past_capture(db, device: str, ago: timedelta) -> None:
    db.add(
        CheckoutAttempt(
            shop_id=SHOP,
            ts=datetime.now(UTC) - ago,
            source=Source.SIMULATOR,
            paypal_live=False,
            sku="SKU-PILLAR-18",
            amount=Decimal("18.00"),
            method="card",
            fingerprint_hash=fingerprint(device, "ua"),
            guard_decision=Decision.ALLOW,
            outcome=Outcome.CAPTURED,
            capture_id="SIMCAP-OLD",
        )
    )
    await db.commit()


async def _row(db, attempt_id: str) -> CheckoutAttempt:
    a = await db.get(CheckoutAttempt, attempt_id, populate_existing=True)
    assert a is not None
    return a


# ---- the gate: targeted 3-D Secure and holds ----------------------------------------------------------


async def test_returning_customers_skip_3ds_and_the_hold_first_timers_get_both(db):
    await past_capture(db, "regular", timedelta(days=3))
    await attack_defences(db)

    regular = await place_order(db, None, order("regular"))
    assert (regular.decision, regular.reason, regular.outcome) == (
        Decision.ALLOW,
        "returning_customer",
        Outcome.CAPTURED,
    )
    newcomer = await place_order(db, None, order("newcomer"))
    assert (newcomer.decision, newcomer.outcome) == (Decision.ALLOW_WITH_SCA, Outcome.AUTHORIZED)
    row = await _row(db, newcomer.attempt_id)
    assert row.authorization_id and row.authorization_id.startswith("SIMAUTH-") and row.capture_id is None
    # A PayPal wallet has no 3-D Secure: the hold is what protects it.
    wallet = await place_order(db, None, order("wallet-new", method="wallet"))
    assert (wallet.decision, wallet.reason, wallet.outcome) == (
        Decision.ALLOW,
        "payment_hold",
        Outcome.AUTHORIZED,
    )


async def test_a_bot_whose_test_payments_got_through_before_detection_is_not_a_returning_customer(db):
    await past_capture(db, "early-bot", timedelta(minutes=5))  # inside the attack window
    await attack_defences(db)
    res = await place_order(db, None, order("early-bot"))
    assert res.decision == Decision.ALLOW_WITH_SCA and res.outcome == Outcome.AUTHORIZED


async def test_no_holds_once_a_harmless_verdict_relaxed_the_defences(db):
    inc = await attack_defences(db, classification="genuine_rush")
    gs = await db.get(GuardState, SHOP)
    gs.mode, gs.held_skus = GuardMode.NORMAL, []  # policy R2 RELAX restored the checkout
    await db.commit()
    invalidate_cache()
    res = await place_order(db, None, order("rush-shopper"))
    assert (res.decision, res.reason, res.outcome) == (Decision.ALLOW, "normal", Outcome.CAPTURED)
    assert inc.id


async def test_a_declined_hold_is_a_decline(db):
    from app.paypal.orders import CardDetails

    await attack_defences(db)
    req = order("decliner", outcome="decline")
    req.card = CardDetails(number="4111111111111111", name="CCREJECT-SD")  # sandbox trigger: stolen card
    res = await place_order(db, None, req)
    assert res.outcome in {Outcome.DECLINED, Outcome.SCA_REQUIRED}


# ---- voids ----------------------------------------------------------------------------------------------


async def held(db, device: str, **kw) -> CheckoutAttempt:
    res = await place_order(db, None, order(device, method="wallet", **kw))
    assert res.outcome == Outcome.AUTHORIZED
    return await _row(db, res.attempt_id)


async def test_the_attacks_holds_are_voided_and_a_strangers_is_left_for_capture(db):
    inc = await attack_defences(db, strong_fingerprints=[fingerprint("bot", "ua")])
    bot = await held(db, "bot")
    stranger = await held(db, "stranger")

    done = await Executor(db, None).apply(inc, [R4])

    voids = [a for a in done if a.type == ActionType.VOID]
    assert [(a.status, a.params["attempt_id"]) for a in voids] == [(ActionStatus.EXECUTED, bot.id)]
    assert voids[0].params["simulated"] is True and voids[0].paypal_ref.startswith("SIMVOID-")
    assert (await _row(db, bot.id)).outcome == Outcome.VOIDED
    assert (await _row(db, stranger.id)).outcome == Outcome.AUTHORIZED
    # Nothing was captured, so there is nothing to refund.
    assert not [a for a in done if a.type == ActionType.REFUND]


async def test_a_weak_match_waits_for_the_owner_who_can_void_or_keep_it(db):
    domain_sig = {"suspect_domains": ["gmail.com"]}
    inc = await attack_defences(db, **domain_sig)
    a = await held(db, "maybe-bot")
    b = await held(db, "maybe-human")
    done = await Executor(db, None).apply(inc, [R4])
    pending = {x.params["attempt_id"]: x for x in done if x.type == ActionType.VOID}
    assert {x.status for x in pending.values()} == {ActionStatus.AWAITING_APPROVAL}

    await Executor(db, None).approve(inc, pending[a.id])
    await Executor(db, None).reject(inc, pending[b.id])  # "Keep": capture it now
    await db.commit()
    assert (await _row(db, a.id)).outcome == Outcome.VOIDED
    kept = await _row(db, b.id)
    assert kept.outcome == Outcome.CAPTURED and kept.capture_id.startswith("SIMCAP-")


# ---- settling everything else ---------------------------------------------------------------------------


async def test_holds_are_captured_when_the_incident_closes_or_is_judged_harmless(db):
    inc = await attack_defences(db)
    a = await held(db, "shopper-a")
    assert await Holds(Executor(db, None)).settle(SHOP) == 0  # attack still open: keep holding

    inc.classification, inc.confidence = "genuine_rush", 0.9
    await db.commit()
    assert await Holds(Executor(db, None)).settle(SHOP) == 1
    assert (await _row(db, a.id)).outcome == Outcome.CAPTURED

    inc.classification = "card_testing"
    await db.commit()
    b = await held(db, "shopper-b")
    inc.status = IncidentStatus.CLOSED
    await db.commit()
    assert await Holds(Executor(db, None)).settle(SHOP) == 1
    assert (await _row(db, b.id)).outcome == Outcome.CAPTURED


async def test_a_hold_nobody_decided_is_captured_after_a_day(db):
    inc = await attack_defences(db, suspect_domains=["gmail.com"])
    a = await held(db, "undecided")
    (pending,) = [x for x in await Executor(db, None).apply(inc, [R4]) if x.type == ActionType.VOID]
    holds = Holds(Executor(db, None))
    assert await holds.settle(SHOP) == 0  # the owner decides first
    assert await holds.settle(SHOP, datetime.now(UTC) + timedelta(hours=25)) == 1
    assert (await _row(db, a.id)).outcome == Outcome.CAPTURED
    expired = await db.get(Action, pending.id, populate_existing=True)
    assert expired.status == ActionStatus.REJECTED and expired.error == "hold expired: captured"


# ---- live, against the PayPal sandbox double -------------------------------------------------------------


@pytest.fixture
async def live_attack(db):
    inc = await attack_defences(db)
    return inc


def web_wallet(device: str) -> CheckoutRequest:
    return CheckoutRequest(
        shop_id=SHOP,
        sku=CHEAPEST_SKU,
        method="wallet",
        device_id=device,
        user_agent="ua",
        ip=None,
        email="b@x.io",
    )


async def test_live_holds_are_authorised_then_voided_or_captured_at_paypal(db, sandbox, live_attack):
    inc = live_attack
    inc.signature = {"top_sku": CHEAPEST_SKU, "strong_fingerprints": [fingerprint("live-bot", "ua")]}
    await db.commit()

    created = []
    for device in ("live-bot", "live-human"):
        res = await place_order(db, sandbox.client, web_wallet(device))
        assert sandbox.orders[res.order_id].intent == "AUTHORIZE"
        sandbox.approve(res.order_id)
        a = await capture_web_order(db, sandbox.client, SHOP, res.order_id)
        assert a is not None and a.outcome == Outcome.AUTHORIZED and a.authorization_id.startswith("AUTH")
        created.append(a)
    # Held, not captured: no order capture call at all.
    assert not [c for c in sandbox.posts("/v2/checkout/orders/") if c[1].endswith("/capture")]

    await Executor(db, sandbox.client).apply(inc, [R4])
    bot, human = [await _row(db, a.id) for a in created]
    assert bot.outcome == Outcome.VOIDED
    assert sandbox.authorizations[bot.authorization_id].authorization["status"] == "VOIDED"
    assert human.outcome == Outcome.AUTHORIZED

    inc.status = IncidentStatus.CLOSED
    await db.commit()
    assert await Holds(Executor(db, sandbox.client)).settle(SHOP) == 1
    human = await _row(db, human.id)
    assert human.outcome == Outcome.CAPTURED and human.capture_id.startswith("CAP")
    assert sandbox.authorizations[human.authorization_id].authorization["status"] == "CAPTURED"
    # Retried settlement reuses the same request id: PayPal sees one capture per hold.
    assert {p[2] for p in sandbox.posts("/v2/payments/authorizations/")} == {
        f"cg-hold-void-{bot.id}",
        f"cg-hold-capture-{human.id}",
    }
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert refunds == []


# ---- the review window: holds before anything is detected ----------------------------------------------


@pytest.fixture
async def review_window(db):
    """The production default: first-time shoppers' payments settle after a 10-minute review window."""
    from sqlalchemy import update

    from app.db.models import MerchantSettings

    await db.execute(update(MerchantSettings).values(review_hold_min=10))
    await db.commit()
    invalidate_cache()


async def test_in_normal_times_a_newcomer_is_held_for_the_review_window_and_then_charged(db, review_window):
    await past_capture(db, "regular", timedelta(days=3))
    regular = await place_order(db, None, order("regular"))
    assert regular.outcome == Outcome.CAPTURED  # a returning customer is never held
    newcomer = await place_order(db, None, order("newcomer"))
    assert (newcomer.decision, newcomer.reason, newcomer.outcome) == (
        Decision.ALLOW,
        "normal",
        Outcome.AUTHORIZED,
    )
    row = await _row(db, newcomer.attempt_id)
    assert row.hold_reason == "review" and row.hold_incident_id is None

    holds = Holds(Executor(db, None))
    assert await holds.settle(SHOP) == 0  # still inside the window
    assert await holds.settle(SHOP, datetime.now(UTC) + timedelta(minutes=11)) == 1
    assert (await _row(db, newcomer.attempt_id)).outcome == Outcome.CAPTURED


async def test_an_attacks_payments_from_before_detection_are_voided_not_refunded(db, review_window):
    # The burst's first payments get through before any incident exists: held for review, not captured.
    early = [await place_order(db, None, order("burst-bot", method="wallet")) for _ in range(2)]
    stranger = await place_order(db, None, order("burst-stranger", method="wallet"))
    assert all(r.outcome == Outcome.AUTHORIZED for r in [*early, stranger])

    # Detection opens the incident; the scorecard is sure; the policy plans refunds (R4).
    inc = await attack_defences(db, strong_fingerprints=[fingerprint("burst-bot", "ua")])
    inc.opened_at = datetime.now(UTC)
    await db.commit()
    done = await Executor(db, None).apply(inc, [R4])
    assert sorted(a.type for a in done) == [ActionType.VOID, ActionType.VOID]
    for r in early:
        assert (await _row(db, r.attempt_id)).outcome == Outcome.VOIDED

    # The stranger's payment waits while the incident covers it, even past the review window ...
    holds = Holds(Executor(db, None))
    assert await holds.settle(SHOP, datetime.now(UTC) + timedelta(minutes=11)) == 0
    # ... and is charged once the incident is over.
    inc.status = IncidentStatus.CLOSED
    await db.commit()
    assert await holds.settle(SHOP, datetime.now(UTC) + timedelta(minutes=11)) == 1
    assert (await _row(db, stranger.attempt_id)).outcome == Outcome.CAPTURED
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert refunds == []
