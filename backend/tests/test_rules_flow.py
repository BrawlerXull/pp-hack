"""Rules-first detection and policy on the real test database: R0 before the analyst, RELAX, the baseline
that keeps past attacks out of "normal", and the guard's per-device velocity breaker. The AI is always
faked; nothing here reaches Gemini or PayPal."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.actions.executor import Executor
from app.ai import analyst, reporter
from app.ai.schemas import Verdict
from app.db import session as db_session
from app.db.models import (
    Action,
    ActionType,
    CheckoutAttempt,
    Decision,
    GuardState,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Outcome,
    Source,
)
from app.detection import baseline as baseline_mod
from app.detection.features import compute_all
from app.detection.triggers import evaluate
from app.guard import devices
from app.guard.check import device_over_limit, invalidate_cache, issue_challenge_token
from app.incidents.service import IncidentService
from app.policy.engine import PlannedAction
from app.shop.catalog import CHEAPEST_SKU
from app.shop.service import CheckoutRequest, OfflinePlan, fingerprint, place_order
from app.simulator.scenarios import card_testing
from app.simulator.seed import seed_history
from tests.conftest import SHOP, blocked
from tests.test_pipeline import fake_ai, run_scenario

REASSESS = timedelta(minutes=2)


async def rules(db, incident_id: str) -> list[str]:
    q = select(Action.policy_rule).where(Action.incident_id == incident_id).order_by(Action.created_at)
    return list((await db.execute(q)).scalars())


# ---- R0 and RELAX -------------------------------------------------------------------------------------


def observing(monkeypatch, classification: str, confidence: float) -> list[str]:
    """Fake AI that records the guard mode other sessions (the checkout) see while it is 'thinking'."""
    seen: list[str] = []
    good = fake_ai(classification, confidence)

    async def fn(**kw):
        if kw["schema"] is Verdict:
            async with db_session.get_sessionmaker()() as other:
                gs = await other.get(GuardState, SHOP)
                seen.append(gs.mode)
        return await good(**kw)

    monkeypatch.setattr("app.ai.analyst.generate_json", fn)
    monkeypatch.setattr("app.ai.reporter.generate_json", fn)
    return seen


async def test_r0_turns_on_3ds_before_the_analyst_and_relax_undoes_it_once(db, monkeypatch):
    seen = observing(monkeypatch, "genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    svc = IncidentService(db, None, REASSESS)

    incident = await svc.tick(SHOP)
    assert seen == ["sca_all"]  # committed before the call: checkouts were protected while Gemini thought
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.held_skus == []  # the confident harmless verdict relaxed it
    assert await rules(db, incident.id) == ["R0_fraud_signal", "R2_relax_genuine_rush"]
    relax = (await db.execute(select(Action).where(Action.type == ActionType.RELAX))).scalar_one()
    assert relax.params == {"restored_mode": "normal", "released_skus": []}

    await svc.tick(SHOP)  # enforcement only (no new AI call): RELAX is idempotent
    assert await rules(db, incident.id) == ["R0_fraud_signal", "R2_relax_genuine_rush"]


def owner_closes_during(monkeypatch, method: str) -> None:
    """Close the open incident from another session (the owner's real close path) just before `method`."""
    real = getattr(IncidentService, method)

    async def wrapped(self, incident, *args, **kwargs):
        async with db_session.get_sessionmaker()() as other:
            inc = await other.get(Incident, incident.id, with_for_update=True)
            if inc.status == IncidentStatus.OPEN:
                await IncidentService(other, None, REASSESS).close(inc, "owner_closed")
        return await real(self, incident, *args, **kwargs)

    monkeypatch.setattr(IncidentService, method, wrapped)


async def test_an_owner_close_between_r0_and_the_claim_leaves_the_checkout_restored(db, monkeypatch):
    observing(monkeypatch, "unclear", 0.5)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    svc = IncidentService(db, None, REASSESS)
    incident = await svc.tick(SHOP)  # R0, then R3 (3-D Secure stays on)

    seen = observing(monkeypatch, "unclear", 0.5)
    incident.last_assessed_at = datetime.now(UTC) - timedelta(minutes=5)
    incident.assessment_context = {**incident.assessment_context, "triggers": []}  # force a re-ask
    await db.commit()
    owner_closes_during(monkeypatch, "_claim")
    await svc.tick(SHOP)

    assert seen == []  # the analyst was never asked about a closed incident
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.held_skus == [] and await blocked(db) == []
    assert (await rules(db, incident.id))[-1] == "owner_closed"  # nothing re-armed after the close


async def test_an_owner_close_before_r0_stops_r0(db, monkeypatch):
    observing(monkeypatch, "card_testing", 0.95)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    owner_closes_during(monkeypatch, "_rules_first")

    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.held_skus == []
    assert await rules(db, incident.id) == ["owner_closed"]


async def test_the_owner_is_told_when_r0_adds_friction_on_a_reassessment(db, monkeypatch):
    """R0 turns 3-D Secure on again; the analyst then says 'unclear', whose R3 plan adds nothing new. The
    owner must still hear about the friction R0 added."""
    observing(monkeypatch, "genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    svc = IncidentService(db, None, REASSESS)
    incident = await svc.tick(SHOP)  # R0, then RELAX

    observing(monkeypatch, "unclear", 0.5)
    reports: list[str] = []
    real_report = IncidentService.write_report

    async def counting(self, inc, skip_ai=False):
        reports.append(inc.id)
        return await real_report(self, inc, skip_ai=skip_ai)

    monkeypatch.setattr(IncidentService, "write_report", counting)
    incident.last_assessed_at = datetime.now(UTC) - timedelta(minutes=5)
    incident.assessment_context = {**incident.assessment_context, "triggers": [], "verdict_triggers": []}
    await db.commit()

    await svc.tick(SHOP)  # new_trigger re-ask; R0 re-arms 3-D Secure before the call
    assert await rules(db, incident.id) == ["R0_fraud_signal", "R2_relax_genuine_rush", "R0_fraud_signal"]
    assert reports == [incident.id]


async def test_r0_is_skipped_during_an_owner_announced_sale(db, monkeypatch):
    seen = observing(monkeypatch, "genuine_rush", 0.9)
    await seed_history(db, SHOP, days=7)
    ms = await db.get(MerchantSettings, SHOP)
    now = datetime.now(UTC)
    ms.sale_windows = [
        {"start": (now - timedelta(hours=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
    ]
    await db.commit()
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, REASSESS).tick(SHOP)
    assert seen == ["normal"]
    assert await rules(db, incident.id) == []


async def open_incident_with_defences(db, set_by: str | None = None) -> tuple[Incident, GuardState]:
    incident = Incident(shop_id=SHOP, opened_at=datetime.now(UTC), triggers=["decline_spike"])
    db.add(incident)
    await db.flush()
    incident.previous_guard_state = {"mode": "normal", "held_skus": []}
    gs = await db.get(GuardState, SHOP)
    gs.mode, gs.held_skus = "sca_all", [CHEAPEST_SKU]
    await devices.block(db, SHOP, ["old", "bot"], incident.id, {}, datetime.now(UTC))
    gs.set_by_incident = set_by or incident.id
    await db.commit()
    return incident, gs


async def test_relax_restores_mode_and_holds_but_keeps_blocks(db):
    incident, gs = await open_incident_with_defences(db)
    relax = PlannedAction(ActionType.RELAX, "R2_relax_genuine_rush")
    done = await Executor(db, None).apply(incident, [relax])
    assert [a.params for a in done] == [{"restored_mode": "normal", "released_skus": [CHEAPEST_SKU]}]
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "normal" and gs.held_skus == [] and await blocked(db) == ["bot", "old"]
    assert await Executor(db, None).apply(incident, [relax]) == []  # nothing left to restore


async def test_relax_never_undoes_another_incidents_defences(db):
    incident, _ = await open_incident_with_defences(db, set_by="someone-else")
    done = await Executor(db, None).apply(
        incident, [PlannedAction(ActionType.RELAX, "R2_relax_genuine_rush")]
    )
    assert done == []
    gs = await db.get(GuardState, SHOP, populate_existing=True)
    assert gs.mode == "sca_all" and gs.held_skus == [CHEAPEST_SKU]


# ---- baseline (report item 23) -----------------------------------------------------------------------


def attack_row(ts: datetime, i: int, *, outcome: str | None = None, fp: str | None = None) -> CheckoutAttempt:
    declined = i % 4 != 0
    return CheckoutAttempt(
        shop_id=SHOP,
        ts=ts,
        source=Source.SIMULATOR,
        paypal_live=False,
        sku=CHEAPEST_SKU,
        amount=Decimal("3.00"),
        method="card",
        fingerprint_hash=fp or f"bot{i % 4}",
        card_bin6="400000",
        card_last4=f"{i:04d}",
        guard_decision=Decision.ALLOW,
        outcome=outcome or (Outcome.DECLINED if declined else Outcome.CAPTURED),
        decline_code="SUSPECTED_FRAUD" if declined else None,
        decline_category="fraud" if declined else None,
    )


async def detect(db, now: datetime) -> list[str]:
    base = await baseline_mod.load_baseline(db, SHOP, now)
    rows = await baseline_mod.recent_rows(db, SHOP, now - timedelta(minutes=60))
    return evaluate(compute_all(rows, now, base), base)


async def test_a_second_attack_three_hours_after_the_first_still_trips_decline_spike(db):
    await seed_history(db, SHOP, days=7)
    now = datetime.now(UTC)
    first = now - timedelta(hours=3)
    db.add_all(attack_row(first + timedelta(seconds=10 * i), i) for i in range(60))
    db.add_all(attack_row(now - timedelta(seconds=10 * i), i) for i in range(60))
    await db.commit()

    # Control: without the incident record, the first run's declines become "normal" and hide the second.
    assert (await baseline_mod.load_baseline(db, SHOP, now)).decline_rate > 0.3
    assert "decline_spike" not in await detect(db, now)

    db.add(
        Incident(
            shop_id=SHOP,
            opened_at=first + timedelta(minutes=2),  # the first attempts land before it opens (lead 15 min)
            closed_at=first + timedelta(minutes=30),
            status=IncidentStatus.CLOSED,
        )
    )
    await db.commit()
    assert (await baseline_mod.load_baseline(db, SHOP, now)).decline_rate < 0.1
    assert "decline_spike" in await detect(db, now)


async def test_baseline_ignores_guard_stopped_attempts_refunds_and_blocked_devices(db):
    await seed_history(db, SHOP, days=7)
    now = datetime.now(UTC)
    before = await baseline_mod.load_baseline(db, SHOP, now)

    t = now - timedelta(hours=5)
    db.add_all(attack_row(t + timedelta(seconds=i), i, outcome=Outcome.BLOCKED) for i in range(40))
    db.add(attack_row(t, 1, outcome=Outcome.REFUNDED, fp="refunded-bot"))
    db.add(attack_row(t, 2, outcome=Outcome.CAPTURED, fp="blocked-now"))
    db.add(attack_row(t, 3, outcome=Outcome.CAPTURED, fp="real-shopper"))
    await devices.block(db, SHOP, ["blocked-now"], None, {}, now)
    await db.commit()

    after = await baseline_mod.load_baseline(db, SHOP, now)
    assert "real-shopper" in after.known_fingerprints
    assert "refunded-bot" not in after.known_fingerprints  # a refunded capture was the attacker's
    assert "blocked-now" not in after.known_fingerprints
    # 40 blocked attempts in one hour did not raise "normal" volume (one real capture barely moves it).
    assert after.attempts_per_hour_mean < before.attempts_per_hour_mean + 0.1


def closed_incident(opened: datetime, minutes: int, classification: str | None, confidence: float | None):
    return Incident(
        shop_id=SHOP,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=minutes),
        status=IncidentStatus.CLOSED,
        classification=classification,
        confidence=confidence,
    )


async def test_a_customer_first_seen_in_a_genuine_rush_incident_is_known_later(db):
    """Rate statistics drop every incident window; the known-customer sets keep captures from incidents
    the AI confidently called harmless (a flash sale's new shoppers are returning customers next time)."""
    await seed_history(db, SHOP, days=7)
    now = datetime.now(UTC)
    windows = {
        "sale-shopper": ("genuine_rush", 0.9),
        "weak-rush-shopper": ("genuine_rush", 0.5),
        "attack-capture": ("card_testing", 0.95),
        "unjudged-capture": (None, None),
    }
    for n, (fp, (cls, conf)) in enumerate(windows.items()):
        opened = now - timedelta(hours=10 + 3 * n)
        db.add(closed_incident(opened, 30, cls, conf))
        db.add(attack_row(opened + timedelta(minutes=5), 0, outcome=Outcome.CAPTURED, fp=fp))
    await db.commit()

    base = await baseline_mod.load_baseline(db, SHOP, now)
    assert "sale-shopper" in base.known_fingerprints
    assert not {"weak-rush-shopper", "attack-capture", "unjudged-capture"} & base.known_fingerprints


async def test_incident_hours_are_left_out_of_the_hourly_series_not_zero_filled(db):
    """Three busy hours inside an incident become missing hours, not zero-traffic hours, so they do not
    drag the mean down (which would inflate velocity_z for ordinary traffic)."""
    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    start = now - timedelta(hours=30)
    db.add_all(
        attack_row(start + timedelta(hours=h, minutes=m), 0, outcome=Outcome.CAPTURED, fp=f"s{h}-{m}")
        for h in range(28)
        for m in range(0, 60, 6)  # 10 attempts every hour
    )
    await db.commit()
    without = await baseline_mod.load_baseline(db, SHOP, now)
    assert 9 <= without.attempts_per_hour_mean <= 10

    db.add(closed_incident(start + timedelta(hours=10, minutes=15), 180, "card_testing", 0.9))
    await db.commit()
    with_incident = await baseline_mod.load_baseline(db, SHOP, now)
    assert with_incident.attempts_per_hour_mean >= without.attempts_per_hour_mean - 0.1


# ---- per-device velocity breaker (report item 21) ------------------------------------------------------


def test_device_limits_with_and_without_a_challenge_token():
    assert not device_over_limit(5, 1, has_token=False)
    assert device_over_limit(6, 0, has_token=False) and device_over_limit(0, 2, has_token=False)
    assert not device_over_limit(11, 3, has_token=True)
    assert device_over_limit(12, 0, has_token=True) and device_over_limit(0, 4, has_token=True)


def shopper(device: str, outcome: str = "approve", ip: str = "203.0.113.7", token: str | None = None):
    return CheckoutRequest(
        shop_id=SHOP,
        sku="SKU-PILLAR-18",
        method="wallet",
        device_id=device,
        user_agent="Mozilla/5.0",
        ip=ip,
        email="jo@gmail.com",
        challenge_token=token,
        offline=OfflinePlan(outcome, is_bot=False),
    )


async def test_six_attempts_in_ten_minutes_challenge_the_device(db):
    results = [await place_order(db, None, shopper("dev-fast")) for _ in range(7)]
    assert [r.decision for r in results[:6]] == [Decision.ALLOW] * 6
    assert results[6].decision == Decision.CHALLENGE and results[6].reason == "device_velocity"
    assert results[6].outcome == Outcome.CHALLENGED

    # A solved challenge lets a person continue, but only up to 12 attempts in the window.
    token = issue_challenge_token(fingerprint("dev-fast", "Mozilla/5.0"))
    more = [await place_order(db, None, shopper("dev-fast", token=token)) for _ in range(6)]
    assert [r.decision for r in more[:5]] == [Decision.ALLOW] * 5  # attempts 8-12 (the 7th was challenged)
    # Past the token limit another challenge could not help: blocked (not looped on a challenge) until
    # the device's attempts age out of the window.
    assert more[5].decision == Decision.BLOCK and more[5].reason == "device_velocity_limit"
    assert fingerprint("dev-fast", "Mozilla/5.0") not in await blocked(db)  # not a permanent block


async def test_two_declines_challenge_the_device_and_four_beat_a_token(db):
    res = [await place_order(db, None, shopper("dev-unlucky", "decline")) for _ in range(3)]
    assert [r.outcome for r in res[:2]] == [Outcome.DECLINED] * 2
    assert res[2].reason == "device_velocity"
    token = issue_challenge_token(fingerprint("dev-unlucky", "Mozilla/5.0"))
    res = [await place_order(db, None, shopper("dev-unlucky", "decline", token=token)) for _ in range(3)]
    assert [r.outcome for r in res[:2]] == [Outcome.DECLINED] * 2 and res[2].reason == "device_velocity_limit"


async def test_parallel_requests_from_one_device_cannot_outrun_the_breaker(db, sandbox, monkeypatch):
    """A bot firing requests in parallel: each waits on PayPal for a while, yet only `attempts` (6) of
    them reach it, because each request's row is committed before its PayPal call."""
    import asyncio

    from app.shop import service

    real_create = service.create_order

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(0.2)
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(service, "create_order", slow_create)
    sessions = db_session.get_sessionmaker()

    async def one():
        async with sessions() as s:
            req = shopper("dev-parallel")
            req.offline = None
            return await place_order(s, sandbox.client, req)

    results = await asyncio.gather(*(one() for _ in range(10)))
    allowed = [r for r in results if r.decision == Decision.ALLOW]
    assert len(allowed) == 6 and all(r.order_id for r in allowed)
    assert {r.reason for r in results if r.decision != Decision.ALLOW} == {"device_velocity"}


async def test_device_velocity_never_keys_on_ip_and_forgets_old_attempts(db):
    # Ten shoppers behind one carrier NAT (same IP): none of them is slowed down.
    res = [await place_order(db, None, shopper(f"dev-nat-{i}")) for i in range(10)]
    assert {r.decision for r in res} == {Decision.ALLOW}

    old = datetime.now(UTC) - timedelta(minutes=11)
    fp = fingerprint("dev-yesterday", "Mozilla/5.0")
    db.add_all(attack_row(old, i, fp=fp) for i in range(8))
    await db.commit()
    assert (await place_order(db, None, shopper("dev-yesterday"))).decision == Decision.ALLOW


async def test_kill_switch_turns_the_device_breaker_off(db):
    ms = await db.get(MerchantSettings, SHOP)
    ms.kill_switch = True
    await db.commit()
    invalidate_cache()
    res = [await place_order(db, None, shopper("dev-owner-testing")) for _ in range(8)]
    assert {r.decision for r in res} == {Decision.ALLOW}


# ---- analyst prompt and eval (report item 10) ----------------------------------------------------------


def test_analyst_input_puts_pattern_signals_first_and_the_sale_window_in_context():
    inp = analyst.build_input(
        features={},
        baseline=baseline_mod.Baseline(),
        triggers=["velocity_spike"],
        sale_window={"note": "Autumn sale"},
        guard_state={"mode": "normal"},
        actions_so_far=[],
        sample=[],
    )
    keys = list(inp)
    assert keys[:2] == ["triggers", "windows"] and keys[-1] == "context"
    assert inp["context"]["sale_window_active"] and inp["context"]["sale_note"] == "Autumn sale"
    assert "sale_window_active" not in inp
    rush = analyst.SYSTEM.index("- genuine_rush")
    assert "unannounced" in analyst.SYSTEM[rush : analyst.SYSTEM.index("- paypal_outage")]
    assert analyst.SYSTEM.index("Supporting context") > analyst.SYSTEM.index("- unclear")


def test_eval_has_an_unannounced_rush_case_scored_separately():
    from scripts import eval_ai

    case = eval_ai.CASES["flash_sale_no_hint"]
    assert case.scenario == "flash_sale" and case.sale_window is None
    assert case.expected == {"genuine_rush"} and case.min_seeds >= 3 and case.separate
    assert eval_ai.CASES["flash_sale"].sale_window is not None
    now = datetime.now(UTC)
    rows = eval_ai.rows_for("card_testing", seed=1, now=now)
    assert any(r.decline_category == "fraud" for r in rows)  # same code table as the shop flow


# ---- report wording when the rules (not the AI) acted ---------------------------------------------------


def test_template_alert_never_says_nothing_was_blocked_when_rules_blocked():
    facts = {
        "classification": reporter.LABELS["unclear"],
        "defences": reporter.DEFENCE_TEXT[ActionType.BLOCK_FP],
    }
    alert = reporter.fill(reporter.template(facts).alert, facts)
    assert "blocked the devices" in alert and "Nothing was blocked" not in alert
    quiet = facts | {"defences": reporter.NO_DEFENCES}
    assert "Nothing was blocked" in reporter.fill(reporter.template(quiet).alert, quiet)


def test_defence_text_claims_neither_an_attack_nor_genuine_shoppers():
    """BLOCK_FP is also a rules-only guess (R3b), and RELAX also follows a paypal_outage verdict."""
    assert "attack" not in reporter.DEFENCE_TEXT[ActionType.BLOCK_FP]
    assert "genuine" not in reporter.DEFENCE_TEXT[ActionType.RELAX]
