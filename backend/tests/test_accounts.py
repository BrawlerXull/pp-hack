"""Team accounts: signup, login, roles (owner / viewer) and the team endpoints, with real session cookies."""

import httpx
import pytest
from sqlalchemy import select

from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import Role, User
from app.db.session import get_session
from app.main import app

NEW = {"name": "Asha", "email": "Asha@Example.com", "password": "candles-at-night"}


@pytest.fixture
async def api(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def as_demo(api: httpx.AsyncClient) -> None:
    res = await api.post("/auth/login", json={"password": get_settings().admin_password})
    assert res.status_code == 200


async def test_the_demo_account_signs_in_with_the_admin_password_alone_or_with_its_email(api):
    await as_demo(api)
    me = (await api.get("/auth/me")).json()
    assert me["role"] == "owner" and me["is_demo"] and me["email"] == get_settings().demo_email
    api.cookies.clear()
    res = await api.post(
        "/auth/login", json={"email": get_settings().demo_email, "password": get_settings().admin_password}
    )
    assert res.status_code == 200
    assert (await api.post("/auth/login", json={"password": "nope"})).status_code == 401


async def test_signup_makes_a_viewer_who_can_look_but_not_change_settings(api, db):
    res = await api.post("/auth/signup", json=NEW)
    assert res.status_code == 200 and res.json()["user"]["role"] == "viewer"
    assert (await api.get("/auth/me")).json()["email"] == "asha@example.com"
    assert (await api.get("/settings")).status_code == 200
    res = await api.patch("/settings/switches", json={"kill_switch": True})
    assert res.status_code == 403 and "owner" in res.json()["detail"]
    stored = (await db.execute(select(User).where(User.email == "asha@example.com"))).scalar_one()
    assert stored.password_hash and NEW["password"] not in stored.password_hash


async def test_signup_checks_the_email_the_password_and_duplicates(api):
    assert (await api.post("/auth/signup", json={**NEW, "email": "not-an-email"})).status_code == 422
    assert (await api.post("/auth/signup", json={**NEW, "password": "short"})).status_code == 422
    assert (await api.post("/auth/signup", json=NEW)).status_code == 200
    res = await api.post("/auth/signup", json={**NEW, "email": "ASHA@example.com"})
    assert res.status_code == 409


async def test_a_member_signs_in_with_their_own_password(api):
    await api.post("/auth/signup", json=NEW)
    api.cookies.clear()
    assert (await api.get("/auth/me")).status_code == 401
    wrong = await api.post("/auth/login", json={"email": NEW["email"], "password": "candles-at-noon"})
    assert wrong.status_code == 401
    ok = await api.post("/auth/login", json={"email": " asha@example.com ", "password": NEW["password"]})
    assert ok.status_code == 200 and ok.json()["user"]["name"] == "Asha"


async def test_an_owner_promotes_a_viewer_and_a_viewer_cannot_change_roles(api):
    member = (await api.post("/auth/signup", json=NEW)).json()["user"]
    assert (await api.patch(f"/auth/team/{member['id']}", json={"role": "owner"})).status_code == 403
    api.cookies.clear()
    await as_demo(api)
    res = await api.patch(f"/auth/team/{member['id']}", json={"role": "owner"})
    assert res.status_code == 200 and res.json()["role"] == "owner"
    assert len((await api.get("/auth/team")).json()) == 2


async def test_the_demo_account_stays_an_owner(api, db):
    await as_demo(api)
    demo = (await db.execute(select(User).where(User.is_demo))).scalar_one()
    assert (await api.patch(f"/auth/team/{demo.id}", json={"role": "viewer"})).status_code == 403
    assert (await api.delete(f"/auth/team/{demo.id}")).status_code == 409  # yourself


async def test_a_removed_member_is_signed_out_at_once(api):
    member = (await api.post("/auth/signup", json=NEW)).json()
    viewer_cookies = dict(api.cookies)
    api.cookies.clear()
    await as_demo(api)
    assert (await api.delete(f"/auth/team/{member['user']['id']}")).status_code == 200
    api.cookies.clear()
    api.cookies.update(viewer_cookies)
    assert (await api.get("/auth/me")).status_code == 401


async def test_a_session_from_before_accounts_is_refused(api):
    from app.api.deps import _serializer

    old = _serializer(get_settings()).dumps({"role": "owner", "shop": "wick-and-wax", "v": "abcd"})
    assert (await api.get("/auth/me", headers={"Authorization": f"Bearer {old}"})).status_code == 401


async def test_a_member_changes_their_password(api):
    await api.post("/auth/signup", json=NEW)
    res = await api.post("/auth/password", json={"current": "wrong-one!", "new": "a-new-password"})
    assert res.status_code == 400
    res = await api.post("/auth/password", json={"current": NEW["password"], "new": "a-new-password"})
    assert res.status_code == 200
    api.cookies.clear()
    ok = await api.post("/auth/login", json={"email": NEW["email"], "password": "a-new-password"})
    assert ok.status_code == 200


async def test_signups_can_be_closed_or_made_owners(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "signup_role", Role.OWNER)
    assert (await api.post("/auth/signup", json=NEW)).json()["user"]["role"] == "owner"
    monkeypatch.setattr(get_settings(), "signup_enabled", False)
    assert (await api.post("/auth/signup", json={**NEW, "email": "b@example.com"})).status_code == 403


async def test_the_login_page_learns_the_demo_email_without_signing_in(api):
    out = (await api.get("/auth/options")).json()
    assert out == {"demo_email": get_settings().demo_email, "signup_enabled": True, "signup_role": "viewer"}
