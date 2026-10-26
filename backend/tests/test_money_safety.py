"""Money safety (P5): strong vs weak refund matching, never refunding a disputed or SCA-passed capture,
the owner's "Keep" holding across incidents, expiring device blocks with owner unblock (trust), and
in-flight refunds re-sent by the sweeper with the same PayPal-Request-Id. AI and PayPal are always faked."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.actions.executor import MAX_SWEEPS, SWEEP_AFTER, Executor, classify_match
from app.api.dashboard import ApproveIn, approve, close_incident, list_devices, reject, unblock_device
from app.config import get_settings
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    Decision,
    DeclineCategory,
    DeviceEntry,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
)
from app.detection.features import AttemptRow, Baseline
from app.detection.triggers import signature
from app.guard import devices
from app.guard.check import invalidate_cache
from app.incidents.service import IncidentService
from app.incidents.webhook_processor import apply_dispute
from app.policy.engine import PlannedAction
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import OfflinePlan, fingerprint, place_order
from tests.conftest import SHOP, blocked
from tests.test_incident_flow import REASSESS, attack, counting
from tests.test_paypal_live import card_req

R4 = PlannedAction(ActionType.REFUND, "R4_attack")


def row(fp: str, outcome: str = Outcome.CAPTURED, **kw) -> AttemptRow:
    base = dict(
        ts=datetime.now(UTC),
        sku=CHEAPEST_SKU,
        amount=3.1,
        method="card",
        fingerprint_hash=fp,
        email_domain="gmail.com",
        card_key=None,
        payer_id_hash=None,
        outcome=outcome,
        decline_code=None,
    )
    return AttemptRow(**{**base, **kw})


def sig_of(rows: list[AttemptRow], **kw):
    now = datetime.now(UTC)
    return signature(rows, now - timedelta(minutes=15), now + timedelta(seconds=1), Baseline(), **kw)


async def captured(db, fp: str, **kw) -> CheckoutAttempt:
    """A capture on the attacked item, inserted directly (offline-simulated unless paypal_live is set)."""
    fields = dict(
        shop_id=SHOP,
        source="simulator",
        paypal_live=False,
        sku=CHEAPEST_SKU,
        amount=Decimal("3.10"),
        method="card",
        fingerprint_hash=fp,
        email_domain="gmail.com",
        guard_decision=Decision.ALLOW,
        outcome=Outcome.CAPTURED,
    )
    a = CheckoutAttempt(**{**fields, **kw})
    db.add(a)
    await db.flush()
    a.capture_id = a.capture_id or f"CAP-{a.id}"
    await db.commit()
    return a


async def incident_with(db, sig: dict) -> Incident:
    inc = Incident(shop_id=SHOP, opened_at=datetime.now(UTC), signature={"top_sku": CHEAPEST_SKU, **sig})
    db.add(inc)
    await db.commit()
    return inc


async def refunds_for(db, attempt_id: str) -> list[Action]:
    q = select(Action).where(
        Action.type == ActionType.REFUND, Action.params["attempt_id"].as_string() == attempt_id
    )
    return list((await db.execute(q.execution_options(populate_existing=True))).scalars())


async def events(db, name: str) -> list[dict]:
    return list((await db.execute(select(AuditLog.data).where(AuditLog.event == name))).scalars())


# ---- item 13: strong vs weak matching (pure) ----------------------------------------------------------


def test_classify_match_strong_only_for_strong_or_blocked_devices():
    sig = {
        "strong_fingerprints": ["bot"],
        "fingerprints": ["bot", "flagged"],
        "weak_fingerprints": ["unlucky"],
        "suspect_domains": ["burner.example"],
    }
    assert classify_match("bot", None, sig, set()) == (True, ["strong_fingerprint"])
    assert classify_match("old-bot", None, sig, {"old-bot"}) == (True, ["blocked_device"])
    assert classify_match("flagged", None, sig, set()) == (False, ["flagged_device"])
    assert classify_match("unlucky", None, sig, set()) == (False, ["single_decline_device"])
    assert classify_match("fresh", "burner.example", sig, set()) == (False, ["suspect_domain"])
    # A first-time shopper on a domain nobody has seen before, with no bad signal: not touched at all.
    assert classify_match("fresh", "new-domain.example", sig, set()) is None


def test_a_domain_is_suspect_only_with_three_attempts_and_thirty_percent_declines():
    declined = dict(outcome=Outcome.DECLINED, decline_category=DeclineCategory.CUSTOMER)
    rows = [
        # rare.example: 2 attempts, both declined: too few to say anything about the domain.
        row("r1", email_domain="rare.example", **declined),
        row("r2", email_domain="rare.example", **declined),
        # busy.example: 4 attempts, 1 declined (25%): under the bar.
        *[row(f"b{i}", email_domain="busy.example") for i in range(3)],
        row("b3", email_domain="busy.example", **declined),
        # burner.example: 3 attempts, 1 declined (33%): suspect.
        row("x1", email_domain="burner.example"),
        row("x2", email_domain="burner.example"),
        row("x3", email_domain="burner.example", **declined),
    ]
    sig = sig_of(rows)
    assert sig.suspect_domains == ["burner.example"]
    assert sig.new_email_domains == ["burner.example", "busy.example", "rare.example"]


def test_device_flags_are_narrow_and_neutral_outcomes_never_count():
    rows = [
        # A wallet buyer who opened and closed the PayPal popup three times, then paid.
        *[row("wallet-human", Outcome.CREATED, method="wallet") for _ in range(3)],
        row("wallet-human", method="wallet", payer_id_hash="payer-1"),
        # A card shopper stopped by 3-D Secure, the guard challenge and a block: nothing they did.
        row("sca-human", Outcome.SCA_REQUIRED),
        row("sca-human", Outcome.CHALLENGED),
        row("sca-human", Outcome.BLOCKED),
        # One fraud-coded refusal: flagged (blockable with an AI verdict) but not a rules-only bot.
        row("stolen", Outcome.DECLINED, decline_category=DeclineCategory.FRAUD),
        # Two PayPal accounts from one device: strong.
        row("two-payers", payer_id_hash="p1"),
        row("two-payers", payer_id_hash="p2"),
        # Three real tries on the attacked item.
        *[
            row("hammer", Outcome.DECLINED, decline_category=DeclineCategory.UNKNOWN_DECLINE)
            for _ in range(3)
        ],
    ]
    sig = sig_of(rows)
    assert sig.fingerprints == ["hammer", "stolen", "two-payers"]
    assert sig.strong_fingerprints == ["two-payers"]
    assert "3 refusals on the targeted item" in sig.flag_reasons["hammer"]
    assert sig.flag_reasons["stolen"] == "refused as stolen or restricted"
    # A device the owner trusts is never flagged, whatever it does.
    assert "two-payers" not in sig_of(rows, trusted=frozenset({"two-payers"})).fingerprints


# ---- item 13: strong vs weak matching (executor) ------------------------------------------------------


async def test_only_a_strong_match_is_refunded_without_the_owner_and_matched_on_is_stored(db):
    bot = await captured(db, "bot")
    burner = await captured(db, "fresh", email_domain="burner.example")
    newcomer = await captured(db, "newcomer", email_domain="new-domain.example")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"], "suspect_domains": ["burner.example"]})

    done = {a.params["attempt_id"]: a for a in await Executor(db, None).refund_matched(inc, R4)}

    assert done[bot.id].status == ActionStatus.EXECUTED and done[bot.id].params["match"] == "strong"
    assert done[bot.id].params["matched_on"] == ["strong_fingerprint"]
    weak = done[burner.id]
    assert (weak.status, weak.policy_rule) == (ActionStatus.AWAITING_APPROVAL, "R5_weak_match_needs_owner")
    assert weak.params["matched_on"] == ["suspect_domain"] and weak.params["match"] == "weak"
    assert newcomer.id not in done


async def test_a_capture_that_passed_3ds_is_never_auto_refunded(db):
    a = await captured(db, "bot", guard_decision=Decision.ALLOW_WITH_SCA)
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})

    [action] = await Executor(db, None).refund_matched(inc, R4)

    assert (action.status, action.policy_rule) == (
        ActionStatus.AWAITING_APPROVAL,
        "R5_sca_passed_needs_owner",
    )
    assert (await db.get(CheckoutAttempt, a.id, populate_existing=True)).outcome == Outcome.CAPTURED


async def test_a_new_domain_human_with_no_declines_is_not_refunded_during_an_attack(db, monkeypatch):
    await attack(db, monkeypatch)
    human = await captured(db, fingerprint("new-human", "Mozilla/5.0"), email_domain="new-domain.example")

    await IncidentService(db, None, REASSESS).tick(SHOP)

    assert (await db.get(CheckoutAttempt, human.id, populate_existing=True)).outcome == Outcome.CAPTURED
    assert await refunds_for(db, human.id) == []


async def test_a_wallet_buyer_who_cancels_the_popup_three_times_is_neither_blocked_nor_refunded(
    db, monkeypatch
):
    await attack(db, monkeypatch)
    fp = fingerprint("wallet-human", "Mozilla/5.0")
    for _ in range(3):  # PayPal popup opened, then closed: a CREATED order each time
        db.add(
            CheckoutAttempt(
                shop_id=SHOP,
                source="simulator",
                paypal_live=False,
                sku=CHEAPEST_SKU,
                amount=Decimal("3.10"),
                method="wallet",
                fingerprint_hash=fp,
                email_domain="gmail.com",
                guard_decision=Decision.ALLOW,
                outcome=Outcome.CREATED,
            )
        )
    await db.commit()
    paid = await captured(db, fp, method="wallet", payer_id_hash="payer-human")

    await IncidentService(db, None, REASSESS).tick(SHOP)

    assert fp not in await blocked(db)
    assert (await db.get(CheckoutAttempt, paid.id, populate_existing=True)).outcome == Outcome.CAPTURED
    assert await refunds_for(db, paid.id) == []


# ---- item 24: disputes -----------------------------------------------------------------------------


def test_dispute_status_is_tracked_and_resolved_does_not_mark_disputed_blindly():
    a = CheckoutAttempt(disputed=False)
    apply_dispute(a, "CUSTOMER.DISPUTE.CREATED", {"status": "WAITING_FOR_SELLER_RESPONSE"})
    assert (a.disputed, a.dispute_status) == (True, "WAITING_FOR_SELLER_RESPONSE")
    apply_dispute(
        a, "CUSTOMER.DISPUTE.RESOLVED", {"dispute_outcome": {"outcome_code": "RESOLVED_SELLER_FAVOUR"}}
    )
    assert (a.disputed, a.dispute_status) == (False, "RESOLVED:RESOLVED_SELLER_FAVOUR")
    apply_dispute(a, "CUSTOMER.DISPUTE.UPDATED", {"status": "UNDER_REVIEW"})  # late, out of order
    assert (a.disputed, a.dispute_status) == (False, "RESOLVED:RESOLVED_SELLER_FAVOUR")

    paid = CheckoutAttempt(disputed=False)
    apply_dispute(
        paid, "CUSTOMER.DISPUTE.RESOLVED", {"dispute_outcome": {"outcome_code": "RESOLVED_BUYER_FAVOUR"}}
    )
    assert paid.disputed  # the buyer already got the money back: a refund would pay twice


async def test_a_disputed_capture_is_never_matched_for_a_refund(db):
    a = await captured(db, "bot", disputed=True, dispute_status="OPEN")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})

    assert await Executor(db, None).refund_matched(inc, R4) == []
    assert await refunds_for(db, a.id) == []


async def test_approving_a_disputed_refund_is_409_unless_the_owner_overrides(db):
    a = await captured(db, "bot")
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})
    pending = Action(
        incident_id=inc.id,
        type=ActionType.REFUND,
        policy_rule="R5_over_cap_needs_owner",
        status=ActionStatus.AWAITING_APPROVAL,
        params={"attempt_id": a.id, "capture_id": a.capture_id, "amount": "3.10"},
    )
    db.add(pending)
    a.disputed, a.dispute_status = True, "OPEN"  # the dispute arrived while the refund waited
    await db.commit()
    inc_id, action_id, attempt_id = inc.id, pending.id, a.id  # the 409 rolls the session back

    with pytest.raises(HTTPException) as err:
        await approve(inc_id, action_id, SHOP, db, None, None)  # type: ignore[arg-type]
    assert err.value.status_code == 409 and err.value.detail["code"] == "disputed"
    assert "second time" in err.value.detail["message"]
    assert (await db.get(Action, action_id, populate_existing=True)).status == ActionStatus.AWAITING_APPROVAL

    out = await approve(inc_id, action_id, SHOP, db, None, ApproveIn(override_dispute=True))  # type: ignore[arg-type]
    assert out["status"] == ActionStatus.EXECUTED
    assert (await db.get(CheckoutAttempt, attempt_id, populate_existing=True)).outcome == Outcome.REFUNDED
    assert await events(db, "refund.dispute_override")


# ---- item 25: the owner's Keep holds across incidents -----------------------------------------------


async def test_keep_in_one_incident_is_respected_by_the_next(db, monkeypatch):
    ms = await db.get(MerchantSettings, SHOP)
    ms.autopilot = False  # every refund waits for the owner
    await db.commit()
    inc_a = await attack(db, monkeypatch)
    q = select(Action).where(Action.status == ActionStatus.AWAITING_APPROVAL)
    awaiting = (await db.execute(q)).scalars().all()
    assert awaiting
    kept = awaiting[0]
    await reject(inc_a.id, kept.id, SHOP, db)  # "Keep"
    await close_incident(inc_a.id, SHOP, db, None, get_settings())  # type: ignore[arg-type]

    ms = await db.get(MerchantSettings, SHOP, populate_existing=True)
    ms.autopilot = True
    await db.commit()
    counting(monkeypatch, "card_testing")
    inc_b = await IncidentService(db, None, REASSESS).tick(SHOP)  # same attack still in the window
    assert inc_b is not None and inc_b.id != inc_a.id and inc_b.status == IncidentStatus.OPEN

    attempt_id = kept.params["attempt_id"]
    k = await db.get(CheckoutAttempt, attempt_id, populate_existing=True)
    assert k.outcome == Outcome.CAPTURED
    # Not vacuous: B's attack signature matches the kept capture strongly, so only the Keep stopped it.
    await db.refresh(inc_b)
    match = classify_match(
        k.fingerprint_hash, k.email_domain, inc_b.signature, await devices.blocked(db, SHOP)
    )
    assert match is not None and match[0]
    assert [(r.incident_id, r.status) for r in await refunds_for(db, attempt_id)] == [
        (inc_a.id, ActionStatus.REJECTED)
    ]
    b_refunds = (await db.execute(select(Action).where(Action.incident_id == inc_b.id))).scalars().all()
    assert not [r for r in b_refunds if r.type == ActionType.REFUND]


async def test_a_second_claim_on_one_capture_is_refused_by_the_index_in_a_savepoint(db):
    a = await captured(db, "bot")
    inc_a = await incident_with(db, {})
    inc_b = await incident_with(db, {})
    params = {"attempt_id": a.id, "capture_id": a.capture_id, "amount": "3.10"}
    ex = Executor(db, None)
    assert await ex._record_refund(inc_a, "R4", ActionStatus.REJECTED, params) is not None
    await db.commit()

    assert await ex._record_refund(inc_b, "R4", ActionStatus.AWAITING_APPROVAL, params) is None
    await db.commit()  # the savepoint rolled back alone: the session is still usable
    assert [r.incident_id for r in await refunds_for(db, a.id)] == [inc_a.id]
    assert await events(db, "refund.already_claimed")

    # A FAILED refund is outside the index: it never blocks a retry.
    first = (await refunds_for(db, a.id))[0]
    first.status = ActionStatus.FAILED
    await db.commit()
    assert await ex._record_refund(inc_b, "R4", ActionStatus.AWAITING_APPROVAL, params) is not None


# ---- item 15: expiring blocks, owner unblock -------------------------------------------------------


async def test_blocks_expire_are_audited_and_the_guard_stops_blocking(db):
    fp = fingerprint("dev-expire", "Mozilla/5.0")
    await devices.block(db, SHOP, [fp], None, {fp: "test"}, datetime.now(UTC))
    await db.commit()
    invalidate_cache()
    req = replace(
        card_req(device="dev-expire"), user_agent="Mozilla/5.0", offline=OfflinePlan("approve", is_bot=False)
    )
    assert (await place_order(db, None, req)).decision == Decision.BLOCK

    entry = (await db.execute(select(DeviceEntry))).scalar_one()
    entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    invalidate_cache()
    assert (await place_order(db, None, req)).decision != Decision.BLOCK  # filtered before any prune
    assert await devices.prune_expired(db, SHOP, datetime.now(UTC)) == [fp]
    await db.commit()
    assert (await events(db, "block.expired"))[0]["fingerprints"] == [fp]
    assert await devices.prune_expired(db, SHOP, datetime.now(UTC)) == []  # released once


async def test_an_unblocked_device_is_trusted_and_not_blocked_again_by_enforcement(db, monkeypatch):
    await attack(db, monkeypatch)
    fp = (await blocked(db))[0]
    listed = await list_devices(SHOP, db)
    assert fp in [d["fingerprint"] for d in listed["blocked"]]
    assert all(d["reason"] and d["incident_id"] and d["expires_at"] for d in listed["blocked"])

    out = await unblock_device(fp, SHOP, db)
    assert out == {"fingerprint": fp, "was_blocked": True, "trusted": True}

    await IncidentService(db, None, REASSESS).tick(SHOP)  # enforcement re-runs the signature
    assert fp not in await blocked(db)
    incident = (await db.execute(select(Incident))).scalar_one()
    await db.refresh(incident)
    assert fp not in incident.signature["fingerprints"]
    listed = await list_devices(SHOP, db)
    assert [d["fingerprint"] for d in listed["trusted"]] == [fp]
    assert await events(db, "device.unblocked")


# ---- in-flight refunds and the sweeper ----------------------------------------------------------------


@pytest.mark.parametrize("trouble", ["refund_answer_lost", "refund_outage"])
async def test_an_in_flight_refund_is_re_sent_with_the_same_request_id(db, sandbox, trouble):
    res = await place_order(db, sandbox.client, card_req())
    a = await db.get(CheckoutAttempt, res.attempt_id)
    incident = Incident(shop_id=SHOP)
    db.add(incident)
    await db.flush()
    setattr(sandbox, trouble, 1)

    params = {"attempt_id": a.id, "capture_id": a.capture_id, "amount": str(a.amount)}
    ex = Executor(db, sandbox.client)
    action = await ex.execute_refund(incident, a, params, "R4", approved_by="auto")
    await db.commit()
    assert action.status == ActionStatus.IN_FLIGHT and action.claimed_at is not None

    assert await ex.sweep_in_flight(SHOP, now=datetime.now(UTC) + timedelta(minutes=1)) == []  # not due yet
    [swept] = await ex.sweep_in_flight(SHOP, now=datetime.now(UTC) + SWEEP_AFTER + timedelta(seconds=5))

    assert swept.status == ActionStatus.EXECUTED and swept.paypal_ref == sandbox.refunds[a.capture_id]["id"]
    assert len(sandbox.refunds) == 1
    keys = {rid for _, _, rid in sandbox.posts(f"/v2/payments/captures/{a.capture_id}/refund")}
    assert keys == {f"cg-refund-{a.id}"}
    assert (await db.get(CheckoutAttempt, a.id, populate_existing=True)).outcome == Outcome.REFUNDED


async def test_the_sweeper_gives_up_after_max_sweeps_and_skips_while_the_kill_switch_is_on(db, sandbox):
    res = await place_order(db, sandbox.client, card_req())
    a = await db.get(CheckoutAttempt, res.attempt_id)
    incident = Incident(shop_id=SHOP)
    db.add(incident)
    await db.flush()
    sandbox.refund_outage = 100
    ex = Executor(db, sandbox.client)
    params = {"attempt_id": a.id, "capture_id": a.capture_id, "amount": str(a.amount)}
    action = await ex.execute_refund(incident, a, params, "R4", approved_by="auto")
    await db.commit()

    ms = await db.get(MerchantSettings, SHOP)
    ms.kill_switch = True
    await db.commit()
    later = datetime.now(UTC) + SWEEP_AFTER * 2
    assert await ex.sweep_in_flight(SHOP, now=later) == []
    ms.kill_switch = False
    await db.commit()

    for i in range(MAX_SWEEPS + 1):
        [swept] = await ex.sweep_in_flight(SHOP, now=later + SWEEP_AFTER * 2 * (i + 1))
    assert (
        swept.id == action.id and swept.status == ActionStatus.FAILED and "check the capture" in swept.error
    )
    assert await events(db, "refund.sweep_gave_up")
    assert (await db.get(CheckoutAttempt, a.id, populate_existing=True)).outcome == Outcome.CAPTURED


# ---- review fixes: a flagged-but-not-strong device stays a WEAK match under an attack verdict ----------


async def human_declines(db, fp: str, n: int) -> None:
    """Honest refusals (insufficient funds) on the attacked item from one real shopper."""
    for _ in range(n):
        db.add(
            CheckoutAttempt(
                shop_id=SHOP,
                source="simulator",
                paypal_live=False,
                sku=CHEAPEST_SKU,
                amount=Decimal("3.10"),
                method="card",
                fingerprint_hash=fp,
                email_domain="gmail.com",
                card_bin6="411111",
                card_last4="1111",
                card_expiry="2030-01",
                guard_decision=Decision.ALLOW,
                outcome=Outcome.DECLINED,
                decline_code="INSUFFICIENT_FUNDS",
                decline_category=DeclineCategory.CUSTOMER,
            )
        )
    await db.commit()


def test_captures_never_count_as_tries_on_the_targeted_item():
    declined = dict(outcome=Outcome.DECLINED, decline_category=DeclineCategory.CUSTOMER)
    rows = [row("gift-buyer") for _ in range(3)] + [row("retry", **declined), row("retry"), row("retry")]
    rows += [row("outage", Outcome.ERROR) for _ in range(3)]
    sig = sig_of(rows)
    assert sig.fingerprints == [] and sig.weak_fingerprints == ["retry"]


@pytest.mark.parametrize("declines", [2, 3])
async def test_a_human_with_honest_declines_is_not_auto_refunded_under_an_attack_verdict(
    db, monkeypatch, declines
):
    await attack(db, monkeypatch)
    fp = fingerprint(f"unlucky-human-{declines}", "Mozilla/5.0")
    await human_declines(db, fp, declines)
    paid = await captured(db, fp, card_bin6="411111", card_last4="1111", card_expiry="2030-01")

    await IncidentService(db, None, REASSESS).tick(SHOP)

    # With 3 refusals the device is flagged and this very pass blocks it; that block must not make the
    # capture a STRONG match (it did before: blocked_device), so the owner still decides.
    assert (fp in await blocked(db)) == (declines == 3)
    assert (await db.get(CheckoutAttempt, paid.id, populate_existing=True)).outcome == Outcome.CAPTURED
    [refund] = await refunds_for(db, paid.id)
    assert (refund.status, refund.policy_rule) == (
        ActionStatus.AWAITING_APPROVAL,
        "R5_weak_match_needs_owner",
    )
    assert refund.params["match"] == "weak" and "blocked_device" not in refund.params["matched_on"]


async def test_only_a_block_from_another_incident_that_saw_a_strong_bot_counts_as_strong(db):
    inc_a = await incident_with(db, {"strong_fingerprints": ["bot"], "fingerprints": ["bot", "weakling"]})
    inc_b = await incident_with(db, {})
    now = datetime.now(UTC)
    await devices.block(db, SHOP, ["bot", "weakling"], inc_a.id, {}, now)
    await db.commit()

    assert await devices.blocked_as_strong(db, SHOP, now, inc_b.id) == {"bot"}
    assert await devices.blocked_as_strong(db, SHOP, now, inc_a.id) == frozenset()


async def test_with_autopilot_off_the_approval_row_still_says_3ds_passed(db):
    ms = await db.get(MerchantSettings, SHOP)
    ms.autopilot = False
    await db.commit()
    a = await captured(db, "bot", guard_decision=Decision.ALLOW_WITH_SCA)
    inc = await incident_with(db, {"strong_fingerprints": ["bot"]})

    [action] = await Executor(db, None).refund_matched(
        inc, PlannedAction(ActionType.REFUND, "R5_refund_matched", needs_approval=True)
    )

    assert action.policy_rule == "R5_sca_passed_needs_owner" and action.params["sca_passed"] is True
    assert (await db.get(CheckoutAttempt, a.id, populate_existing=True)).outcome == Outcome.CAPTURED


# ---- review fixes: disputes fail closed ----------------------------------------------------------------


def resolved(code: str | None, dispute_id: str = "PP-D-1") -> dict:
    return {"dispute_id": dispute_id, "dispute_outcome": {"outcome_code": code} if code else {}}


@pytest.mark.parametrize("code", [None, "NONE", "SOMETHING_NEW", "RESOLVED_WITH_PAYOUT"])
def test_a_resolution_without_a_seller_side_outcome_keeps_the_capture_disputed(code):
    a = CheckoutAttempt(disputed=False)
    apply_dispute(a, "CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-1", "status": "OPEN"})
    apply_dispute(a, "CUSTOMER.DISPUTE.RESOLVED", resolved(code))
    assert a.disputed and a.dispute_status.startswith("RESOLVED:")


@pytest.mark.parametrize("code", ["RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"])
def test_only_seller_side_outcomes_clear_disputed(code):
    a = CheckoutAttempt(disputed=False)
    apply_dispute(a, "CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-1", "status": "OPEN"})
    apply_dispute(a, "CUSTOMER.DISPUTE.RESOLVED", resolved(code))
    assert not a.disputed


def test_a_second_dispute_reopens_and_a_buyer_payout_is_never_cleared():
    a = CheckoutAttempt(disputed=False)
    apply_dispute(a, "CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-1", "status": "OPEN"})
    apply_dispute(a, "CUSTOMER.DISPUTE.RESOLVED", resolved("RESOLVED_SELLER_FAVOUR"))
    assert not a.disputed
    apply_dispute(a, "CUSTOMER.DISPUTE.UPDATED", {"dispute_id": "PP-D-1", "status": "UNDER_REVIEW"})
    assert not a.disputed  # late event for the resolved dispute
    apply_dispute(a, "CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-2", "status": "OPEN"})
    assert (a.disputed, a.dispute_id, a.dispute_status) == (True, "PP-D-2", "OPEN")
    apply_dispute(a, "CUSTOMER.DISPUTE.RESOLVED", resolved("RESOLVED_SELLER_FAVOUR", "PP-D-1"))
    assert a.disputed  # the old dispute's (repeated) ending says nothing about the open one

    paid = CheckoutAttempt(disputed=False)
    apply_dispute(paid, "CUSTOMER.DISPUTE.RESOLVED", resolved("RESOLVED_BUYER_FAVOUR", "PP-D-1"))
    apply_dispute(paid, "CUSTOMER.DISPUTE.CREATED", {"dispute_id": "PP-D-2", "status": "OPEN"})
    apply_dispute(paid, "CUSTOMER.DISPUTE.RESOLVED", resolved("RESOLVED_SELLER_FAVOUR", "PP-D-2"))
    assert paid.disputed  # the buyer was already paid once


async def test_the_sweeper_never_re_sends_a_refund_for_a_capture_disputed_while_in_flight(db, sandbox):
    res = await place_order(db, sandbox.client, card_req())
    a = await db.get(CheckoutAttempt, res.attempt_id)
    incident = Incident(shop_id=SHOP)
    db.add(incident)
    await db.flush()
    sandbox.refund_outage = 1  # PayPal never got it
    ex = Executor(db, sandbox.client)
    params = {"attempt_id": a.id, "capture_id": a.capture_id, "amount": str(a.amount)}
    action = await ex.execute_refund(incident, a, params, "R4", approved_by="auto")
    await db.commit()
    a.disputed, a.dispute_status = True, "OPEN"
    await db.commit()
    posts = len(sandbox.posts(f"/v2/payments/captures/{a.capture_id}/refund"))

    [swept] = await ex.sweep_in_flight(SHOP, now=datetime.now(UTC) + SWEEP_AFTER * 2)

    assert swept.id == action.id and swept.status == ActionStatus.FAILED and "disputed" in swept.error
    assert len(sandbox.posts(f"/v2/payments/captures/{a.capture_id}/refund")) == posts
    assert sandbox.refunds == {}
    assert await events(db, "refund.sweep_disputed")


# ---- review fixes: unconfirmed refunds reach the owner -------------------------------------------------


async def test_the_report_never_says_no_action_needed_while_a_refund_is_unconfirmed(db):
    from app.ai import reporter

    a = await captured(db, "bot")
    b = await captured(db, "bot2")
    inc = await incident_with(db, {"strong_fingerprints": ["bot", "bot2"]})
    inc.classification, inc.confidence = "card_testing", 0.95
    # a: failed once, then refunded on a retry (settled); b: failed for good.
    tries = ((a, ActionStatus.FAILED), (a, ActionStatus.EXECUTED), (b, ActionStatus.FAILED))
    for attempt, status in tries:
        db.add(
            Action(
                incident_id=inc.id,
                type=ActionType.REFUND,
                policy_rule="R4_attack",
                status=status,
                params={
                    "attempt_id": attempt.id,
                    "capture_id": attempt.capture_id,
                    "amount": "3.10",
                    "matched_on": ["strong_fingerprint"],
                },
            )
        )
    await db.commit()

    texts, model_id = await reporter.write(db, inc, skip_ai=True)

    assert model_id == "template" and "No action needed" not in texts["alert_text"]
    assert "1 refunds were not confirmed by PayPal" in texts["alert_text"]
    # Offline-simulated captures are labelled as such in the evidence pack's transactions table.
    assert f"| {b.capture_id} (simulated) | $3.10 | not refunded (check in PayPal) |" in texts["evidence_md"]
    assert (
        f"| {a.capture_id} (simulated) | $3.10 | refunded | device used several cards or accounts |"
        in texts["evidence_md"]
    )
