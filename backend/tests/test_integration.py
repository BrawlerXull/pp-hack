"""Connecting a shop's own checkout: API keys made on the Developers page, dry-run checks, the connection
status and the public OpenAPI spec the SDKs are generated from."""

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import ApiKey, CheckoutAttempt
from app.db.session import get_session
from app.main import app

CHECK = {"sku": "TEA-3", "method": "card", "fingerprint_hash": "f" * 32, "amount": "3.00"}


@pytest.fixture
async def api(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def as_demo(api: httpx.AsyncClient) -> None:
    assert (
        await api.post("/auth/login", json={"password": get_settings().admin_password})
    ).status_code == 200


async def test_a_new_key_is_shown_once_stored_hashed_and_works(api, db):
    await as_demo(api)
    made = (await api.post("/integration/keys", json={"name": "Live store"})).json()
    secret = made["key"]
    assert secret.startswith("cg_sk_") and made["prefix"] == secret[:10]
    listed = (await api.get("/integration/keys")).json()
    assert [k["name"] for k in listed] == ["Live store"] and "key" not in listed[0]
    stored = (await db.execute(select(ApiKey))).scalar_one()
    assert secret not in stored.key_hash and stored.last_used_at is None

    res = await api.post("/v1/guard/check", json=CHECK, headers={"X-Guard-Key": secret})
    assert res.status_code == 200 and res.json()["attempt_id"]
    await db.refresh(stored)
    assert stored.last_used_at is not None
    status = (await api.get("/integration/status")).json()
    assert status["checks_24h"] == 1 and status["latest_check"]["sku"] == "TEA-3"


async def test_a_revoked_key_stops_working_at_once(api):
    await as_demo(api)
    made = (await api.post("/integration/keys", json={"name": "Old"})).json()
    assert (await api.delete(f"/integration/keys/{made['id']}")).status_code == 200
    res = await api.post("/v1/guard/check", json=CHECK, headers={"X-Guard-Key": made["key"]})
    assert res.status_code == 401
    assert (await api.get("/integration/keys")).json() == []


async def test_a_dry_run_answers_but_records_nothing(api, db):
    await as_demo(api)
    secret = (await api.post("/integration/keys", json={"name": "Test"})).json()["key"]
    res = (
        await api.post("/v1/guard/check", json={**CHECK, "dry_run": True}, headers={"X-Guard-Key": secret})
    ).json()
    assert res["decision"] and res["attempt_id"] is None and res["custom_id"] is None
    assert (await db.execute(select(CheckoutAttempt))).first() is None
    assert (await api.get("/integration/status")).json()["last_call_at"] is not None


async def test_viewers_see_keys_but_cannot_make_or_revoke_them(api):
    await api.post("/auth/signup", json={"name": "V", "email": "v@example.com", "password": "viewer-pass-1"})
    assert (await api.get("/integration/keys")).status_code == 200
    assert (await api.post("/integration/keys", json={"name": "x"})).status_code == 403


async def test_the_public_spec_has_only_the_guard_api_and_its_key_header(api):
    spec = (await api.get("/v1/openapi.json")).json()
    assert set(spec["paths"]) == {"/v1/guard/check", "/v1/guard/outcome"}
    assert spec["components"]["securitySchemes"]["GuardKey"] == {
        "type": "apiKey",
        "description": "A key from the Developers page",
        "in": "header",
        "name": "X-Guard-Key",
    }
    assert spec["paths"]["/v1/guard/check"]["post"]["operationId"] == "check"
    assert spec["servers"][0]["url"].endswith("/api")
    assert "UserOut" not in spec["components"]["schemas"]


def test_a_hosted_postgres_url_works_with_asyncpg():
    """Supabase and Neon hand out libpq URLs: sslmode becomes ssl, channel_binding goes."""
    from app.config import Settings

    url = "postgresql://u:p@db.example.com:5432/postgres?sslmode=require&channel_binding=require"
    assert (
        Settings(database_url=url).database_url
        == "postgresql+asyncpg://u:p@db.example.com:5432/postgres?ssl=require"
    )
    assert Settings(database_url="postgres://u:p@h/db").database_url == "postgresql+asyncpg://u:p@h/db"
