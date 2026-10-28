"""Truthful reporting: alerts and evidence packs built from what actually ran, no AI-written numbers in
the analyst's prose, and the alert history (+ optional chat webhook)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from app import notify
from app.ai import analyst, reporter
from app.ai.schemas import Evidence, ReportDraft, Verdict
from app.api.deps import get_paypal, require_owner
from app.config import Settings
from app.db.audit import audit
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    Decision,
    DeviceEntry,
    Incident,
    MerchantSettings,
    Outcome,
)
from app.db.session import get_session
from app.detection.features import Features
from app.incidents.service import IncidentService
from app.main import app
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import place_order
from app.simulator.runner import to_request
from app.simulator.scenarios import card_testing
from app.simulator.seed import seed_history
from tests.conftest import SHOP
from tests.test_pipeline import fake_ai

# ---- template alert: assembled from the actions that ran (pure) ----------------------------------------


def facts(**kw) -> dict:
    """A contained card-testing attack with nothing left for the owner; tests override one thing each."""
    base = {
        "classification": reporter.LABELS["card_testing"],
        "targeted_item": "Tealight 3-pack",
        "defences": "; ".join(reporter.DEFENCE_TEXT[t] for t in (ActionType.SCA_ON, ActionType.BLOCK_FP)),
        "refunded_count": "3",
        "refunded_total": "$9.30",
        "refunds_unresolved": "0",
        "pending_refunds": "0",
        "awaiting_approval_count": "0",
        "disputed_count": "0",
        "dispute_capture_ids": "none",
        "recommended_only": "",
        "ai_status": reporter.AI_STATUS["live"],
        "_ai": "live",
        "_executed": [ActionType.SCA_ON, ActionType.BLOCK_FP, ActionType.REFUND],
        "_sale": False,
        "_closed": False,
    }
    return base | kw


def alert(f: dict) -> str:
    return reporter.fill(reporter.template(f).alert, f)


def test_all_clear_only_when_real_defences_ran_and_nothing_waits():
    text = alert(facts())
    assert "No action needed" in text and "I refunded 3 test payments ($9.30)" in text
    assert "blocked the devices" in text


@pytest.mark.parametrize(
    "override, expect",
    [
        ({"awaiting_approval_count": "2", "pending_refunds": "2"}, "2 refunds wait for your approval"),
        ({"refunds_unresolved": "1"}, "1 refunds were not confirmed by PayPal"),
        ({"disputed_count": "1"}, "1 payments are disputed"),
        (
            {"_executed": [ActionType.SCA_ON], "defences": reporter.DEFENCE_TEXT[ActionType.SCA_ON]},
            "3-D Secure",
        ),
        ({"_sale": True}, "Your sale is on"),
        ({"_ai": "rules", "ai_status": reporter.AI_STATUS["rules"]}, "fixed safety rules decided"),
        ({"_ai": "kept", "ai_status": reporter.AI_STATUS["kept"]}, "its last verdict stands"),
        ({"recommended_only": "refunds (recommended only)"}, "Not applied: refunds (recommended only)"),
    ],
)
def test_never_no_action_needed_when_something_is_open(override, expect):
    text = alert(facts(**override))
    assert "No action needed" not in text and expect in text


def test_alert_claims_only_what_ran():
    """Kill switch on (R1): an attack verdict but nothing executed. It used to say "I've blocked it"."""
    f = facts(defences=reporter.NO_DEFENCES, refunded_count="0", _executed=[])
    text = alert(f)
    assert "blocked" not in text.replace("Nothing was blocked", "") and "refunded" not in text
    assert "Nothing was blocked" in text and "No action needed" not in text


def test_worst_case_alert_fits_and_keeps_the_urgent_parts():
    every = "; ".join(
        reporter.DEFENCE_TEXT[t] for t in (ActionType.SCA_ON, ActionType.BLOCK_FP, ActionType.HOLD_SKU)
    )
    f = facts(
        classification=reporter.LABELS["account_testing"],
        defences=every,
        refunds_unresolved="12",
        awaiting_approval_count="14",
        disputed_count="3",
        refunded_count="40",
        refunded_total="$124.00",
        _ai="kept",
        _sale=True,
        _executed=[ActionType.SCA_ON, ActionType.BLOCK_FP, ActionType.HOLD_SKU, ActionType.REFUND],
    )
    text = reporter.render(reporter.template(f), f)["alert_text"]
    assert len(text) <= reporter.ALERT_MAX and text.endswith(".")
    assert "12 refunds were not confirmed" in text and "14 refunds wait" in text
    assert "I turned on 3-D Secure, blocked the devices and paused the targeted item" in text


# ---- AI drafts must not drop what the owner has to know (pure) -----------------------------------------


@pytest.fixture
def ai_draft(monkeypatch):
    def use(draft: ReportDraft):
        async def fake(**kw):
            return draft, "mock-model"

        monkeypatch.setattr("app.ai.reporter.generate_json", fake)

    return use


def draft(**kw) -> ReportDraft:
    return ReportDraft(
        **{
            "alert": "A {{classification}} was handled. No action needed tonight.",
            "report_md": "# Report",
            "evidence_md": "The merchant refunded the test payments before any dispute was opened.",
        }
        | kw
    )


async def test_ai_alert_saying_no_action_needed_is_replaced_while_approvals_wait(ai_draft):
    ai_draft(draft())
    f = facts(awaiting_approval_count="2", refund_table="")
    composed, model_id = await reporter.compose(Incident(), f)
    assert model_id == "mock-model"
    assert composed.alert == reporter.template(f).alert  # the template, with the approvals sentence
    ok = facts(refund_table="")
    composed, _ = await reporter.compose(Incident(), ok)
    assert composed.alert == draft().alert  # all clear: the model's wording stands


async def test_ai_drafts_with_unknown_placeholders_fall_back(ai_draft):
    ai_draft(draft(alert="Attack on {{store_name}}.", report_md="# {{made_up}}"))
    f = facts(refund_table="")
    composed, _ = await reporter.compose(Incident(), f)
    assert composed.alert == reporter.template(f).alert
    assert composed.report_md == reporter.template(f).report_md
    assert "made_up" not in reporter.PLACEHOLDERS and "awaiting_approval_count" in reporter.PLACEHOLDERS


async def test_ai_evidence_that_ignores_a_dispute_is_replaced(ai_draft):
    ai_draft(draft())
    f = facts(disputed_count="1", dispute_capture_ids="CAP-1", refund_table="")
    composed, _ = await reporter.compose(Incident(), f)
    assert "before any dispute" not in composed.evidence_md and "{{disputed_count}}" in composed.evidence_md


async def test_an_ai_alert_that_would_not_fit_after_filling_falls_back(ai_draft):
    """Twenty capture ids push the approvals sentence past the 400-character cut."""
    ids = ", ".join(f"7XY{i:07d}AB" for i in range(20))
    f = facts(awaiting_approval_count="2", refunded_capture_ids=ids, refund_table="")
    ai_draft(
        draft(alert="I refunded {{refunded_capture_ids}}. {{awaiting_approval_count}} refunds wait for you.")
    )
    composed, _ = await reporter.compose(Incident(), f)
    assert composed.alert == reporter.template(f).alert
    text = reporter.render(composed, f)["alert_text"]
    assert "2 refunds wait for your approval" in text and len(text) <= reporter.ALERT_MAX
    # Short once filled, but a list placeholder is still never allowed in a phone notification.
    ai_draft(draft(alert="Handled. Refunded {{refund_ids}}. No action needed tonight."))
    composed, _ = await reporter.compose(Incident(), facts(refund_ids="R1", refund_table=""))
    assert "{{refund_ids}}" not in composed.alert


async def test_an_ai_report_saying_no_action_needed_is_replaced_while_approvals_wait(ai_draft):
    ai_draft(draft(report_md="# All handled\n\nNo action needed, everything is handled."))
    f = facts(awaiting_approval_count="3", refund_table="")
    composed, _ = await reporter.compose(Incident(), f)
    assert composed.report_md == reporter.template(f).report_md
    ok = "# All handled\n\n{{awaiting_approval_count}} refunds wait for your approval."
    ai_draft(draft(report_md=ok))
    composed, _ = await reporter.compose(Incident(), f)
    assert composed.report_md == ok


@pytest.mark.parametrize(
    "override",
    [
        {"refunded_count": "0"},  # nothing was refunded
        {"disputed_count": "1", "dispute_capture_ids": "CAP-1"},  # mentions the count, still claims "before"
        {"_resolved_disputes": True},
    ],
)
async def test_ai_evidence_claiming_refunds_came_before_any_dispute_must_be_true(ai_draft, override):
    ev = "{{disputed_count}} disputed. The merchant refunded the test payments before any dispute was opened."
    ai_draft(draft(evidence_md=ev))
    f = facts(refund_table="", **override)
    composed, _ = await reporter.compose(Incident(), f)
    assert composed.evidence_md == reporter.template(f).evidence_md
    assert "before any dispute" not in composed.evidence_md
    composed, _ = await reporter.compose(Incident(), facts(refund_table=""))
    assert composed.evidence_md == ev  # true here: refunds ran and no dispute exists


# ---- DB facts ------------------------------------------------------------------------------------------


async def capture(db, fp: str, **kw) -> CheckoutAttempt:
    a = CheckoutAttempt(
        **{
            "shop_id": SHOP,
            "source": "simulator",
            "paypal_live": False,
            "sku": CHEAPEST_SKU,
            "amount": Decimal("3.10"),
            "method": "card",
            "fingerprint_hash": fp,
            "email_domain": "gmail.com",
            "guard_decision": Decision.ALLOW,
            "outcome": Outcome.CAPTURED,
        }
        | kw
    )
    db.add(a)
    await db.flush()
    a.capture_id = a.capture_id or f"CAP-{a.id}"
    return a


async def attack_incident(db, **kw) -> Incident:
    inc = Incident(
        shop_id=SHOP,
        opened_at=datetime.now(UTC) - timedelta(minutes=5),
        signature={"top_sku": CHEAPEST_SKU},
        classification="card_testing",
        confidence=0.95,
        model_id="mock-model",
        verdict={"summary": "Bots are testing cards.", "recommended_actions": ["SCA_ON", "BLOCK_FP"]},
        **kw,
    )
    db.add(inc)
    await db.flush()
    return inc


def refund(inc: Incident, a: CheckoutAttempt, status: ActionStatus, **kw) -> Action:
    params = {
        "attempt_id": a.id,
        "capture_id": a.capture_id,
        "amount": str(a.amount),
        "matched_on": ["strong_fingerprint"],
    }
    return Action(
        incident_id=inc.id,
        type=ActionType.REFUND,
        policy_rule="R5_refund_matched",
        status=status,
        params=params | kw.pop("params", {}),
        **kw,
    )


async def test_evidence_pack_lists_refunded_captures_refund_ids_and_simulated_rows(db):
    inc = await attack_incident(db)
    a = await capture(db, "bot", outcome=Outcome.REFUNDED, refund_id="SIMREF-AAA")
    b = await capture(db, "bot2", paypal_live=True, capture_id="7XY12345AB", processor_code="0000")
    db.add(refund(inc, a, ActionStatus.EXECUTED, paypal_ref="SIMREF-AAA", params={"simulated": True}))
    db.add(refund(inc, b, ActionStatus.AWAITING_APPROVAL))
    db.add(Action(incident_id=inc.id, type=ActionType.SCA_ON, policy_rule="R4_attack", status="executed"))
    await db.commit()

    f = await reporter.gather_facts(db, inc)
    assert f["refunded_capture_ids"] == f"{a.capture_id} (simulated)"
    assert f["refund_ids"] == "SIMREF-AAA (simulated)"
    assert f["awaiting_approval_count"] == "1" and f["disputed_count"] == "0"
    # Only 3-D Secure ran: the AI's BLOCK_FP recommendation was never applied.
    assert f["recommended_only"] == "device blocks (recommended only)"
    texts = reporter.render(reporter.template(f), f)
    ev = texts["evidence_md"]
    assert (
        f"| {a.capture_id} (simulated) | $3.10 | refunded | device used several cards or accounts "
        "| SIMREF-AAA (simulated) | refunded | - | no |"
    ) in ev
    assert "| 7XY12345AB | $3.10 | waiting for the merchant |" in ev and "| captured | 0000 | no |" in ev
    assert "before any dispute was opened" in ev
    assert "No action needed" not in texts["alert_text"] and "1 refunds wait" in texts["alert_text"]
    assert "device blocks (recommended only)" in texts["report_md"]


async def test_a_disputed_capture_changes_the_evidence_text(db):
    inc = await attack_incident(db)
    a = await capture(db, "bot", outcome=Outcome.REFUNDED)
    disputed = await capture(db, "bot3", disputed=True, dispute_status="OPEN", dispute_id="PP-D-1")
    block(db, inc, "bot3")
    db.add(refund(inc, a, ActionStatus.EXECUTED, paypal_ref="SIMREF-B", params={"simulated": True}))
    await db.commit()

    f = await reporter.gather_facts(db, inc)
    assert f["disputed_count"] == "1" and f["dispute_capture_ids"] == disputed.capture_id
    texts = reporter.render(reporter.template(f), f)
    ev = texts["evidence_md"]
    assert "before any dispute was opened" not in ev
    assert f"1 payments from this incident have an open dispute ({disputed.capture_id})" in ev
    assert f"| {disputed.capture_id} (simulated) | $3.10 | not refunded (disputed" in ev
    assert "| yes (open) |" in ev
    assert "No action needed" not in texts["alert_text"] and "1 payments are disputed" in texts["alert_text"]


def block(db, inc: Incident, fp: str) -> None:
    now = datetime.now(UTC)
    db.add(
        DeviceEntry(
            shop_id=SHOP,
            fingerprint=fp,
            kind="block",
            incident_id=inc.id,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )


@pytest.mark.parametrize(
    "disputed, outcome",
    [(False, "RESOLVED_SELLER_FAVOUR"), (True, "RESOLVED_BUYER_FAVOUR")],
)
async def test_a_resolved_dispute_is_history_not_something_to_answer(db, disputed, outcome):
    """webhook_processor.apply_dispute keeps the dispute_id after a resolution (and `disputed` when the
    buyer was paid): nothing is left to answer, so it must not hold back "no action needed"."""
    inc = await attack_incident(db)
    a = await capture(db, "bot", outcome=Outcome.REFUNDED)
    gone = await capture(
        db, "bot3", disputed=disputed, dispute_id="PP-D-1", dispute_status=f"RESOLVED:{outcome}"
    )
    block(db, inc, "bot3")
    db.add(refund(inc, a, ActionStatus.EXECUTED, paypal_ref="SIMREF-B", params={"simulated": True}))
    db.add(Action(incident_id=inc.id, type=ActionType.SCA_ON, policy_rule="R4_attack", status="executed"))
    db.add(Action(incident_id=inc.id, type=ActionType.BLOCK_FP, policy_rule="R4_attack", status="executed"))
    await db.commit()

    f = await reporter.gather_facts(db, inc)
    assert f["disputed_count"] == "0" and f["dispute_capture_ids"] == "none" and f["_resolved_disputes"]
    texts = reporter.render(reporter.template(f), f)
    assert "disputed" not in texts["alert_text"] and "No action needed" in texts["alert_text"]
    ev = texts["evidence_md"]
    assert "before any dispute" not in ev and "open dispute" not in ev and reporter.RESOLVED_TEXT in ev
    assert f"| {gone.capture_id} (simulated) | $3.10 | not refunded (dispute resolved) |" in ev
    assert f"| resolved ({outcome.lower()}) |" in ev


async def test_a_dispute_on_an_unrelated_order_in_the_window_is_not_part_of_the_incident(db):
    inc = await attack_incident(db)
    await capture(db, "real-customer", disputed=True, dispute_status="OPEN", dispute_id="PP-D-9")
    await db.commit()
    f = await reporter.gather_facts(db, inc)
    assert f["disputed_count"] == "0" and f["refund_table"] == ""
    assert "dispute" not in reporter.render(reporter.template(f), f)["alert_text"]


async def test_devices_blocked_counts_the_device_table_and_ai_status_is_reported(db):
    now = datetime.now(UTC)
    inc = await attack_incident(db, ai_refresh_failed_at=now)
    other = await attack_incident(db)
    for fp, incident_id in (("d1", inc.id), ("d2", inc.id), ("d3", other.id)):
        db.add(
            DeviceEntry(
                shop_id=SHOP,
                fingerprint=fp,
                kind="block",
                incident_id=incident_id,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    # One expired-and-released block of this incident still counts: it was blocked by this incident.
    db.add(
        DeviceEntry(
            shop_id=SHOP,
            fingerprint="d4",
            kind="block",
            incident_id=inc.id,
            created_at=now - timedelta(days=2),
            expires_at=now - timedelta(days=1),
            released_at=now - timedelta(days=1),
        )
    )
    await db.commit()

    f = await reporter.gather_facts(db, inc)
    assert f["devices_blocked"] == "3"
    assert f["ai_status"] == reporter.AI_STATUS["kept"] and f["_ai"] == "kept"
    # P9: the scorecard decides, so Gemini being down only means "rules" when the scorecard was unsure
    # too (an attack the scorecard is sure of is a live verdict whoever wrote the words).
    inc.ai_available, inc.ai_refresh_failed_at = False, None
    assert (await reporter.gather_facts(db, inc))["_ai"] == "live"
    inc.classification = "unclear"
    assert (await reporter.gather_facts(db, inc))["_ai"] == "rules"


async def test_the_report_says_who_decided_and_who_wrote_the_words(db):
    inc = await attack_incident(db)
    inc.classifier_version, inc.decided_by, inc.model_id = "scorecard-v1", "scorecard", "template"
    f = await reporter.gather_facts(db, inc)
    assert f["decision_path"] == "scorecard-v1; explained by Checkout Guard's own template"
    inc.decided_by, inc.model_id = "scorecard+gemini_agree", "gemini-x"
    f = await reporter.gather_facts(db, inc)
    assert f["decision_path"] == "scorecard-v1, confirmed by the AI second opinion; explained by gemini-x"
    report = reporter.render(reporter.template(f), f)
    assert "**Decided by:** scorecard-v1, confirmed by the AI second opinion" in report["report_md"]
    assert "**Decided by:**" in report["evidence_md"]


async def test_an_incident_closed_before_its_first_verdict_does_not_blame_the_ai(db):
    inc = await attack_incident(db)
    inc.classification, inc.verdict, inc.closed_at = None, None, datetime.now(UTC)
    await db.commit()
    f = await reporter.gather_facts(db, inc)
    assert f["_ai"] == "none" and f["ai_status"] == reporter.AI_STATUS["none"]
    text = reporter.render(reporter.template(f), f)["alert_text"]
    assert "unavailable" not in text and "before a verdict was reached" in text
    assert "No action needed" not in text


async def test_the_rules_fallback_list_is_not_reported_as_recommended_only(db):
    """Gemini down during a PayPal outage: R3d plans NOTIFY only. The fallback verdict's fixed
    ['SCA_ON', 'NOTIFY'] is nobody's recommendation, so no "3-D Secure (recommended only)"."""
    inc = await attack_incident(db)
    inc.classification, inc.model_id, inc.ai_available = "unclear", "rules-fallback", False
    inc.verdict = analyst.fallback_verdict("Gemini timed out").model_dump()
    audit(db, "policy", "decided", inc.id, actions=["NOTIFY:R3d_outage"])
    await db.commit()
    f = await reporter.gather_facts(db, inc)
    assert f["recommended_only"] == "" and f["_ai"] == "rules"
    assert "Not applied" not in reporter.render(reporter.template(f), f)["report_md"]


