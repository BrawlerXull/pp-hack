"""Dispute assistant (app/disputes): fixed rules recommend from our records, the AI reads the buyer and drafts
the note (never a number of its own), the owner decides, PayPal's Disputes API gets the answer."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_paypal, require_owner
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    Decision,
    DisputeCase,
    Incident,
    Outcome,
    Source,
)
from app.db.session import get_session
from app.disputes import advisor
from app.disputes.advisor import DisputeDraft, recommend
from app.disputes.respond import respond
from app.incidents.webhook_processor import process_pending
from app.main import app
from tests.conftest import SHOP

FACTS = {"refunded": False, "refund_id": None, "sca_authenticated": False, "attack_incident": None}


def test_the_rules_decide_from_our_records_in_order():
    assert recommend("UNAUTHORISED", {**FACTS, "refunded": True, "refund_id": "RF1"}) == (
        "contest",
        "D1_already_refunded",
        "PROOF_OF_REFUND",
        {"refund_ids": ["RF1"]},
    )
    assert recommend("UNAUTHORISED", {**FACTS, "sca_authenticated": True})[:2] == (
        "contest",
        "D2_3ds_authenticated",
    )
    # 3-D Secure only answers an unauthorised-use claim, not "item not received".
    assert (
        recommend("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", {**FACTS, "sca_authenticated": True})[0] == "review"
    )
    assert recommend("UNAUTHORISED", {**FACTS, "attack_incident": "inc1"})[:2] == (
        "accept",
        "D3_attack_payment",
    )
    assert recommend("UNAUTHORISED", FACTS)[:2] == ("review", "D4_review")
    # Seen live: PayPal refuses OTHER for "item not received" and accepts PROOF_OF_FULFILLMENT.
    assert recommend("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", FACTS)[2] == "PROOF_OF_FULFILLMENT"


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


async def attack_capture(db, live: bool = False) -> tuple[Incident, CheckoutAttempt]:
    inc = Incident(
        shop_id=SHOP,
        opened_at=datetime.now(UTC),
        classification="card_testing",
        confidence=0.97,
        signature={},
    )
    db.add(inc)
    await db.flush()
    a = CheckoutAttempt(
        shop_id=SHOP,
        source=Source.SIMULATOR,
        paypal_live=live,
        sku="SKU-TEALIGHT-3",
        amount=Decimal("3.00"),
        method="card",
        fingerprint_hash="bot-device",
        guard_decision=Decision.ALLOW,
        outcome=Outcome.CAPTURED,
        capture_id="CAP-ATTACK-1",
    )
    db.add(a)
    await db.flush()
    db.add(
        Action(
            incident_id=inc.id,
            type=ActionType.REFUND,
            policy_rule="R5_weak_match_needs_owner",
            status=ActionStatus.AWAITING_APPROVAL,
            params={"attempt_id": a.id, "capture_id": a.capture_id, "amount": "3.00"},
        )
    )
    await db.commit()
    return inc, a


async def test_a_chargeback_on_an_attack_payment_is_recommended_for_acceptance(client, db):
    inc, a = await attack_capture(db)
    res = await client.post("/demo/dispute")
    assert res.status_code == 200 and res.json()["attempt_id"] == a.id
    await process_pending(db)  # the simulated CUSTOMER.DISPUTE.CREATED marks the payment disputed
    assert await advisor.open_new_cases(db, SHOP) == 1
    assert await advisor.open_new_cases(db, SHOP) == 0  # one case per dispute

    (case,) = (await client.get("/dashboard/disputes")).json()
    assert (case["recommendation"], case["rule"], case["status"]) == ("accept", "D3_attack_payment", "open")
    assert case["incident_id"] == inc.id and case["reason"] == "UNAUTHORISED"
    # No Gemini key in tests: the fixed template, with our numbers filled in.
    assert case["note_by"] == "template" and "CAP-ATTACK-1" in case["note"] and "$3.00" in case["note"]

    done = await client.post(f"/dashboard/disputes/{case['id']}/accept")
    assert done.json()["status"] == "accepted" and done.json()["paypal_result"] == "simulated: no PayPal call"
    assert (await client.post(f"/dashboard/disputes/{case['id']}/accept")).status_code == 409


async def test_the_ai_reads_the_buyer_and_drafts_but_a_number_it_wrote_itself_is_refused(db, monkeypatch):
    facts = {
        **FACTS,
        "attack_incident": "inc",
        "capture_id": "CAP-9",
        "amount": "$3.00",
        "payment_date": "1 Nov",
    }

    async def ai_ok(**_kw):
        return (
            DisputeDraft(
                buyer_says="The buyer says they never shopped here, which fits an unauthorised charge.",
                note="We accept: payment {{capture_id}} for {{amount}} was made with a stolen card.",
            ),
            "gemini-test",
        )

    monkeypatch.setattr(advisor, "generate_json", ai_ok)
    note, by, buyer_says, _ = await advisor.draft(
        "D3_attack_payment", "accept", "UNAUTHORISED", ["hi"], facts
    )
    assert by == "gemini-test" and note == "We accept: payment CAP-9 for $3.00 was made with a stolen card."
    assert buyer_says and "never shopped" in buyer_says

    async def ai_invents(**_kw):
        return DisputeDraft(buyer_says="fine", note="We refunded 3 dollars on 1 November."), "gemini-test"

    monkeypatch.setattr(advisor, "generate_json", ai_invents)
    note, by, _, _ = await advisor.draft("D3_attack_payment", "accept", "UNAUTHORISED", [], facts)
    assert by == "template" and "CAP-9" in note


def test_buyer_messages_are_scrubbed_before_the_ai_sees_them():
    text = advisor._scrub("Call me on 0412 345 678 or jo.doe@example.com, card 4111 1111 1111 1111")
    assert "@" not in text and "4111" not in text and "0412" not in text


async def test_answers_reach_paypals_disputes_api(db, sandbox):
    _, a = await attack_capture(db, live=True)
    for dispute_id, rule, rec, evidence_type in (
        ("PP-D-1", "D3_attack_payment", "accept", None),
        ("PP-D-2", "D1_already_refunded", "contest", "PROOF_OF_REFUND"),
    ):
        case = DisputeCase(
            id=dispute_id,
            shop_id=SHOP,
            attempt_id=a.id,
            amount=Decimal("3.00"),
            recommendation=rec,
            rule=rule,
            evidence_type=evidence_type,
            evidence={"refund_ids": ["RF-77"]} if evidence_type else {},
            note="Drafted note.",
            note_by="template",
            paypal_live=True,
        )
        db.add(case)
        await db.flush()
        await respond(db, sandbox.client, case, rec, note=None)
        assert case.status == ("accepted" if rec == "accept" else "contested")
    await db.commit()

    (accept, contest) = sandbox.dispute_answers
    assert accept[:2] == ("PP-D-1", "accept-claim") and json.loads(accept[2]) == {"note": "Drafted note."}
    assert contest[:2] == ("PP-D-2", "provide-evidence")
    body = contest[2].decode()
    assert '"evidence_type": "PROOF_OF_REFUND"' in body and '"refund_ids": ["RF-77"]' in body


async def test_the_owner_can_edit_the_note_or_answer_in_paypal_instead(client, db):
    _, a = await attack_capture(db)
    db.add(
        DisputeCase(
            id="PP-D-3",
            shop_id=SHOP,
            attempt_id=a.id,
            amount=Decimal("3.00"),
            recommendation="review",
            rule="D4_review",
            note="draft",
            note_by="template",
            paypal_live=False,
        )
    )
    await db.commit()
    res = (
        await client.post("/dashboard/disputes/PP-D-3/contest", json={"note": "Tracking number attached."})
    ).json()
    assert (res["status"], res["note"], res["note_by"]) == ("contested", "Tracking number attached.", "owner")
    stored = (await db.execute(select(DisputeCase).where(DisputeCase.id == "PP-D-3"))).scalar_one()
    assert stored.decided_at is not None


async def test_disputes_are_synced_from_paypal_and_cases_follow_paypals_status(db):
    """Webhooks can be late (seen live: about three minutes): the worker also reads the Disputes API."""
    import respx

    from app.config import SANDBOX_API_BASE, Settings
    from app.disputes import sync
    from app.paypal.client import PayPalClient

    _, a = await attack_capture(db, live=True)
    dispute = {
        "dispute_id": "PP-R-SYNC-1",
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "update_time": "2026-10-29T17:38:13Z",
        "dispute_amount": {"currency_code": "USD", "value": "3.00"},
        "disputed_transactions": [{"seller_transaction_id": a.capture_id}],
        "messages": [{"posted_by": "BUYER", "content": "item not received"}],
    }
    settings = Settings(_env_file=None, paypal_client_id="id", paypal_client_secret="s")
    with respx.mock(base_url=SANDBOX_API_BASE) as pp:
        pp.post("/v1/oauth2/token").respond(json={"access_token": "t", "expires_in": 3600})
        pp.get("/v1/customer/disputes").respond(json={"items": [{"dispute_id": "PP-R-SYNC-1"}]})
        detail = pp.get("/v1/customer/disputes/PP-R-SYNC-1").respond(json=dispute)
        client = PayPalClient(settings, http=httpx.AsyncClient(base_url=SANDBOX_API_BASE), max_retries=0)
        assert await sync.sync(db, client, SHOP, force=True) == 1
        assert await sync.sync(db, client, SHOP, force=True) == 0  # unchanged: nothing new stored
        await process_pending(db)
        assert await advisor.open_new_cases(db, SHOP) == 1
        case = await db.get(DisputeCase, "PP-R-SYNC-1")
        # A confirmed attack's payment: accept, whatever the buyer's reason.
        assert (case.status, case.rule) == ("open", "D3_attack_payment")

        # The owner answered in PayPal itself: the case follows PayPal's status.
        detail.respond(
            json={**dispute, "status": "WAITING_FOR_BUYER_RESPONSE", "update_time": "2026-10-29T18:00:00Z"}
        )
        await sync.sync(db, client, SHOP, force=True)
        await db.refresh(case)
        assert case.status == "contested"
        await client.aclose()
