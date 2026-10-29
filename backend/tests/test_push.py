"""Push notifications (app/push.py): sent only after commit, merged per tag, delivered through FCM HTTP v1
to the devices whose member wants that kind, and stale tokens dropped."""

import json

import httpx
import pytest
import respx
from sqlalchemy import select

from app import push
from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import Incident, IncidentStatus, PushDevice, User
from app.db.session import get_session
from app.incidents.service import alert_push
from app.main import app
from tests.conftest import SHOP

FCM = "https://fcm.googleapis.com/v1/projects/cg-test/messages:send"
SERVICE_ACCOUNT = json.dumps(
    {"project_id": "cg-test", "client_email": "push@cg-test.iam.gserviceaccount.com"}
)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(push, "send", lambda **kw: out.append(kw))
    return out


async def test_queued_pushes_wait_for_the_commit_and_merge_by_tag(db, sent):
    for _ in range(3):
        push.queue(
            db, "approval", "Approval needed: {n} payment{s}", "b", "/guard/incidents/i1", "approval-i1"
        )
    push.queue(db, "dispute", "New PayPal dispute", "b", "/guard", "dispute-d1")
    assert sent == []
    await db.commit()
    titles = sorted(p["title"] for p in sent)
    assert titles == ["Approval needed: 3 payments", "New PayPal dispute"]


async def test_a_rollback_drops_the_queued_pushes(db, sent):
    await db.execute(select(User.id))  # inside a transaction, like every caller
    push.queue(db, "approval", "Approval needed: {n} payment{s}", "b", "/u", "approval-i1")
    await db.rollback()
    await db.commit()
    assert sent == []


def test_the_alert_push_says_attack_while_open_and_report_after():
    incident = Incident(id="i1", shop_id=SHOP, status=IncidentStatus.OPEN, classification="card_testing")
    kind, title, body, url, _ = alert_push(incident, "Attack stopped.")
    assert (kind, title, body, url) == (
        "incident",
        "Card-testing attack",
        "Attack stopped.",
        "/guard/incidents/i1",
    )
    incident.status = IncidentStatus.CLOSED
    assert alert_push(incident, "x")[0] == "report"


async def members(db) -> tuple[User, User]:
    a = User(shop_id=SHOP, email="a@x.co", name="A")
    b = User(shop_id=SHOP, email="b@x.co", name="B", push_kinds=["dispute"])
    db.add_all([a, b])
    await db.flush()
    db.add_all(
        [
            PushDevice(user_id=a.id, token="token-a-" + "x" * 20),
            PushDevice(user_id=a.id, token="token-gone-" + "x" * 20),
            PushDevice(user_id=b.id, token="token-b-" + "x" * 20),
        ]
    )
    await db.commit()
    return a, b


async def test_delivery_respects_each_members_kinds_and_drops_dead_tokens(db, monkeypatch):
    await members(db)
    monkeypatch.setattr(get_settings(), "firebase_service_account", SERVICE_ACCOUNT)

    async def token(_info):
        return "access"

    monkeypatch.setattr(push, "_access_token", token)
    with respx.mock(assert_all_called=False) as mock:

        def reply(request: httpx.Request) -> httpx.Response:
            msg = json.loads(request.content)["message"]
            assert request.headers["authorization"] == "Bearer access"
            assert (
                msg["data"]["title"] == "Card-testing attack"
                and msg["webpush"]["headers"]["Urgency"] == "high"
            )
            if msg["token"].startswith("token-gone"):
                return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
            return httpx.Response(200, json={"name": "m1"})

        route = mock.post(FCM).mock(side_effect=reply)
        out = await push.deliver("incident", "Card-testing attack", "b", "/guard", "incident-i1")
    assert out == {"sent": 1, "failed": 1}
    assert {json.loads(c.request.content)["message"]["token"][:7] for c in route.calls} == {
        "token-a",
        "token-g",
    }
    left = set((await db.execute(select(PushDevice.token))).scalars())
    assert not any(t.startswith("token-gone") for t in left) and len(left) == 2


async def test_without_a_service_account_nothing_is_sent(db):
    await members(db)
    assert await push.deliver("incident", "t", "b", "/", "x") == {"sent": 0, "failed": 0}


@pytest.fixture
async def api(db):
    async def session():
        yield db

    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_a_browser_token_follows_whoever_signs_in_on_it(api, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "firebase_web_config", '{"apiKey": "k", "projectId": "cg-test"}')
    monkeypatch.setattr(get_settings(), "firebase_vapid_key", "vapid")
    monkeypatch.setattr(get_settings(), "firebase_service_account", SERVICE_ACCOUNT)
    token = {"token": "fcm-" + "t" * 40, "label": "Chrome on Mac"}
    await api.post("/auth/signup", json={"name": "A", "email": "a@x.co", "password": "password-a"})
    cfg = (await api.get("/push/config")).json()
    assert cfg["enabled"] and cfg["firebase"]["projectId"] == "cg-test" and cfg["vapid_key"] == "vapid"
    assert (await api.post("/push/devices", json=token)).status_code == 200
    api.cookies.clear()
    await api.post("/auth/signup", json={"name": "B", "email": "b@x.co", "password": "password-b"})
    await api.post("/push/devices", json=token)
    rows = (await db.execute(select(PushDevice.user_id, User.email).join(User))).all()
    assert [r.email for r in rows] == ["b@x.co"]
    assert (await api.get("/push/config")).json()["devices"][0]["label"] == "Chrome on Mac"


async def test_a_member_picks_their_kinds(api):
    await api.post("/auth/signup", json={"name": "A", "email": "a@x.co", "password": "password-a"})
    assert (await api.put("/push/kinds", json={"kinds": ["dispute", "incident"]})).json() == {
        "kinds": ["incident", "dispute"]
    }
    assert (await api.put("/push/kinds", json={"kinds": ["spam"]})).status_code == 422