async def test_recommendation_the_policy_took_up_is_not_recommended_only(db):
    inc = await attack_incident(db)
    audit(db, "policy", "decided", inc.id, actions=["SCA_ON:R4_attack", "BLOCK_FP:R4_attack"])
    await db.commit()
    assert (await reporter.gather_facts(db, inc))["recommended_only"] == ""


async def test_autopilot_off_with_pending_refunds_never_says_no_action_needed(db, monkeypatch):
    fn = fake_ai("card_testing", 0.95)
    monkeypatch.setattr("app.ai.analyst.generate_json", fn)
    monkeypatch.setattr("app.ai.reporter.generate_json", fn)
    ms = await db.get(MerchantSettings, SHOP)
    ms.autopilot = False
    await db.commit()
    await seed_history(db, SHOP, days=7)
    for a in list(card_testing("offline", count=60, seed=1)):
        await place_order(db, None, to_request(SHOP, "test", a, "offline"))

    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    pending = (
        await db.execute(select(Action).where(Action.status == ActionStatus.AWAITING_APPROVAL))
    ).scalars()
    n = len(list(pending))
    assert n > 0

    texts, _ = await reporter.write(db, incident, skip_ai=True)
    assert "No action needed" not in texts["alert_text"]
    assert f"{n} refunds wait for your approval" in texts["alert_text"]
    assert f"**Refunds awaiting the merchant's approval:** {n}" in texts["evidence_md"]
    assert "**Refunded captures:** none" in texts["evidence_md"]
    fps = (
        await db.execute(select(DeviceEntry.fingerprint).where(DeviceEntry.incident_id == incident.id))
    ).all()
    assert f"{len(fps)} devices blocked by this incident" in texts["evidence_md"]


