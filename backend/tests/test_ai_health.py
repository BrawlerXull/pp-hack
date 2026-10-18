"""/healthz and /dashboard/summary report the shared Gemini breaker state (no network: PayPal is stubbed,
the breaker reads the ai_model_state test table)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ai import breaker, gemini
from app.api.deps import get_paypal, require_owner
from app.config import Settings
from app.db.models import CooldownReason
from app.db.session import get_session
from app.main import app
from tests.conftest import SHOP

KEY = "health-key"


class StubPayPal:
    async def access_token(self) -> str:
        return "token"


@pytest.fixture
async def client(db, monkeypatch):
    settings = Settings(_env_file=None, gemini_api_key=KEY, gemini_model="m1", gemini_fallback_models="m2")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(gemini, "store", breaker.DbStore())

    async def session():
        yield db

    app.dependency_overrides.update(
        {get_paypal: lambda: StubPayPal(), get_session: session, require_owner: lambda: SHOP}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def pause(model: str, trip: breaker.Trip) -> None:
    now = datetime.now(UTC)
    name, cd = breaker.Breaker().trip(model, trip, now, "boom")
    await gemini.store.save(KEY, name, cd)


async def test_healthz_ok_and_one_paused_model_is_not_degraded(client):
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok" and body["ai"] == "configured"

    await pause("m1", breaker.Trip(CooldownReason.QUOTA, 60))
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok" and body["ai"] == "degraded"
    summary = (await client.get("/dashboard/summary")).json()
    assert summary["ai"]["state"] == "degraded"
    assert [m["paused"] for m in summary["ai"]["models"]] == [True, False]
    assert summary["ai"]["models"][0]["reason"] == "quota"


async def test_healthz_degraded_on_rejected_key_without_leaking_detail(client):
    await pause("m1", breaker.Trip(CooldownReason.BAD_KEY, None, all_models=True))
    body = (await client.get("/healthz")).json()
    assert body["status"] == "degraded" and body["ai"] == "key_rejected"
    assert "m1" not in str(body) and "until" not in str(body)  # chain and timings stay owner-only
    summary = (await client.get("/dashboard/summary")).json()
    assert summary["ai"]["state"] == "key_rejected" and "API key rejected" in summary["ai"]["detail"]


async def test_healthz_degraded_when_every_model_cools_down(client):
    for m in ("m1", "m2"):
        await pause(m, breaker.Trip(CooldownReason.OVERLOADED, timedelta(minutes=5).total_seconds()))
    body = (await client.get("/healthz")).json()
    assert body["status"] == "degraded" and body["ai"] == "cooling_down"
