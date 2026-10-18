"""End-to-end on the real test database: offline-simulated traffic → guard → detector → (mocked) AI →
policy → actions → reports. The AI is mocked here so the test is deterministic; see eval/ for live AI."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ai import analyst
from app.ai.schemas import Evidence, Verdict
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    GuardState,
    MerchantSettings,
    Outcome,
    Report,
)
from app.guard.check import invalidate_cache
from app.incidents.service import IncidentService
from app.shop.service import place_order
from app.simulator.runner import to_request
from app.simulator.scenarios import card_testing, flash_sale
from app.simulator.seed import seed_history
from tests.conftest import SHOP


def fake_ai(classification: str, confidence: float):
    async def _generate_json(*, system, prompt, schema, temperature=0.1):
        if schema is Verdict:
            # Cite real numbers from the input, like a well-behaved model would.
            data = json.loads(prompt.split("\n\n", 1)[1])
            observed = data["windows"]["15m"]["attempts"]
            ev = [Evidence(feature="attempts", window_min=15, observed=observed, why="volume")]
            return Verdict(
                classification=classification,
                confidence=confidence,
                summary="mocked verdict",
                evidence=ev,
                recommended_actions=["SCA_ON"],
            ), "mock-model"
        raise analyst.AIUnavailable("reports use the template in tests")

    return _generate_json


@pytest.fixture
def mock_ai(monkeypatch):
    def use(classification: str, confidence: float):
        fn = fake_ai(classification, confidence)
        monkeypatch.setattr("app.ai.analyst.generate_json", fn)
        monkeypatch.setattr("app.ai.reporter.generate_json", fn)

    return use


async def run_scenario(db, gen, n):
    for a in list(gen("offline", count=n, seed=1)):
        await place_order(db, None, to_request(SHOP, "test", a, "offline"))


async def test_card_testing_is_contained_and_refunded(db, mock_ai):
    mock_ai("card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)

    svc = IncidentService(db, None, timedelta(minutes=2))
    incident = await svc.tick(SHOP)
    assert incident is not None and incident.classification == "card_testing"

    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all" and gs.held_skus == ["SKU-TEALIGHT-3"] and len(gs.blocked_fingerprints) == 4

    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    captured_before = [r for r in refunds if r.status == ActionStatus.EXECUTED]
    assert captured_before and all(r.params["simulated"] for r in captured_before)
    still_captured = (
        (
            await db.execute(
                select(CheckoutAttempt).where(
                    CheckoutAttempt.source == "simulator", CheckoutAttempt.outcome == Outcome.CAPTURED
                )
            )
        )
        .scalars()
        .all()
    )
    assert still_captured == []  # every attack capture refunded (all under the $10 / $100 caps)

    report = await db.get(Report, incident.id)
    assert "card-testing attack" in report.alert_text and "$" in report.alert_text

    # The bot keeps going: it is now blocked before reaching PayPal.
    invalidate_cache()
    more = list(card_testing("offline", count=5, seed=1))
    res = [await place_order(db, None, to_request(SHOP, "test", a, "offline")) for a in more]
    assert {r.outcome for r in res} == {Outcome.BLOCKED}


async def test_refund_caps_queue_for_owner_approval(db, mock_ai):
    mock_ai("card_testing", 0.95)
    ms = await db.get(MerchantSettings, SHOP)
    ms.max_auto_refund_per_incident = 6  # only two $3 refunds allowed automatically
    await db.commit()
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)

    await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert sum(r.status == ActionStatus.EXECUTED for r in refunds) == 2
    assert any(r.status == ActionStatus.AWAITING_APPROVAL for r in refunds)


async def test_flash_sale_gets_no_defences(db, mock_ai):
    mock_ai("genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    ms = await db.get(MerchantSettings, SHOP)
    now = datetime.now(UTC)
    ms.sale_windows = [
        {
            "start": (now - timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
            "note": "Autumn sale",
        }
    ]
    await db.commit()
    await run_scenario(db, flash_sale, 60)

    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    assert incident is not None and incident.classification == "genuine_rush"
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.blocked_fingerprints == [] and gs.held_skus == []
    acts = (await db.execute(select(Action.type))).scalars().all()
    assert ActionType.REFUND not in acts and ActionType.BLOCK_FP not in acts


async def test_ai_outage_falls_back_to_rules_only_defences(db, monkeypatch):
    """Used to assert "soft defences only, no blocks". That left an AI outage with no real defence: 3-D
    Secure is card-only and the web shop is wallet-only. Now (policy R0 + R3b) fraud-tier triggers get
    3-D Secure before the AI is asked, and with the AI down the strongest bot fingerprints are blocked and
    the targeted item held. Still never a refund, and the classification stays "unclear"."""
    calls: list[type] = []

    async def down(**kw):
        calls.append(kw["schema"])
        raise analyst.AIUnavailable("quota")

    monkeypatch.setattr("app.ai.analyst.generate_json", down)
    monkeypatch.setattr("app.ai.reporter.generate_json", down)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)

    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    assert incident.classification == "unclear" and not incident.ai_available
    assert calls == [Verdict]  # the reporter did not try Gemini again in the same tick
    assert (await db.get(Report, incident.id)).model_id == "template"
    assert incident.rules_hint == "fraud_signals"
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all" and gs.held_skus == ["SKU-TEALIGHT-3"]
    assert gs.blocked_fingerprints == incident.signature["strong_fingerprints"]
    assert len(gs.blocked_fingerprints) == 4  # the bot farm: a new card on every attempt
    rules = (await db.execute(select(Action.policy_rule).where(Action.incident_id == incident.id))).scalars()
    assert set(rules) == {"R0_fraud_signal", "R3b_ai_down_fraud_signal"}
    assert not (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    report = await db.get(Report, incident.id)
    assert "Nothing was blocked" not in report.alert_text and "blocked the devices" in report.alert_text


async def test_incident_deescalates_when_quiet(db, mock_ai):
    mock_ai("card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    svc = IncidentService(db, None, timedelta(minutes=2))
    incident = await svc.tick(SHOP)

    later = datetime.now(UTC) + timedelta(minutes=75)  # attack traffic has aged out of every window
    await svc.tick(SHOP, now=later)
    await db.refresh(incident)
    assert incident.status == "closed"
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.held_skus == [] and gs.blocked_fingerprints  # bots stay blocked


async def test_captures_after_the_verdict_are_refunded_on_the_next_tick(db, mock_ai):
    mock_ai("card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    svc = IncidentService(db, None, timedelta(minutes=2))
    await svc.tick(SHOP)

    # A brand-new bot device gets a capture through before it is known (e.g. while the AI was deciding).
    invalidate_cache()
    late = next(a for a in card_testing("offline", count=40, seed=99) if a.outcome == "approve")
    late.device_id = "bot-late"
    from app.db.models import GuardState as GS
    from app.guard.check import invalidate_cache as inv

    gs = await db.get(GS, SHOP, populate_existing=True)
    gs.mode, gs.held_skus = "normal", []  # simulate the window before defences applied
    await db.commit()
    inv()
    res = await place_order(db, None, to_request(SHOP, "test", late, "offline"))
    assert res.outcome == Outcome.CAPTURED

    await svc.tick(SHOP)  # no new AI call: enforcement only
    a = await db.get(CheckoutAttempt, res.attempt_id, populate_existing=True)
    assert a.outcome == Outcome.REFUNDED


async def test_new_shopper_on_held_item_is_challenged_then_allowed_with_3ds(db, mock_ai):
    from app.db.models import Decision
    from app.guard.check import issue_challenge_token
    from app.shop.service import CheckoutRequest, OfflinePlan, fingerprint

    mock_ai("card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    invalidate_cache()

    human = dict(
        shop_id=SHOP,
        sku="SKU-TEALIGHT-3",
        method="card",
        device_id="new-human-1",
        user_agent="Mozilla/5.0",
        ip=None,
        email="jo@gmail.com",
        offline=OfflinePlan("approve", is_bot=False),
    )
    first = await place_order(db, None, CheckoutRequest(**human))
    assert first.decision == Decision.CHALLENGE and first.outcome == Outcome.CHALLENGED

    token = issue_challenge_token(fingerprint("new-human-1", "Mozilla/5.0"))
    second = await place_order(db, None, CheckoutRequest(**human, challenge_token=token))
    assert second.decision == Decision.ALLOW_WITH_SCA and second.outcome == Outcome.CAPTURED