# ---- number guard on the analyst's prose (pure) --------------------------------------------------------

FEATURES = {15: Features(window_min=15, attempts=60, decline_rate=0.8, unique_fingerprints=4)}


def verdict(summary: str, why: str = "Most payments were declined.", notes: str = "") -> Verdict:
    return Verdict(
        classification="card_testing",
        confidence=0.95,
        summary=summary,
        evidence=[Evidence(feature="decline_rate", window_min=15, observed=0.8, why=why)],
        recommended_actions=["SCA_ON"],
        uncertainty_notes=notes,
    )


def test_an_unmatched_number_in_the_summary_is_replaced_from_validated_evidence():
    v = verdict("A bot made 45 attempts on the cheapest candle.")
    assert analyst.number_guard(v, FEATURES) == 1
    assert "45" not in v.summary
    lead = analyst.SUMMARY_LEAD["card_testing"]
    assert v.summary == f"{lead} Main signals: decline rate 80% over 15 min."


def test_numbers_matching_computed_features_pass():
    v = verdict("About 60 attempts in 15 minutes, 80% declined, from 4 devices. 3-D Secure is on.")
    assert analyst.number_guard(v, FEATURES) == 0 and v.summary.startswith("About 60 attempts")


def test_why_and_uncertainty_notes_are_guarded_too():
    v = verdict(
        "Bots are testing cards.",
        why="Declines jumped to 95 percent.",
        notes="Volume is high. Roughly 300 cards were tried. Some returning customers too.",
    )
    assert analyst.number_guard(v, FEATURES) == 2
    assert v.evidence[0].why == "Decline rate was 80% over 15 min."
    assert "300" not in v.uncertainty_notes and v.uncertainty_notes.startswith("Volume is high.")
    assert "Some returning customers too." in v.uncertainty_notes and "were removed" in v.uncertainty_notes


