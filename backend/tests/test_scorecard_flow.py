"""P9 on the real test database: the scorecard's verdict drives the policy, Gemini only explains or breaks
ties, and everything the decision rests on is stored on the incident and in the audit log. Gemini is
always faked."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.ai import analyst
from app.db.models import (
    Action,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    Decision,
    GuardState,
    Outcome,
    Report,
    Source,
)
from app.detection import baseline as baseline_mod
from app.incidents.decision import DECIDED_BY_AGREE, DECIDED_BY_DISAGREE
from app.incidents.service import IncidentService, incident_event
from app.simulator.scenarios import card_testing
from app.simulator.seed import HOURLY_WEIGHTS, seed_history
from tests.conftest import SHOP, blocked
from tests.test_pipeline import fake_ai, force_scorecard, run_scenario

REASSESS = timedelta(minutes=2)


def use_ai(monkeypatch, classification: str, confidence: float = 0.99) -> None:
    fn = fake_ai(classification, confidence)
    monkeypatch.setattr("app.ai.analyst.generate_json", fn)
    monkeypatch.setattr("app.ai.reporter.generate_json", fn)


async def verdict_events(db, incident_id: str) -> list[dict]:
    q = select(AuditLog.data).where(AuditLog.incident_id == incident_id, AuditLog.event == "verdict")
    return list((await db.execute(q.order_by(AuditLog.id))).scalars())


async def test_the_incident_and_audit_log_record_how_the_scorecard_decided(db, monkeypatch):
    use_ai(monkeypatch, "genuine_rush")  # Gemini's own reading is recorded, never used
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)

    assert incident.classification == "card_testing" and incident.classifier_version == "scorecard-v1"
    # Gemini answered, but argued for a rush: the words are our template, credited as such.
    assert incident.decided_by == "scorecard" and incident.model_id == "template" and incident.ai_available
    assert abs(sum(incident.class_probs.values()) - 1) < 0.01
    assert incident.confidence == max(incident.class_probs.values()) and incident.class_margin > 0.3
    assert incident.scorecard["top_reasons"] and incident.scorecard["tiebreak"] is None
    # Gemini argued for a rush: its text is not used as the explanation of a card-testing verdict.
    assert "read the traffic differently" in incident.verdict["uncertainty_notes"]
    assert incident.verdict["recommended_actions"] == []  # no advice written for another class
    explained = (await db.execute(select(AuditLog.data).where(AuditLog.event == "ai.explained"))).scalar_one()
    assert explained["gemini_view"] == "genuine_rush" and explained["template_text"]
    report = await db.get(Report, incident.id)
    assert report is not None and "recommended only" not in (report.report_md or "")

    [event] = await verdict_events(db, incident.id)
    assert event["decided_by"] == "scorecard" and event["classifier"] == "scorecard-v1"
    assert event["probabilities"] == incident.class_probs and event["margin"] == incident.class_margin
    assert event["reasons"] and event["first"]
    ev = incident_event(incident)
    assert ev["decided_by"] == "scorecard" and ev["class_probs"] == incident.class_probs


async def test_a_disagreeing_second_opinion_leaves_it_unclear_with_soft_defences(db, monkeypatch):
    force_scorecard(monkeypatch, "unclear", 0.6, margin=0.05, top="card_testing")
    use_ai(monkeypatch, "genuine_rush")
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)

    assert incident.classification == "unclear" and incident.decided_by == DECIDED_BY_DISAGREE
    assert incident.ai_available and incident.scorecard["tiebreak"] == {
        "outcome": "disagree",
        "gemini": "genuine_rush",
        "model": "mock-model",
    }
    assert "read this as genuine rush" in incident.verdict["uncertainty_notes"]
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all" and await blocked(db) == [] and gs.held_skus == []  # R3: soft only
    rules = (await db.execute(select(Action.policy_rule).where(Action.incident_id == incident.id))).scalars()
    assert set(rules) <= {"R0_fraud_signal", "R3_soft_defence"}


async def test_gemini_agreeing_can_never_raise_the_confidence(db, monkeypatch):
    force_scorecard(monkeypatch, "unclear", 0.6, margin=0.05, top="card_testing")
    use_ai(monkeypatch, "card_testing", 0.99)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    assert incident.classification == "card_testing" and incident.decided_by == DECIDED_BY_AGREE
    assert incident.confidence == 0.6  # the scorecard's, not Gemini's 0.99
    # Below the act floor (0.70), but nobody contradicted the fraud signals: R3b, never weaker than the
    # same tie-break with Gemini down. Strong fingerprints only, the item held, no refunds.
    sig = incident.signature
    assert set(await blocked(db)) == set(sig["strong_fingerprints"]) and sig["strong_fingerprints"]
    rules = (await db.execute(select(Action.policy_rule).where(Action.incident_id == incident.id))).scalars()
    rules = set(rules)
    assert "R3b_unopposed_fraud_signal" in rules and not any(r.startswith(("R4", "R5")) for r in rules)
    refunds = await db.execute(select(Action).where(Action.type == ActionType.REFUND))
    assert refunds.scalars().all() == []
    [event] = await verdict_events(db, incident.id)
    assert event["tiebreak"]["outcome"] == "agree"


async def test_whether_gemini_explains_never_changes_the_defences(db, monkeypatch):
    """A confident-enough scorecard call below the act floor gets plain R3; Gemini failing to write the
    explanation afterwards must not turn that into R3b blocks on the next tick."""
    force_scorecard(monkeypatch, "card_testing", 0.6, margin=0.4)

    async def down(**kw):
        raise analyst.AIUnavailable("Gemini quota exhausted")

    monkeypatch.setattr("app.ai.analyst.generate_json", down)
    monkeypatch.setattr("app.ai.reporter.generate_json", down)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    service = IncidentService(db, None, REASSESS)
    incident = await service.tick(SHOP)
    assert incident.classification == "card_testing" and incident.decided_by == "scorecard"
    assert not incident.ai_available and incident.scorecard["tiebreak"] is None
    assert await blocked(db) == []
    await service.tick(SHOP)  # between assessments: the policy is re-applied (_enforce)
    assert await blocked(db) == []
    rules = set((await db.execute(select(Action.policy_rule))).scalars())
    assert not any(r.startswith("R3b") for r in rules)


async def test_baseline_learns_hour_of_day_rates_and_the_normal_unapproved_wallet_share(db):
    await seed_history(db, SHOP, days=14)
    now = datetime.now(UTC)
    for i in range(10):
        db.add(
            CheckoutAttempt(
                shop_id=SHOP,
                ts=now - timedelta(days=3, minutes=i),
                source=Source.SEED,
                paypal_live=False,
                sku="SKU-PILLAR-18",
                amount=18,
                method="wallet",
                fingerprint_hash=f"w{i}",
                guard_decision=Decision.ALLOW,
                outcome=Outcome.CREATED if i < 3 else Outcome.CAPTURED,
            )
        )
    await db.commit()
    base = await baseline_mod.load_baseline(db, SHOP, now)
    assert base.hourly_rate is not None and len(base.hourly_rate) == 24
    busiest, quietest = HOURLY_WEIGHTS.index(max(HOURLY_WEIGHTS)), HOURLY_WEIGHTS.index(min(HOURLY_WEIGHTS))
    assert base.hourly_rate[busiest] > base.hourly_rate[quietest]
    assert 0 < base.wallet_unapproved_rate < 0.5
    assert base.public()["wallet_unapproved_rate"] == round(base.wallet_unapproved_rate, 3)
