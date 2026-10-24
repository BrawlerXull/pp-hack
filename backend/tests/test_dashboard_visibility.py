"""Dashboard visibility (P7): worker heartbeat and the summary's system block, the detect → judge → decide
strip's data (trigger tiers, dropped AI claims, fallback reason, guard reasons), ×N vs normal from our own
baseline, 10-second chart buckets, detection delay, alert filtering, the War Room switches and demo guards.
Gemini and PayPal are always faked; nothing here leaves the machine."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import gemini
from app.ai.schemas import Evidence, Verdict
from app.api import demo as demo_api
from app.api.dashboard import detection_delay_s
from app.api.deps import get_paypal, require_owner
from app.config import Settings
from app.db.models import (
    AuditLog,
    CheckoutAttempt,
    Incident,
    MerchantSettings,
    Source,
    SystemStatus,
)
from app.db.session import get_session
from app.detection import normal
from app.detection.features import Baseline, Features
from app.incidents.service import IncidentService, incident_event
from app.main import app
from app.shop.service import attempt_event, place_order
from app.simulator import runner
from app.simulator.runner import SimRun, to_request
from app.simulator.scenarios import card_testing
from app.simulator.seed import seed_history
from app.worker import heartbeat
from tests.conftest import SHOP
from tests.test_pipeline import run_scenario

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
BASE = {"attempts_per_hour_mean": 24.0, "decline_rate": 0.05, "error_rate": 0.0}


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


# ---- ×N vs normal (pure) ---------------------------------------------------------------------------


def test_vs_normal_uses_the_shops_baseline_per_window():
    assert normal.normal_value("attempts", 15, BASE) == 6.0  # 24 an hour → 6 per 15 minutes
    assert normal.vs_normal("attempts", 15, 60, BASE) == 10.0
    assert normal.vs_normal("attempts_per_min", 5, 4.0, BASE) == 10.0
    assert normal.vs_normal("decline_rate", 15, 0.8, BASE) == 16.0
    # A near-zero normal is floored: three errors must not read as "×1000 vs normal".
    assert normal.vs_normal("error_rate", 5, 0.5, BASE) == 100.0
    assert normal.vs_normal("top_sku_share", 15, 0.9, BASE) is None  # no meaningful normal
    assert normal.vs_normal("attempts", 15, None, BASE) is None
    assert normal.vs_normal("attempts", 15, 10, {}) is None


def test_a_quiet_shops_ordinary_orders_are_not_a_spike():
    quiet = {"attempts_per_hour_mean": 3.0}  # 0.25 per 5 minutes: two orders would read "×8"
    assert normal.vs_normal("attempts", 5, 2, quiet) == 1.0
    assert normal.vs_normal("attempts_per_min", 5, 0.4, quiet) == 1.0
    assert normal.vs_normal("attempts", 5, 40, quiet) == 20.0  # a burst still stands out


def test_evidence_multiplier_ignores_the_models_own_baseline_and_observed():
    # The model claims a baseline of 59 and an observed 60.4; we use our stored numbers only.
    verdict = {
        "evidence": [
            {"feature": "attempts", "window_min": 15, "observed": 60.4, "baseline": 59, "why": "x"},
            {"feature": "top_sku_share", "window_min": 15, "observed": 0.9, "baseline": 0.1, "why": "y"},
        ]
    }
    features = {"15m": {"attempts": 60, "top_sku_share": 0.9}, "baseline": BASE}
    rows = normal.evidence_vs_normal(verdict, features)
    assert rows[0] == {
        "feature": "attempts",
        "window_min": 15,
        "observed": 60,
        "normal": 6.0,
        "multiplier": 10.0,
    }
    assert rows[1]["multiplier"] is None and rows[1]["normal"] is None
    assert normal.evidence_vs_normal(None, features) == []


def test_live_snapshot_tiles():
    features = {
        5: Features(window_min=5, attempts=20, reached_paypal=20, decline_rate=0.5, captured=3),
        15: Features(window_min=15, attempts=30, blocked_or_challenged=4, sca_required=2),
    }
    snap = normal.live_snapshot(features, Baseline(attempts_per_hour_mean=48.0, decline_rate=0.05))
    assert snap["5m"]["attempts"] == 20 and snap["5m"]["attempts_vs_normal"] == 5.0
    assert snap["5m"]["decline_rate_vs_normal"] == 10.0
    assert snap["15m"]["stopped"] == 6
    assert snap["15m"]["decline_rate_vs_normal"] is None  # nothing reached PayPal: no rate to compare
    assert snap["baseline"]["attempts_per_hour_mean"] == 48.0


# ---- heartbeat and the system block ----------------------------------------------------------------


def row(age_s: float, **kw) -> SystemStatus:
    return SystemStatus(component="worker", started_at=NOW, heartbeat_at=NOW - timedelta(seconds=age_s), **kw)


AI_OK = {"state": "ok", "detail": "configured", "models": [{"model": "m1", "paused": False}]}


def test_worker_is_alive_only_with_a_fresh_heartbeat():
    s = Settings(_env_file=None)
    assert heartbeat.system_block(row(5), AI_OK, s, NOW, card_capture_seen=False)["worker"]["alive"]
    live = {"5m": {}, "at": NOW.isoformat()}
    dead = heartbeat.system_block(row(16, live=live), AI_OK, s, NOW, card_capture_seen=False)
    assert not dead["worker"]["alive"] and dead["live"] is None  # stale tiles are not shown
    never = heartbeat.system_block(None, AI_OK, s, NOW, card_capture_seen=False)
    assert not never["worker"]["alive"] and never["worker"]["heartbeat_at"] is None


def at(seconds_before: float) -> datetime:
    return NOW - timedelta(seconds=seconds_before)


def test_detector_state_tells_running_slow_stalled_and_failing_apart():
    def state(**kw) -> str:
        return heartbeat.detector_state(row(1, **kw), NOW)

    assert state(last_tick_at=at(3)) == "running"
    assert state(last_tick_at=at(45)) == "slow"  # e.g. waiting on Gemini
    assert state(last_tick_at=at(200)) == "stalled"
    # Every pass raises while the heartbeat beats on: failing, never green.
    assert (
        state(last_tick_at=at(200), last_tick_error_at=at(2), last_tick_error="OperationalError") == "failing"
    )
    assert state(last_tick_at=None, last_tick_error_at=at(2)) == "failing"
    # A later good pass clears it.
    assert state(last_tick_at=at(1), last_tick_error_at=at(6)) == "running"
    # No pass finished since the start: counted from started_at, not "running" forever.
    started = SystemStatus(component="worker", started_at=at(200), heartbeat_at=at(1))
    assert heartbeat.detector_state(started, NOW) == "stalled"
    assert heartbeat.detector_state(None, NOW) == "stopped"


def test_live_tiles_need_a_recent_pass_from_this_process():
    s = Settings(_env_file=None)

    def live(at_: datetime, started_at: datetime = NOW - timedelta(hours=1)) -> dict | None:
        r = row(1, live={"5m": {}, "at": at_.isoformat()})
        r.started_at = started_at
        return heartbeat.system_block(r, AI_OK, s, NOW, card_capture_seen=False)["live"]

    assert live(at(10)) is not None
    assert live(at(90)) is None  # the detector has not finished a pass for a while
    assert live(at(10), started_at=at(5)) is None  # the previous process's numbers
    assert (
        heartbeat.system_block(row(1, live={"5m": {}}), AI_OK, s, NOW, card_capture_seen=False)["live"]
        is None
    )


def test_ai_pill_live_cooling_down_and_rules_only():
    s = Settings(_env_file=None)

    def ai(state: str, untils: list[str | None], detail: str = "d") -> dict:
        models = [{"model": f"m{i}", "paused": True, "until": u} for i, u in enumerate(untils)]
        return heartbeat.system_block(
            row(1, last_ai_model="m1-001"),
            {"state": state, "detail": detail, "models": models},
            s,
            NOW,
            card_capture_seen=False,
        )["ai"]

    live = heartbeat.system_block(row(1, last_ai_model="m1-001"), AI_OK, s, NOW, card_capture_seen=False)
    assert (
        live["ai"]["mode"] == "live" and live["ai"]["last_model"] == "m1-001" and live["ai"]["reason"] is None
    )
    assert ai("degraded", ["2026-10-24T12:01:00+00:00"])["mode"] == "live"
    cooling = ai("cooling_down", ["2026-10-24T12:05:00+00:00", "2026-10-24T12:01:00+00:00"], "quota")
    assert cooling == cooling | {
        "mode": "cooling_down",
        "until": "2026-10-24T12:01:00+00:00",
        "reason": "quota",
    }
    # Paused until restart (no end time) or no key at all: rules only, with the reason.
    assert ai("cooling_down", [None, "2026-10-24T12:01:00+00:00"])["mode"] == "rules_only"
    rejected = ai("key_rejected", [None], "Gemini API key rejected")
    assert rejected["mode"] == "rules_only" and rejected["reason"] == "Gemini API key rejected"


def test_paypal_pill_is_sandbox_and_card_processing_unknown_without_evidence():
    s = Settings(_env_file=None, paypal_client_id="id", paypal_client_secret="secret")
    block = heartbeat.system_block(row(1), AI_OK, s, NOW, card_capture_seen=False)
    assert block["paypal"] == {"environment": "sandbox", "configured": True, "card_processing": "unknown"}
    seen = heartbeat.system_block(row(1), AI_OK, s, NOW, card_capture_seen=True)
    assert seen["paypal"]["card_processing"] == "seen_working"


async def test_beat_upserts_and_keeps_the_last_model_across_restarts(db, monkeypatch):
    outcome = {"model": "gemini-x-001", "at": NOW, "error": None, "error_at": None}
    monkeypatch.setattr(gemini, "last_outcome", lambda: dict(outcome))
    await heartbeat.beat(
        db,
        NOW,
        started_at=NOW,
        last_tick_at=NOW,
        live={"5m": {"attempts": 1}},
        last_tick_error_at=NOW,
        last_tick_error="OperationalError",
    )
    # A restarted worker knows no model yet: the stored one stays.
    outcome.update(model=None, at=None, error="m1: quota", error_at=NOW)
    later = NOW + timedelta(seconds=5)
    await heartbeat.beat(db, later, started_at=later, last_tick_at=None, live=None)
    stored = await db.get(SystemStatus, "worker", populate_existing=True)
    assert stored.heartbeat_at == later and stored.started_at == later
    assert stored.last_ai_model == "gemini-x-001" and stored.last_ai_error == "m1: quota"
    # The detector fields are the new process's own: nothing yet, not the old process's ticks and tiles.
    assert stored.last_tick_at is None and stored.live is None and stored.last_tick_error_at is None


async def test_gemini_records_its_last_answer_and_failure(monkeypatch):
    class Out(BaseModel):
        ok: bool

    settings = Settings(_env_file=None, gemini_api_key="k", gemini_model="m1", gemini_fallback_models="m2")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini, "_last", {"model": None, "at": None, "error": None, "error_at": None})

    class Models:
        async def generate_content(self, *, model, contents, config):
            if model == "m1":
                raise ValueError("bad json")
            return SimpleNamespace(parsed=None, text='{"ok": true}', model_version="m2-001")

    monkeypatch.setattr(gemini, "_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=Models())))
    await gemini.generate_json(system="s", prompt="p", schema=Out)
    last = gemini.last_outcome()
    assert last["model"] == "m2-001" and last["at"] is not None
    # Only a classified reason reaches the dashboard, never the raw SDK text.
    assert last["error"] == "m1: unusable answer (ValueError)" and last["error_at"] is not None


async def test_summary_carries_system_switches_and_the_running_sale(db, client):
    db.add(SystemStatus(component="worker", started_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC)))
    ms = await db.get(MerchantSettings, SHOP)
    now = datetime.now(UTC)
    end = (now + timedelta(hours=1)).isoformat()
    ms.sale_windows = [{"start": (now - timedelta(minutes=5)).isoformat(), "end": end, "note": "Autumn"}]
    ms.kill_switch = True
    await db.commit()
    body = (await client.get("/dashboard/summary")).json()
    assert body["system"]["worker"]["alive"] is True
    assert body["system"]["worker"]["state"] == "running" and body["system"]["worker"]["assessing"] is False
    assert body["system"]["ai"]["mode"] == "rules_only"  # the test settings have no Gemini key
    assert body["system"]["simulator_enabled"] is True
    assert body["system"]["paypal"]["environment"] == "sandbox"
    assert body["switches"] == {"kill_switch": True, "autopilot": True}
    assert body["sale_window"] == {"end": end, "note": "Autumn"}


async def test_card_processing_is_seen_only_from_a_real_card_capture(db, client):
    async def card(paypal_live: bool) -> None:
        db.add(
            CheckoutAttempt(
                shop_id=SHOP,
                sku="SKU-TEALIGHT-3",
                amount=4,
                method="card",
                fingerprint_hash="f" * 64,
                guard_decision="allow",
                outcome="captured",
                capture_id="CAP1",
                paypal_live=paypal_live,
            )
        )
        await db.commit()

    await card(paypal_live=False)  # simulated: proves nothing about the PayPal account
    assert (await client.get("/dashboard/summary")).json()["system"]["paypal"]["card_processing"] == "unknown"
    await card(paypal_live=True)
    body = (await client.get("/dashboard/summary")).json()
    assert body["system"]["paypal"]["card_processing"] == "seen_working"


# ---- detect → judge → decide data ------------------------------------------------------------------


def test_incident_event_exposes_tiers_dropped_claims_and_fallback_reason():
    inc = Incident(
        id="i1",
        shop_id=SHOP,
        opened_at=NOW,
        status="open",
        ai_available=False,
        triggers=["velocity_spike", "fraud_declines", "something_new"],
        dropped_evidence=2,
        ai_fallback_reason="Gemini quota exhausted",
        model_id="rules-fallback",
    )
    ev = incident_event(inc)
    assert ev["trigger_tiers"] == {"velocity_spike": "volume", "fraud_declines": "fraud"}
    assert ev["dropped_evidence"] == 2 and ev["ai_fallback_reason"] == "Gemini quota exhausted"
    assert ev["model_id"] == "rules-fallback"


def ai_with_one_invented_claim(monkeypatch, available: bool = True) -> None:
    async def fn(*, system, prompt, schema, temperature=0.1):
        if schema is not Verdict:
            raise gemini.AIUnavailable("reports use the template in tests")
        if not available:
            raise gemini.AIUnavailable("quota", "Gemini quota exhausted")
        observed = json.loads(prompt.split("\n\n", 1)[1])["windows"]["15m"]["attempts"]
        ev = [
            Evidence(feature="attempts", window_min=15, observed=observed, why="volume"),
            Evidence(feature="attempts", window_min=15, observed=observed * 10 + 50, why="invented"),
        ]
        return Verdict(
            classification="card_testing", confidence=0.95, summary="s", evidence=ev, recommended_actions=[]
        ), "mock-model"

    monkeypatch.setattr("app.ai.analyst.generate_json", fn)
    monkeypatch.setattr("app.ai.reporter.generate_json", fn)


async def test_dropped_ai_claims_and_fallback_reason_are_recorded_on_the_incident(db, client, monkeypatch):
    ai_with_one_invented_claim(monkeypatch)
    await seed_history(db, SHOP, days=7)
    await run_scenario(db, card_testing, 60)
    incident = await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    assert incident.dropped_evidence == 1 and incident.ai_fallback_reason is None

    detail = (await client.get(f"/dashboard/incidents/{incident.id}")).json()
    assert detail["dropped_evidence"] == 1 and detail["trigger_tiers"]
    assert len(detail["evidence_vs_normal"]) == 1
    assert detail["evidence_vs_normal"][0]["multiplier"] > 1
    assert detail["detected_in_s"] is not None and detail["detected_in_s"] >= 0

    # The next re-check fails: the verdict (and its dropped count) is kept, and the reason is on record.
    incident.last_assessed_at -= timedelta(minutes=11)
    incident.verdict_at -= timedelta(minutes=11)
    await db.commit()
    ai_with_one_invented_claim(monkeypatch, available=False)
    await IncidentService(db, None, timedelta(minutes=2)).tick(SHOP)
    await db.refresh(incident)
    assert incident.ai_refresh_failed_at is not None and incident.dropped_evidence == 1
    assert incident.ai_fallback_reason == "Gemini quota exhausted"
    # The kept verdict's rows still show the numbers it was checked against, not the re-check's.
    incident.features = incident.features | {"15m": {"attempts": 7}}
    await db.commit()
    kept = (await client.get(f"/dashboard/incidents/{incident.id}")).json()
    assert kept["evidence_vs_normal"] == detail["evidence_vs_normal"]


async def test_attempt_event_says_why_the_guard_decided(db):
    a = next(iter(card_testing("offline", count=1, seed=1)))
    res = await place_order(db, None, to_request(SHOP, "test", a, "offline"))
    attempt = await db.get(CheckoutAttempt, res.attempt_id)
    assert attempt_event(attempt)["guard_reason"] == "normal"


async def test_detection_delay_counts_from_the_first_flagged_device(db):
    opened = datetime.now(UTC).replace(microsecond=0)

    def attempt(fp: str, seconds_before: int, source: str = Source.SIMULATOR) -> CheckoutAttempt:
        return CheckoutAttempt(
            shop_id=SHOP,
            ts=opened - timedelta(seconds=seconds_before),
            source=source,
            sku="SKU-TEALIGHT-3",
            amount=4,
            method="card",
            fingerprint_hash=fp,
            guard_decision="allow",
            outcome="declined",
        )

    db.add_all(
        [
            attempt("shopper", 200),
            attempt("bot", 42),
            attempt("bot", 10),
            attempt("old-bot", 600),  # before the lookback: not part of this burst
            attempt("seed", 100, Source.SEED),
        ]
    )
    inc = Incident(shop_id=SHOP, opened_at=opened, signature={"fingerprints": ["bot", "old-bot"]})
    db.add(inc)
    await db.flush()
    assert await detection_delay_s(db, inc) == 42
    inc.signature = {}  # no flagged device (a rush): the burst starts at the first attempt in the lookback
    assert await detection_delay_s(db, inc) == 200


# ---- chart buckets and alerts ----------------------------------------------------------------------


async def test_timeseries_in_10_second_buckets(db, client):
    now = datetime.now(UTC)
    start = datetime.fromtimestamp((now.timestamp() // 10) * 10 - 60, UTC)  # a 10 s boundary a minute ago
    for offset, outcome in ((1, "declined"), (4, "declined"), (9, "captured"), (12, "declined")):
        db.add(
            CheckoutAttempt(
                shop_id=SHOP,
                ts=start + timedelta(seconds=offset),
                source="simulator",
                sku="SKU-TEALIGHT-3",
                amount=4,
                method="card",
                fingerprint_hash="f",
                guard_decision="allow",
                outcome=outcome,
            )
        )
    await db.commit()
    points = (await client.get("/dashboard/timeseries", params={"minutes": 10, "bucket_s": 10})).json()
    assert [(datetime.fromisoformat(p["t"]), p.get("declined"), p.get("captured")) for p in points] == [
        (start, 2, 1),
        (start + timedelta(seconds=10), 1, None),
    ]
    per_minute = (await client.get("/dashboard/timeseries", params={"minutes": 10})).json()
    assert sum(p.get("declined", 0) for p in per_minute) == 3 and "minute" in per_minute[0]
    too_many = await client.get("/dashboard/timeseries", params={"minutes": 1440, "bucket_s": 10})
    assert too_many.status_code == 422


async def test_alerts_can_be_narrowed_to_one_incident(db, client):
    a, b = Incident(shop_id=SHOP), Incident(shop_id=SHOP)
    db.add_all([a, b])
    await db.flush()
    db.add_all(
        [
            AuditLog(actor="system", event="alert.sent", incident_id=a.id, data={"text": "first"}),
            AuditLog(actor="system", event="alert.sent", incident_id=b.id, data={"text": "second"}),
        ]
    )
    await db.commit()
    assert [r["text"] for r in (await client.get("/dashboard/alerts")).json()] == ["second", "first"]
    only_a = (await client.get("/dashboard/alerts", params={"incident_id": a.id})).json()
    assert [r["text"] for r in only_a] == ["first"]


# ---- War Room switches and demo guards -------------------------------------------------------------


async def test_switches_change_one_flag_and_leave_the_rest(db, client):
    ms = await db.get(MerchantSettings, SHOP)
    ms.sale_windows = [{"start": NOW.isoformat(), "end": NOW.isoformat(), "note": "kept"}]
    await db.commit()
    body = (await client.patch("/settings/switches", json={"kill_switch": True})).json()
    assert (
        body["kill_switch"] is True
        and body["autopilot"] is True
        and body["sale_windows"][0]["note"] == "kept"
    )
    body = (await client.patch("/settings/switches", json={"autopilot": False})).json()
    assert body["kill_switch"] is True and body["autopilot"] is False
    q = select(AuditLog.data).where(AuditLog.event == "settings.switches").order_by(AuditLog.id)
    assert list((await db.execute(q)).scalars()) == [{"kill_switch": True}, {"autopilot": False}]


async def test_demo_reset_switches_the_guard_back_on(db, client):
    ms = await db.get(MerchantSettings, SHOP)
    ms.kill_switch, ms.autopilot = True, False
    await db.commit()
    assert (await client.post("/demo/reset")).status_code == 200
    await db.refresh(ms)
    assert ms.kill_switch is False and ms.autopilot is True


async def test_second_demo_run_is_refused_while_one_is_active(client, monkeypatch):
    monkeypatch.setattr(runner, "RUNS", {})
    monkeypatch.setattr(demo_api, "RUNS", runner.RUNS)
    runner.RUNS["r1"] = SimRun(id="r1", scenario="card_testing", mode="offline", total=10)
    res = await client.post("/demo/run/flash_sale", json={"mode": "offline"})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "run_active"
    assert res.json()["detail"]["run_id"] == "r1"
    runner.RUNS["r1"].cancelled = True  # stopped (or reset): a new run may start
    assert runner.active_run() is None
    runner.RUNS["r2"] = SimRun(id="r2", scenario="x", mode="offline", total=1, finished_at=NOW)
    assert runner.active_run() is None


async def test_flash_sale_announces_its_window_only_when_the_run_starts(db, client, monkeypatch):
    monkeypatch.setattr(runner, "RUNS", {})
    monkeypatch.setattr(demo_api, "RUNS", runner.RUNS)
    runner.RUNS["r1"] = SimRun(id="r1", scenario="card_testing", mode="offline", total=10)
    body = {"mode": "offline", "announce_sale": True}
    assert (await client.post("/demo/run/flash_sale", json=body)).status_code == 409
    ms = await db.get(MerchantSettings, SHOP)
    await db.refresh(ms)
    assert ms.sale_windows == []  # a refused run leaves no sale behind

    runner.RUNS["r1"].cancelled = True
    ms.kill_switch = True  # set by the owner meanwhile: the run must not undo it
    await db.commit()
    started = SimRun(id="r2", scenario="flash_sale", mode="offline", total=60)
    monkeypatch.setattr(demo_api, "start_run", lambda *a, **k: started)
    assert (await client.post("/demo/run/flash_sale", json=body)).status_code == 200
    await db.refresh(ms)
    assert len(ms.sale_windows) == 1 and "(simulated)" in ms.sale_windows[0]["note"]
    assert ms.kill_switch is True and ms.autopilot is True