def test_a_percentage_only_matches_a_rate_and_number_words_are_not_trusted():
    # 60 is the attempts count, not a rate: "60%" must not pass because some count equals 60.
    assert not analyst.numbers_match("60% of payments failed.", analyst._feature_values(FEATURES))
    assert analyst.numbers_match("80% of payments failed.", analyst._feature_values(FEATURES))
    v = verdict("A bot made forty-five attempts.")
    assert analyst.number_guard(v, FEATURES) == 1 and "forty" not in v.summary
    assert analyst.numbers_match("One device used two cards, which happens often.", [])


async def test_analyse_applies_the_number_guard(monkeypatch):
    async def fake(**kw):
        return verdict("A bot made 45 attempts."), "mock-model"

    monkeypatch.setattr("app.ai.analyst.generate_json", fake)
    res = await analyst.analyse({"x": 1}, FEATURES)
    assert "45" not in res.verdict.summary and res.numbers_replaced == 1
    assert "no digits" in analyst.SYSTEM.lower() or "never write digits" in analyst.SYSTEM.lower()


# ---- alert history and the optional chat webhook -------------------------------------------------------

HOOK = "https://hooks.example.test/T000/B000"


@pytest.fixture
def webhook(monkeypatch):
    def use(url: str):
        settings = Settings(_env_file=None, notify_webhook_url=url, notify_timeout_s=0.5)
        monkeypatch.setattr(notify, "get_settings", lambda: settings)

    return use


