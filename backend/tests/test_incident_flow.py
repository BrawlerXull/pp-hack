"""Incident flow on the real test database: what survives a failed Gemini call (the explanation's wording,
an agreed tie-break), no transaction or row lock held across Gemini/PayPal calls, and re-assessing only on
material change. The AI is always faked (monkeypatched generate_json); nothing here reaches Gemini or
PayPal."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.actions.executor import Executor
from app.ai import analyst, gemini
from app.ai.schemas import ReportDraft, Verdict
from app.api.dashboard import claim_regenerate_slot, close_incident, regenerate_report, reset_guard
from app.config import get_settings
from app.db import session as db_session
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
    Report,
)
from app.guard.check import invalidate_cache
from app.incidents import cadence
from app.incidents import service as service_mod
from app.incidents.decision import AGREE_CAP, DECIDED_BY_AGREE, DECIDED_BY_SCORECARD
from app.incidents.service import IncidentService, release_assessment_claims
from app.shop.service import fingerprint, place_order
from app.simulator.runner import to_request
from app.simulator.scenarios import BOT_UA, card_testing, flash_sale
from app.simulator.seed import seed_history
from tests.conftest import SHOP, blocked
from tests.test_pipeline import fake_ai, force_scorecard, run_scenario

REASSESS = timedelta(minutes=2)


def use_ai(monkeypatch, fn) -> None:
    monkeypatch.setattr("app.ai.analyst.generate_json", fn)
    monkeypatch.setattr("app.ai.reporter.generate_json", fn)


def counting(monkeypatch, classification: str | None, confidence: float = 0.95) -> list[type]:
    """Fake AI that records each call's schema; classification None means Gemini is down."""
    calls: list[type] = []
    good = fake_ai(classification or "unclear", confidence)

    async def fn(**kw):
        calls.append(kw["schema"])
        if classification is None:
            raise analyst.AIUnavailable("quota", "Gemini quota exhausted")
        return await good(**kw)

    use_ai(monkeypatch, fn)
    return calls


