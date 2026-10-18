"""Gemini client layer (app/ai/gemini.py + breaker.py) against a fake genai client scripted per model.
No network: the fake replaces `gemini._client`; nothing here can reach Google."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel
from sqlalchemy import select

from app.ai import breaker, gemini
from app.ai.schemas import Verdict
from app.config import Settings
from app.db.models import AIModelState, CooldownReason

T0 = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
KEY = "test-key"


class Out(BaseModel):
    ok: bool


def quota_error(delay: str | None = "37s", message: str = "You exceeded your current quota") -> Exception:
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}] if delay else []
    body = {"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED", "details": details}}
    return genai_errors.ClientError(429, body)


def overloaded() -> Exception:
    body = {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
    return genai_errors.ServerError(503, body)


def bad_key() -> Exception:
    body = {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}],
        }
    }
    return genai_errors.ClientError(400, body)


class FakeModels:
    def __init__(self, script: dict[str, list[Any]]):
        self.script = {m: list(steps) for m, steps in script.items()}
        self.calls: list[str] = []

    async def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
        self.calls.append(model)
        step = self.script[model].pop(0)
        if isinstance(step, BaseException):
            raise step
        if step == "hang":
            await asyncio.sleep(10)
        return SimpleNamespace(parsed=None, text=step, model_version=f"{model}-001")


@pytest.fixture
def fake(monkeypatch):
    """Install a scripted fake client and a 3-model chain m1 → m2 → m3; returns a function to script it."""
    settings = Settings(
        _env_file=None,
        gemini_api_key=KEY,
        gemini_model="m1",
        gemini_fallback_models="m2,m3",
        ai_timeout_s=0.05,
    )
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    now = {"t": T0}
    monkeypatch.setattr(gemini, "_now", lambda: now["t"])

    def script(**steps: list[Any]) -> FakeModels:
        models = FakeModels({"m1": [], "m2": [], "m3": []} | steps)
        client = SimpleNamespace(aio=SimpleNamespace(models=models))
        monkeypatch.setattr(gemini, "_client", lambda: client)
        return models

    script.now = now  # type: ignore[attr-defined]
    return script


async def call() -> tuple[Out, str]:
    return await gemini.generate_json(system="s", prompt="p", schema=Out)


# ---- error classification (pure) -------------------------------------------------------------------


def test_classify_uses_retry_delay_and_spots_daily_quota():
    assert breaker.classify(quota_error("37s")) == breaker.Trip(CooldownReason.QUOTA, 37.0)
    assert breaker.classify(quota_error(None)) == breaker.Trip(CooldownReason.QUOTA, 60.0)
    daily = quota_error("20s", "Quota exceeded for metric: generate_content_free_tier_requests, PerDay")
    assert breaker.classify(daily) == breaker.Trip(CooldownReason.DAILY_QUOTA, 3600.0)


def test_classify_overload_timeout_bad_key_and_parse_errors():
    assert breaker.classify(overloaded()).reason == CooldownReason.OVERLOADED
    assert 15 <= breaker.classify(overloaded()).seconds <= 30
    assert breaker.classify(TimeoutError()).reason == CooldownReason.TIMEOUT
    assert breaker.classify(bad_key()) == breaker.Trip(CooldownReason.BAD_KEY, None, all_models=True)
    unauth = genai_errors.ClientError(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
    assert breaker.classify(unauth).all_models
    blocked_key = {
        "code": 403,
        "status": "PERMISSION_DENIED",
        "details": [{"reason": "API_KEY_SERVICE_BLOCKED"}],
    }
    assert breaker.classify(genai_errors.ClientError(403, {"error": blocked_key})).all_models
    disabled = {"code": 403, "status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}
    assert breaker.classify(genai_errors.ClientError(403, {"error": disabled})).all_models
    # A 403 scoped to one model (a preview the project isn't allowlisted for) must not stop the fallbacks.
    model_403 = {
        "code": 403,
        "status": "PERMISSION_DENIED",
        "message": "Permission denied on gemini-3.5-flash",
    }
    assert breaker.classify(genai_errors.ClientError(403, {"error": model_403})) == breaker.Trip(
        CooldownReason.BAD_REQUEST, 600.0
    )
    # A 400 that is not about the key (a config one model rejects) pauses only that model.
    other_400 = genai_errors.ClientError(400, {"error": {"code": 400, "message": "thinking not supported"}})
    assert breaker.classify(other_400) == breaker.Trip(CooldownReason.BAD_REQUEST, 600.0)
    assert breaker.classify(ValueError("bad json")) is None


def test_cooldown_expires():
    b = breaker.Breaker()
    b.trip("m1", breaker.Trip(CooldownReason.QUOTA, 30), T0)
    assert b.usable(["m1", "m2"], T0 + timedelta(seconds=29)) == ["m2"]
    assert b.usable(["m1", "m2"], T0 + timedelta(seconds=31)) == ["m1", "m2"]


# ---- generate_json with the fake client ------------------------------------------------------------


async def test_503_falls_through_to_the_next_model(fake):
    models = fake(m1=[overloaded()], m2=['{"ok": true}'])
    out, model_id = await call()
    assert out.ok and model_id == "m2-001" and models.calls == ["m1", "m2"]
    assert gemini._breaker.blocked("m1", T0).reason == CooldownReason.OVERLOADED


async def test_429_pauses_the_model_so_the_next_call_skips_it(fake):
    models = fake(m1=[quota_error("37s")], m2=['{"ok": true}', '{"ok": true}'])
    await call()
    await call()
    assert models.calls == ["m1", "m2", "m2"]  # m1 not retried while cooling down

    fake.now["t"] = T0 + timedelta(seconds=38)  # cooldown over: m1 is tried again and clears on success
    models.script["m1"].append('{"ok": true}')
    _, model_id = await call()
    assert model_id == "m1-001" and gemini._breaker.blocked("m1", fake.now["t"]) is None


async def test_all_models_cooling_raises_instantly_without_calls(fake):
    models = fake(m1=[quota_error()], m2=[quota_error()], m3=[overloaded()])
    with pytest.raises(gemini.AIUnavailable) as first:
        await call()
    assert models.calls == ["m1", "m2", "m3"] and first.value.reason == "Gemini quota exhausted"
    assert await gemini.cooling_down()

    with pytest.raises(gemini.AIUnavailable, match="cooling down") as second:
        await call()
    assert models.calls == ["m1", "m2", "m3"]  # no new calls
    assert second.value.reason == "Gemini quota exhausted"


async def test_bad_key_disables_every_model_until_restart(fake):
    models = fake(m1=[bad_key()])
    with pytest.raises(gemini.AIUnavailable) as exc:
        await call()
    assert models.calls == ["m1"] and exc.value.reason == "Gemini API key rejected"
    fake.now["t"] = T0 + timedelta(days=1)
    with pytest.raises(gemini.AIUnavailable):
        await call()
    assert models.calls == ["m1"]


async def test_timeout_pauses_briefly_and_tries_the_next_model(fake):
    models = fake(m1=["hang"], m2=['{"ok": true}'])
    _, model_id = await call()
    assert model_id == "m2-001" and models.calls == ["m1", "m2"]
    cd = gemini._breaker.blocked("m1", T0)
    assert cd.reason == CooldownReason.TIMEOUT and cd.until == T0 + timedelta(seconds=15)


async def test_parse_error_tries_the_next_model_without_pausing(fake):
    models = fake(m1=["not json"], m2=['{"ok": "maybe"}'], m3=['{"ok": false}'])
    out, model_id = await call()
    assert out.ok is False and model_id == "m3-001" and models.calls == ["m1", "m2", "m3"]
    assert gemini._breaker.usable(["m1", "m2", "m3"], T0) == ["m1", "m2", "m3"]


def _verdict_json(confidence: object) -> str:
    return (
        f'{{"classification": "card_testing", "confidence": {confidence}, "summary": "s", "evidence": [], '
        '"recommended_actions": ["SCA_ON", "REFUND_MATCHED"]}'
    )


async def test_rounding_overshoot_in_confidence_is_clamped(fake):
    fake(m1=[_verdict_json(1.02)])
    v, model_id = await gemini.generate_json(system="s", prompt="p", schema=Verdict)
    assert v.confidence == 1.0 and model_id == "m1-001"
    assert Verdict.model_validate({**v.model_dump(), "confidence": -0.03}).confidence == 0.0


async def test_percent_scale_confidence_is_rejected_not_clamped_to_certainty(fake):
    # 72 meaning 72% must not become 1.0 (that would clear the refund threshold): the next model answers.
    models = fake(m1=[_verdict_json(72)], m2=[_verdict_json(1.4)], m3=[_verdict_json(0.72)])
    v, model_id = await gemini.generate_json(system="s", prompt="p", schema=Verdict)
    assert models.calls == ["m1", "m2", "m3"] and model_id == "m3-001" and v.confidence == 0.72
    for bad in (60, 1.4, -0.3, float("nan")):
        with pytest.raises(ValueError):
            Verdict.model_validate({**v.model_dump(), "confidence": bad})


async def test_percent_scale_confidence_everywhere_ends_in_the_rules_fallback(fake):
    fake(m1=[_verdict_json(60)], m2=[_verdict_json(72)], m3=[_verdict_json(90)])
    with pytest.raises(gemini.AIUnavailable):
        await gemini.generate_json(system="s", prompt="p", schema=Verdict)
    assert gemini._breaker.usable(["m1", "m2", "m3"], T0) == ["m1", "m2", "m3"]  # bad answers pause nothing


async def test_missing_key_raises_without_touching_the_client(monkeypatch):
    monkeypatch.setattr(gemini, "get_settings", lambda: Settings(_env_file=None, gemini_api_key=""))
    with pytest.raises(gemini.AIUnavailable) as exc:
        await call()
    assert exc.value.reason == "no Gemini API key configured"


# ---- shared state in Postgres (worker and API process agree) ---------------------------------------


async def test_breaker_state_is_shared_through_the_db(db, fake, monkeypatch):
    monkeypatch.setattr(gemini, "store", breaker.DbStore())
    fake(m1=[quota_error("37s")], m2=['{"ok": true}'])
    await call()

    row = await db.get(AIModelState, "m1")
    assert row.reason == CooldownReason.QUOTA and row.key_id == breaker.key_id(KEY) and KEY not in row.key_id
    assert row.until == T0 + timedelta(seconds=37)

    # Another process (fresh in-memory breaker) sees the pause, e.g. /healthz in the API.
    monkeypatch.setattr(gemini, "_breaker", breaker.Breaker())
    st = await gemini.status()
    assert st["state"] == "degraded" and st["models"][0]["paused"] and st["models"][0]["reason"] == "quota"
    assert not st["models"][1]["paused"]

    # Same process later: m1's cooldown is over, it is tried again and its success deletes the row.
    fake.now["t"] = T0 + timedelta(seconds=38)
    monkeypatch.setattr(gemini, "_breaker", breaker.Breaker())
    models = fake(m1=['{"ok": true}'])
    await call()
    assert models.calls == ["m1"]
    db.expire_all()
    assert (await db.execute(select(AIModelState))).scalars().all() == []


async def test_rejected_key_is_shared_and_cleared_on_restart(db, fake, monkeypatch):
    monkeypatch.setattr(gemini, "store", breaker.DbStore())
    fake(m1=[bad_key()])
    with pytest.raises(gemini.AIUnavailable):
        await call()
    monkeypatch.setattr(gemini, "_breaker", breaker.Breaker())
    st = await gemini.status()
    assert st["state"] == "key_rejected" and "API key rejected" in st["detail"]

    await breaker.reset_until_restart()
    monkeypatch.setattr(gemini, "_breaker", breaker.Breaker())
    assert (await gemini.status())["state"] == "ok"


async def test_stale_in_process_key_rejection_yields_to_the_shared_table(db, fake, monkeypatch):
    # API process saw the key rejected earlier; the worker restarted since and deleted the row.
    monkeypatch.setattr(gemini, "store", breaker.DbStore())
    gemini._breaker.trip("m1", breaker.Trip(CooldownReason.BAD_KEY, None, all_models=True), T0)
    assert not await gemini.cooling_down()
    models = fake(m1=['{"ok": true}'])
    await call()
    assert models.calls == ["m1"]