@pytest.fixture
async def client(db):
    async def session():
        yield db

    app.dependency_overrides.update(
        {get_paypal: lambda: None, get_session: session, require_owner: lambda: SHOP}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_every_alert_is_recorded_and_listed_newest_first(db, client):
    inc = await attack_incident(db)
    await db.commit()
    svc = IncidentService(db, None, timedelta(minutes=2))
    first = (await svc.write_report(inc, skip_ai=True)).alert_text
    db.add(Action(incident_id=inc.id, type=ActionType.BLOCK_FP, policy_rule="R4_attack", status="executed"))
    await db.commit()
    second = (await svc.write_report(inc, skip_ai=True)).alert_text

    q = select(AuditLog).where(AuditLog.event == "alert.sent").order_by(AuditLog.id)
    rows = (await db.execute(q)).scalars().all()
    assert [r.data["text"] for r in rows] == [first, second]
    body = (await client.get("/dashboard/alerts", params={"limit": 1})).json()
    assert len(body) == 1 and body[0]["text"] == second and body[0]["incident_id"] == inc.id
    assert len((await client.get("/dashboard/alerts")).json()) == 2
    assert (await client.get("/dashboard/alerts", params={"limit": 0})).status_code == 422


async def test_alert_is_posted_to_the_webhook_after_commit(db, webhook):
    webhook(HOOK)
    inc = await attack_incident(db)
    await db.commit()
    with respx.mock(assert_all_called=True) as router:
        route = router.post(HOOK).mock(return_value=httpx.Response(200))
        report = await IncidentService(db, None, timedelta(minutes=2)).write_report(inc, skip_ai=True)
        await notify.drain()
    sent = route.calls.last.request
    assert sent.read() and report.alert_text in sent.content.decode()
    # The history row was committed before the post was even scheduled.
    assert (await db.execute(select(AuditLog).where(AuditLog.event == "alert.sent"))).scalar_one()


async def test_the_owners_regenerate_neither_pushes_nor_adds_to_the_history(db, webhook):
    webhook(HOOK)
    inc = await attack_incident(db)
    await db.commit()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(HOOK).mock(return_value=httpx.Response(200))
        svc = IncidentService(db, None, timedelta(minutes=2))
        report = await svc.write_report(inc, skip_ai=True, notify_owner=False)
        await notify.drain()
    assert report is not None and report.alert_text and not route.called
    assert not (await db.execute(select(AuditLog).where(AuditLog.event == "alert.sent"))).first()


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout("slow"), httpx.Response(500)])
async def test_a_failing_webhook_is_only_logged(db, webhook, failure):
    webhook(HOOK)
    inc = await attack_incident(db)
    await db.commit()
    with respx.mock() as router:
        router.post(HOOK).mock(side_effect=[failure])
        report = await IncidentService(db, None, timedelta(minutes=2)).write_report(inc, skip_ai=True)
        await notify.drain()
    assert report is not None and report.alert_text


def test_ntfy_topic_urls_get_ntfy_json_and_others_slack_text():
    url, body = notify.request_for("https://ntfy.sh/shop-alerts", "hello", "inc1")
    assert url == "https://ntfy.sh/" and body == {
        "topic": "shop-alerts",
        "title": "Checkout Guard",
        "message": "hello",
    }
    url, body = notify.request_for(HOOK, "hello", "inc1")
    assert url == HOOK and body["text"] == "Checkout Guard: hello"


def test_no_webhook_configured_schedules_nothing(webhook):
    webhook("")
    notify.send("hello", "inc1")
    assert not notify._tasks