async def attack(db, monkeypatch) -> Incident:
    """Real card-testing traffic: the scorecard is sure (card_testing, refund floor), Gemini explains."""
    counting(monkeypatch, "card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    assert incident is not None and incident.classification == "card_testing"
    assert incident.decided_by == DECIDED_BY_SCORECARD and incident.confidence >= 0.85
    return incident


async def age_verdict(db, incident: Incident, minutes: float) -> None:
    """Move the last assessment into the past instead of moving the tick's clock (which would age the
    attack traffic out of the detector's windows and close the incident)."""
    await db.refresh(incident)
    shift = timedelta(minutes=minutes)
    incident.last_assessed_at -= shift
    incident.verdict_at -= shift
    if ai_at := (incident.assessment_context or {}).get("ai_at"):  # and the last Gemini call (backoff)
        shifted = (datetime.fromisoformat(ai_at) - shift).isoformat()
        incident.assessment_context = incident.assessment_context | {"ai_at": shifted}
    await db.commit()


async def new_bot_device(db) -> tuple[str, str]:
    """A device the verdict has never seen: a decline and a capture that lands before defences catch up
    (a second decline would trip the guard's per-device breaker first). Returns (fingerprint, attempt id)."""
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    gs.mode, gs.held_skus = "normal", []
    await db.commit()
    invalidate_cache()
    pool = list(card_testing("offline", count=40, seed=99))
    batch = [a for a in pool if a.outcome == "decline"][:1] + [
        next(a for a in pool if a.outcome == "approve")
    ]
    res = None
    for a in batch:
        a.device_id, a.user_agent = "bot-new", BOT_UA
        res = await place_order(db, None, to_request(SHOP, "test", a, "offline"))
    assert res is not None and res.outcome == Outcome.CAPTURED
    return fingerprint("bot-new", BOT_UA), res.attempt_id


async def events(db, incident_id: str) -> list[str]:
    q = select(AuditLog.event).where(AuditLog.incident_id == incident_id).order_by(AuditLog.id)
    return list((await db.execute(q)).scalars())


# ---- item 1 (P9): the scorecard decides; what is "kept" through a failed Gemini call ------------------
# Before P9 these tests kept the last AI *verdict* through a failed re-check. The scorecard now re-decides
# on every assessment, so a Gemini failure can only cost the explanation's wording (kept for the same class)
# or an agreed tie-break (kept, with Guard 1 and Guard 2). Fake confidences like 0.95 no longer appear on
# the incident: Gemini cannot set a confidence.


async def tiebreak_attack(db, monkeypatch, top="card_testing", confidence=0.9) -> Incident:
    """An incident the scorecard was unsure about (forced) and Gemini agreed with: AGREE_CAP applies."""
    force_scorecard(monkeypatch, "unclear", confidence, margin=0.1, top=top)
    counting(monkeypatch, top, 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    assert incident.classification == top and incident.decided_by == DECIDED_BY_AGREE
    assert incident.confidence == min(confidence, AGREE_CAP)
    return incident


async def test_failed_explanation_keeps_the_wording_and_the_scorecard_still_blocks_and_refunds(
    db, monkeypatch
):
    incident = await attack(db, monkeypatch)
    confidence = incident.confidence
    fp, late_id = await new_bot_device(db)
    await age_verdict(db, incident, 11)  # verdict older than 10 min: due for a re-check
    calls = counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert calls == [Verdict]  # the explanation was attempted (and failed); the reporter didn't retry Gemini
    assert incident.classification == "card_testing" and incident.confidence >= 0.85
    assert incident.confidence != confidence  # re-decided from the new numbers, not kept
    assert incident.model_id == "mock-model" and incident.ai_available  # the AI's wording stands
    assert incident.ai_refresh_failed_at is None and incident.ai_refresh_failures == 1
    assert incident.ai_fallback_reason == "Gemini quota exhausted"
    assert "ai.explanation_kept" in await events(db, incident.id)

    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert fp in await blocked(db) and gs.mode == "sca_all" and gs.held_skus == ["SKU-TEALIGHT-3"]
    late = await db.get(CheckoutAttempt, late_id, populate_existing=True)
    assert late.outcome == Outcome.REFUNDED


async def test_failed_explanation_keeps_genuine_rush_and_the_checkout_normal(db, monkeypatch):
    counting(monkeypatch, "genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    ms = await db.get(MerchantSettings, SHOP)
    now = datetime.now(UTC)
    ms.sale_windows = [
        {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
    ]
    await db.commit()
    await run_scenario(db, flash_sale, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    await age_verdict(db, incident, 11)
    counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert "ai.explanation_kept" in await events(db, incident.id)
    assert incident.classification == "genuine_rush" and incident.confidence >= 0.7
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and await blocked(db) == [] and gs.held_skus == []


async def test_failed_recheck_keeps_an_agreed_tiebreak(db, monkeypatch):
    incident = await tiebreak_attack(db, monkeypatch)
    fp, _ = await new_bot_device(db)
    await age_verdict(db, incident, 11)
    calls = counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert calls == [Verdict] and "ai.refresh_failed" in await events(db, incident.id)
    assert incident.classification == "card_testing" and incident.confidence == AGREE_CAP
    assert incident.decided_by == DECIDED_BY_AGREE and incident.ai_available
    assert incident.ai_refresh_failed_at is not None and incident.ai_refresh_failures == 1
    assert fp in await blocked(db)  # R4 still applies to the kept verdict


async def test_kept_harmless_verdict_yields_to_a_new_fraud_trigger(db, monkeypatch):
    # Guard 1: an agreed genuine_rush was decided before the fraud triggers fired (simulated by its context).
    incident = await tiebreak_attack(db, monkeypatch, top="genuine_rush", confidence=0.6)
    await db.refresh(incident)
    assert set(incident.triggers) & cadence.FRAUD_TRIGGERS
    incident.assessment_context = cadence.context(
        triggers=[], sale_window=False, attempts_15m=0, verdict_triggers=[]
    )
    incident.last_assessed_at -= timedelta(minutes=1)
    await db.commit()
    counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert "ai.kept_verdict_dropped" in await events(db, incident.id)
    assert incident.classification == "unclear" and incident.decided_by == DECIDED_BY_SCORECARD
    assert incident.model_id == cadence.TEMPLATE and not incident.ai_available
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all"


async def test_kept_attack_verdict_yields_when_paypal_starts_failing(db, monkeypatch):
    # Guard 1, reverse: shoppers retrying through a PayPal outage look like retrying bots (3+ attempts).
    incident = await tiebreak_attack(db, monkeypatch)
    fp, late_id = await new_bot_device(db)
    await db.refresh(incident)
    incident.triggers = sorted({*incident.triggers, "processor_errors"})  # fired after the verdict
    incident.last_assessed_at -= timedelta(minutes=1)
    await db.commit()
    counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert "ai.kept_verdict_dropped" in await events(db, incident.id)
    assert incident.model_id == cadence.TEMPLATE and incident.classification != "card_testing"
    assert fp not in await blocked(db)
    late = await db.get(CheckoutAttempt, late_id, populate_existing=True)
    assert late.outcome == Outcome.CAPTURED


async def test_an_agreed_tiebreak_blocks_but_never_refunds_even_when_kept_stale(db, monkeypatch):
    # AGREE_CAP keeps an AI-confirmed verdict under the refund floor; Guard 2 still marks it stale.
    incident = await tiebreak_attack(db, monkeypatch, confidence=0.95)
    fp, late_id = await new_bot_device(db)
    await age_verdict(db, incident, 11)
    incident.ai_refresh_failures, incident.ai_refresh_failed_at = 2, datetime.now(UTC)
    await db.commit()
    counting(monkeypatch, None)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert incident.ai_refresh_failures == 3 and cadence.verdict_stale(incident, datetime.now(UTC))
    assert fp in await blocked(db)
    late = await db.get(CheckoutAttempt, late_id, populate_existing=True)
    assert late.outcome == Outcome.CAPTURED
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert refunds == []


async def test_ai_switched_off_mid_incident_drops_the_kept_tiebreak(db, monkeypatch):
    # AI_ENABLED=false is not a failed re-check: the AI-confirmed class must not keep driving blocks.
    incident = await tiebreak_attack(db, monkeypatch)
    await age_verdict(db, incident, 11)
    off = get_settings().model_copy(update={"ai_enabled": False})
    monkeypatch.setattr(analyst, "get_settings", lambda: off)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert "ai.refresh_failed" not in await events(db, incident.id)
    assert incident.classification == "unclear" and incident.model_id == cadence.TEMPLATE
    assert incident.ai_fallback_reason == gemini.AI_DISABLED and not incident.ai_available
    assert incident.scorecard["tiebreak"]["outcome"] == "unavailable"


async def test_ai_switched_off_the_scorecard_still_decides_and_refunds(db, monkeypatch):
    off = get_settings().model_copy(update={"ai_enabled": False})
    monkeypatch.setattr(analyst, "get_settings", lambda: off)
    calls = counting(monkeypatch, "card_testing")
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    assert calls == []  # switched off: no analyst call, and the reporter skips Gemini in the same tick
    assert incident.classification == "card_testing" and incident.model_id == cadence.TEMPLATE
    assert incident.ai_fallback_reason == gemini.AI_DISABLED and incident.ai_refresh_failures == 0
    refunds = (await db.execute(select(Action).where(Action.type == ActionType.REFUND))).scalars().all()
    assert refunds and all(r.status == ActionStatus.EXECUTED for r in refunds)


async def test_successful_recheck_clears_the_failure_state(db, monkeypatch):
    incident = await attack(db, monkeypatch)
    await age_verdict(db, incident, 11)
    incident.ai_refresh_failures, incident.ai_refresh_failed_at = 2, datetime.now(UTC)
    await db.commit()
    counting(monkeypatch, "card_testing", 0.97)

    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert incident.confidence != 0.97  # Gemini cannot set the confidence: it is the scorecard's
    assert incident.ai_refresh_failures == 0 and incident.ai_refresh_failed_at is None


# ---- item 11: no transaction or lock across Gemini / PayPal -----------------------------------------


async def nowait_locks_free(s) -> None:
    """Fails (instead of hanging) if anyone holds the guard_state or incident row locks."""
    await s.execute(text("SELECT 1 FROM guard_state WHERE shop_id = :s FOR UPDATE NOWAIT"), {"s": SHOP})
    await s.execute(text("SELECT 1 FROM incidents FOR UPDATE NOWAIT"))
    await s.rollback()


async def test_new_incident_is_visible_and_unlocked_while_the_analyst_waits(db, monkeypatch):
    seen: list[tuple[str | None, str | None]] = []
    good = fake_ai("card_testing", 0.95)

    async def slow(**kw):
        if kw["schema"] is Verdict:
            async with db_session.get_sessionmaker()() as other:
                rows = (await other.execute(select(Incident))).scalars().all()
                seen.extend((i.status, i.classification) for i in rows)
                await nowait_locks_free(other)
        return await good(**kw)

    use_ai(monkeypatch, slow)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    # P9: the scorecard's verdict (and its defences) are committed before Gemini is asked to explain it.
    # It used to be ("open", None): the dashboard showed "investigating" until the AI answered.
    assert seen == [("open", "card_testing")]
    assert incident.classification == "card_testing" and incident.assessing_since is None


async def test_owner_reset_does_not_block_while_the_reporter_waits(db, monkeypatch):
    resets: list[str] = []
    good = fake_ai("card_testing", 0.95)

    async def reporter_waits(**kw):
        if kw["schema"] is ReportDraft:
            async with db_session.get_sessionmaker()() as other:
                await other.execute(text("SET lock_timeout = '2s'"))
                await reset_guard(shop_id=SHOP, session=other)  # raises LockNotAvailable if blocked
                resets.append("ok")
            raise analyst.AIUnavailable("template is fine")
        return await good(**kw)

    use_ai(monkeypatch, reporter_waits)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)  # first verdict: report written
    assert resets == ["ok"]

    # Owner close: the restored checkout is committed before the report is drafted.
    await close_incident(incident.id, shop_id=SHOP, session=db, paypal=None, settings=get_settings())
    assert resets == ["ok", "ok"]
    await db.refresh(incident)
    assert incident.status == IncidentStatus.CLOSED


async def test_refunds_run_after_the_guard_state_is_committed(db, monkeypatch):
    checked: list[bool] = []
    real = Executor.execute_refund

    async def spy(self, incident, attempt, params, rule, approved_by, pending=None):
        async with db_session.get_sessionmaker()() as other:
            await other.execute(
                text("SELECT 1 FROM guard_state WHERE shop_id = :s FOR UPDATE NOWAIT"), {"s": SHOP}
            )
            mode = (await other.execute(text("SELECT mode FROM guard_state"))).scalar_one()
            checked.append(mode == "sca_all")  # defences already live for other sessions
            await other.rollback()
        return await real(self, incident, attempt, params, rule, approved_by, pending)

    monkeypatch.setattr(Executor, "execute_refund", spy)
    await attack(db, monkeypatch)
    assert checked and all(checked)


async def refunds_with_owner_acting_after_the_first(db, monkeypatch, owner_sql: str) -> list[str]:
    """Runs a first-verdict attack whose first auto refund triggers the owner's action from another
    session; returns the incident.* events the worker published."""
    real = Executor.execute_refund
    refunded: list[str] = []

    owner: list[asyncio.Task] = []

    async def owner_action() -> None:
        async with db_session.get_sessionmaker()() as other:
            await other.execute(text("SET lock_timeout = '5s'"))
            await other.execute(text(owner_sql))
            await other.commit()

    async def owner_acts_after_first(self, incident, attempt, params, rule, approved_by, pending=None):
        if not refunded:
            # Concurrently, as from the dashboard: a close waits for this refund's commit, then wins.
            owner.append(asyncio.create_task(owner_action()))
            await asyncio.sleep(0.2)
        out = await real(self, incident, attempt, params, rule, approved_by, pending)
        refunded.append(attempt.id)
        return out

    published: list[str] = []
    real_publish = service_mod.publish

    async def spy_publish(session, kind, data):
        published.append(kind)
        await real_publish(session, kind, data)

    monkeypatch.setattr(Executor, "execute_refund", owner_acts_after_first)
    monkeypatch.setattr("app.incidents.service.publish", spy_publish)
    counting(monkeypatch, "card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    await owner[0]
    candidates = await Executor(db, None)._matches(incident)
    assert len(refunded) == 1 and candidates, "the scenario needs more than one matched capture"
    assert "refunds.stopped" in await events(db, incident.id)
    return published


async def test_owner_close_mid_refunds_stops_the_remaining_refunds(db, monkeypatch):
    published = await refunds_with_owner_acting_after_the_first(
        db, monkeypatch, "UPDATE incidents SET status = 'closed', closed_at = now()"
    )
    # No "incident.updated" with status open after the close, and no in-progress report replacing the close's.
    assert published == ["incident.opened"]


async def test_kill_switch_mid_refunds_stops_the_remaining_refunds(db, monkeypatch):
    published = await refunds_with_owner_acting_after_the_first(
        db, monkeypatch, "UPDATE merchant_settings SET kill_switch = true"
    )
    assert "incident.updated" in published


async def test_autopilot_off_mid_refunds_queues_the_rest_for_the_owner(db, monkeypatch):
    real = Executor.execute_refund
    refunded: list[str] = []

    async def owner_turns_autopilot_off(self, incident, attempt, params, rule, approved_by, pending=None):
        out = await real(self, incident, attempt, params, rule, approved_by, pending)
        refunded.append(attempt.id)
        async with db_session.get_sessionmaker()() as other:
            await other.execute(text("UPDATE merchant_settings SET autopilot = false"))
            await other.commit()
        return out

    monkeypatch.setattr(Executor, "execute_refund", owner_turns_autopilot_off)
    await attack(db, monkeypatch)
    pending = (
        (await db.execute(select(Action).where(Action.status == ActionStatus.AWAITING_APPROVAL)))
        .scalars()
        .all()
    )
    assert len(refunded) == 1 and pending
    assert {a.policy_rule for a in pending} == {"R5_refund_matched"}


async def test_report_drafted_while_the_owner_closes_does_not_replace_the_final_one(db, monkeypatch):
    good = fake_ai("card_testing", 0.95)
    closed: list[str] = []

    async def owner_closes_while_drafting(**kw):
        if kw["schema"] is ReportDraft and not closed:
            closed.append("x")
            async with db_session.get_sessionmaker()() as other:
                inc = (await other.execute(select(Incident))).scalar_one()
                await other.execute(text("SET lock_timeout = '2s'"))
                await close_incident(
                    inc.id, shop_id=SHOP, session=other, paypal=None, settings=get_settings()
                )
        return await good(**kw)

    use_ai(monkeypatch, owner_closes_while_drafting)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    report = await db.get(Report, incident.id, populate_existing=True)
    assert closed and "still being handled" not in (report.report_md or "")
    assert "report.discarded" in await events(db, incident.id)


async def test_failure_after_the_analyst_releases_the_claim(db, monkeypatch):
    async def lock_timeout(self, incident, planned):
        raise RuntimeError("lock timeout on guard_state")

    monkeypatch.setattr(Executor, "_apply_guard", lock_timeout)
    counting(monkeypatch, "card_testing")
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    with pytest.raises(RuntimeError):
        await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.rollback()
    incident = (await db.execute(select(Incident))).scalar_one()
    assert incident.assessing_since is None and incident.classification is None

    monkeypatch.undo()
    counting(monkeypatch, "card_testing")
    await IncidentService(db, None, REASSESS).tick(SHOP)  # assessed at once, not after the lease
    await db.refresh(incident)
    assert incident.classification == "card_testing"


async def test_incident_closed_during_the_call_discards_the_explanation(db, monkeypatch):
    """P9: the scorecard's decision and defences land before the Gemini call, so a close during the call
    discards only the AI's words. (It used to discard the whole verdict, with nothing but R0 applied.)"""
    good = fake_ai("card_testing", 0.95)

    async def owner_closes_meanwhile(**kw):
        if kw["schema"] is Verdict:
            async with db_session.get_sessionmaker()() as other:
                await other.execute(text("UPDATE incidents SET status = 'closed', closed_at = now()"))
                await other.commit()
        return await good(**kw)

    use_ai(monkeypatch, owner_closes_meanwhile)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert incident.status == IncidentStatus.CLOSED and incident.classification == "card_testing"
    assert incident.model_id == cadence.TEMPLATE  # the AI's text never landed
    assert "ai.explanation_discarded" in await events(db, incident.id)
    acts = (await db.execute(select(Action.policy_rule).where(Action.incident_id == incident.id))).scalars()
    assert {"R0_fraud_signal", "R4_attack"} <= set(acts)


async def test_a_tiebreak_closed_during_the_call_is_discarded(db, monkeypatch):
    good = fake_ai("card_testing", 0.95)
    force_scorecard(monkeypatch, "unclear", 0.6, margin=0.05, top="card_testing")

    async def owner_closes_meanwhile(**kw):
        async with db_session.get_sessionmaker()() as other:
            await other.execute(text("UPDATE incidents SET status = 'closed', closed_at = now()"))
            await other.commit()
        return await good(**kw)

    use_ai(monkeypatch, owner_closes_meanwhile)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert incident.status == IncidentStatus.CLOSED and incident.classification is None
    assert "ai.verdict_discarded" in await events(db, incident.id)
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    # The discarded verdict blocked and held nothing. 3-D Secure is on: policy R0 commits it for the fraud
    # triggers before the analyst is asked. (This test closes the row with raw SQL; the owner's real close
    # goes through IncidentService.close, which restores the pre-incident mode.)
    assert gs.mode == "sca_all" and await blocked(db) == [] and gs.held_skus == []
    acts = (await db.execute(select(Action.policy_rule).where(Action.incident_id == incident.id))).scalars()
    assert list(acts) == ["R0_fraud_signal"]


async def test_assessment_in_flight_elsewhere_is_not_repeated(db, monkeypatch):
    incident = await attack(db, monkeypatch)
    await age_verdict(db, incident, 11)
    incident.assessing_since = datetime.now(UTC)  # another assessor claimed it
    await db.commit()
    calls = counting(monkeypatch, "card_testing")

    await IncidentService(db, None, REASSESS).tick(SHOP)
    assert calls == []  # the scorecard re-decided; the explanation is left to the claim holder
    incident.assessing_since = datetime.now(UTC) - timedelta(minutes=5)  # a crashed assessor's claim
    await db.commit()
    await age_verdict(db, incident, 11)
    await IncidentService(db, None, REASSESS).tick(SHOP)
    assert calls == [Verdict]


async def test_crash_during_the_first_explanation_keeps_the_decision_and_releases_the_claim(db, monkeypatch):
    """P9: the scorecard's first decision is committed before Gemini is called, so a crash in the call
    loses only the AI's words. (Before, it lost the whole first verdict and the next tick re-asked.)"""

    async def crash(**kw):
        raise RuntimeError("worker died mid-call")

    use_ai(monkeypatch, crash)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    with pytest.raises(RuntimeError):
        await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.rollback()
    incident = (await db.execute(select(Incident))).scalar_one()
    assert incident.classification == "card_testing" and incident.assessing_since is None  # released
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all" and await blocked(db)

    incident.assessing_since = datetime.now(UTC)  # as if the process died without releasing it
    await db.commit()
    await release_assessment_claims(db)  # worker start
    await db.refresh(incident)
    assert incident.assessing_since is None

    # The owner was never alerted about defences that are live: the next tick sends the overdue alert.
    assert await db.get(Report, incident.id) is None and incident.assessment_context["report_due"]
    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    report = await db.get(Report, incident.id, populate_existing=True)
    assert report is not None and report.alert_text
    assert "report_due" not in incident.assessment_context
    sent = (await db.execute(select(AuditLog).where(AuditLog.event == "alert.sent"))).scalars().all()
    assert len(sent) == 1
    await IncidentService(db, None, REASSESS).tick(SHOP)  # sent once, not on every tick
    sent = (await db.execute(select(AuditLog).where(AuditLog.event == "alert.sent"))).scalars().all()
    assert len(sent) == 1


async def test_crash_before_a_first_tiebreak_is_assessed_on_the_next_tick(db, monkeypatch):
    async def crash(**kw):
        raise RuntimeError("worker died mid-call")

    force_scorecard(monkeypatch, "unclear", 0.6, margin=0.05, top="card_testing")
    use_ai(monkeypatch, crash)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    with pytest.raises(RuntimeError):
        await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.rollback()
    incident = (await db.execute(select(Incident))).scalar_one()
    assert incident.last_assessed_at is None and incident.assessing_since is None  # claim released

    calls = counting(monkeypatch, "card_testing")
    await IncidentService(db, None, REASSESS).tick(SHOP)
    await db.refresh(incident)
    assert calls[0] is Verdict and incident.classification == "card_testing"
    assert incident.decided_by == DECIDED_BY_AGREE


# ---- item 19: re-ask only on material change; usage logging; regenerate limit ----------------------


async def test_unchanged_attack_is_enforced_without_asking_the_ai_again(db, monkeypatch):
    incident = await attack(db, monkeypatch)
    calls = counting(monkeypatch, "card_testing")
    await age_verdict(db, incident, 3)  # past the old 120 s cadence, nothing material changed
    await IncidentService(db, None, REASSESS).tick(SHOP)
    assert calls == []
    await age_verdict(db, incident, 8)  # now 11 min old: re-asked
    await IncidentService(db, None, REASSESS).tick(SHOP)
    assert calls == [Verdict]
    assert (await db.execute(select(AuditLog.data).where(AuditLog.event == "verdict"))).scalars().all()[-1][
        "reask"
    ] == "verdict_aged"


async def test_gemini_token_usage_is_audited_per_call(db, monkeypatch):
    good = fake_ai("card_testing", 0.95)

    async def with_usage(**kw):
        meta = SimpleNamespace(
            prompt_token_count=1200,
            candidates_token_count=300,
            thoughts_token_count=40,
            total_token_count=1540,
        )
        gemini._record_usage("m1", SimpleNamespace(usage_metadata=meta, model_version="m1-001"))
        return await good(**kw)

    use_ai(monkeypatch, with_usage)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    rows = (
        (
            await db.execute(
                select(AuditLog.data).where(AuditLog.event == "ai.usage", AuditLog.incident_id == incident.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r["purpose"] for r in rows] == ["analyst", "report"]  # one record per request, per purpose
    assert rows[0]["calls"] == [
        {
            "model": "m1-001",
            "ok": True,
            "prompt_tokens": 1200,
            "output_tokens": 300,
            "thinking_tokens": 40,
            "total_tokens": 1540,
        }
    ]


async def test_report_regenerate_is_rate_limited(db, monkeypatch):
    incident = await attack(db, monkeypatch)
    args = dict(shop_id=SHOP, session=db, paypal=None, settings=get_settings())
    with pytest.raises(HTTPException) as exc:
        await regenerate_report(incident.id, **args)
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) > 0

    await db.execute(text("UPDATE reports SET generated_at = now() - interval '2 minutes'"))
    await db.commit()
    out = await regenerate_report(incident.id, **args)
    assert out["model_id"] == "template"


async def test_regenerate_slot_is_claimed_atomically(db, monkeypatch):
    # Two concurrent regenerates: the second one's UPDATE waits on the first's row lock, then fails the
    # limit, so only one Gemini call is made.
    incident = await attack(db, monkeypatch)
    await db.execute(text("UPDATE reports SET generated_at = now() - interval '2 minutes'"))
    await db.commit()
    assert await claim_regenerate_slot(db, incident.id)  # not committed yet: the row stays locked
    async with db_session.get_sessionmaker()() as other:
        await other.execute(text("SET lock_timeout = '5s'"))
        pending = asyncio.ensure_future(claim_regenerate_slot(other, incident.id))
        await asyncio.sleep(0.2)
        assert not pending.done()
        await db.commit()
        assert not await pending
