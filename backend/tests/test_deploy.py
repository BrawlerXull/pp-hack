"""Run & deploy: production secret checks, AI_ENABLED=false, /healthz + /livez, the LISTEN reconnect and
the attacker terminal (scripts/attack.py)."""

import asyncio
import json

import httpx
import pytest
import respx
from fastapi import Response
from pydantic import ValidationError

from app.ai import analyst, gemini, reporter
from app.api import auth
from app.api.deps import SESSION_COOKIE, get_paypal, require_owner
from app.config import Settings
from app.db.models import User
from app.db.session import get_session
from app.events import Broadcaster
from app.main import app
from app.worker import heartbeat
from scripts import attack
from tests.conftest import SHOP
from tests.test_ai_paths import FEATURES, incident, scripted  # noqa: F401 (fixture)

STRONG = {
    "admin_password": "a-real-judge-password",
    "session_secret": "x" * 40,
    "shop_api_key": "a-real-shop-key",
}

# ---- ENVIRONMENT=production refuses the demo secrets -------------------------------------------------


def test_development_accepts_the_demo_defaults():
    assert Settings(_env_file=None).environment == "development"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("admin_password", "guard-demo", "ADMIN_PASSWORD"),
        ("session_secret", "dev-session-secret-change-me", "SESSION_SECRET"),
        ("session_secret", "change-me-to-a-long-random-string", "SESSION_SECRET"),
        ("session_secret", "short-but-not-default", "SESSION_SECRET"),
        ("shop_api_key", "dev-shop-key", "SHOP_API_KEY"),
    ],
)
def test_production_refuses_default_secrets(field, value, message):
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, environment="production", **(STRONG | {field: value}))


def test_refusal_does_not_echo_other_secrets_into_the_log():
    key = "AIzaFAKE-gemini-key-that-must-not-leak-0123456789"
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None, environment="production", gemini_api_key=key, paypal_client_secret="pp-secret-xyz"
        )
    text = str(exc.value)
    assert "ADMIN_PASSWORD" in text
    assert "0123456789" not in text and "pp-secret" not in text and "input_value" not in text


@pytest.mark.parametrize(("environment", "secure"), [("development", False), ("production", True)])
def test_session_cookie_is_secure_in_production(environment, secure):
    settings = Settings(_env_file=None, environment=environment, **STRONG)
    response = Response()
    auth._start_session(response, settings, User(id="u1", shop_id=SHOP, email="a@b.co", name="A"))
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and ("; Secure" in cookie) is secure


def test_production_starts_with_real_secrets():
    assert Settings(_env_file=None, environment="production", **STRONG).environment == "production"


# ---- AI_ENABLED=false --------------------------------------------------------------------------------


@pytest.fixture
def ai_off(monkeypatch):
    settings = Settings(_env_file=None, ai_enabled=False, gemini_api_key="k")
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    monkeypatch.setattr(analyst, "get_settings", lambda: settings)
    return settings


async def test_ai_disabled_analyst_returns_labelled_rules_fallback_without_a_call(ai_off, monkeypatch):
    prompts = scripted(monkeypatch, "analyst", [])
    res = await analyst.analyse({"x": 1}, FEATURES)
    assert prompts == []
    assert not res.ai_available and res.model_id == "rules-fallback" and res.fallback_reason == "AI disabled"
    assert res.verdict.classification == "unclear" and "AI_ENABLED=false" in res.verdict.uncertainty_notes


async def test_ai_disabled_generate_json_never_builds_a_client(ai_off):
    # conftest's _client stub fails the test if it is reached.
    with pytest.raises(gemini.AIUnavailable) as exc:
        await gemini.generate_json(system="s", prompt="p", schema=analyst.Verdict)
    assert exc.value.reason == "AI disabled"


async def test_ai_disabled_reporter_uses_its_template(ai_off, db, incident, monkeypatch):  # noqa: F811
    prompts = scripted(monkeypatch, "reporter", [])
    _, model_id = await reporter.write(db, incident)
    assert prompts == [] and model_id == "template"


async def test_ai_disabled_shows_as_rules_only_on_the_dashboard(ai_off):
    status = await gemini.status()
    assert status["state"] == "disabled" and "AI disabled" in status["detail"]
    block = heartbeat._ai_block(status, None)
    assert block["mode"] == "rules_only" and "AI disabled" in block["reason"]


# ---- /healthz and /livez -----------------------------------------------------------------------------


class StubPayPal:
    def __init__(self, ok: bool = True):
        self.ok = ok

    async def access_token(self) -> str:
        if not self.ok:
            raise httpx.ConnectError("down")
        return "token"


@pytest.fixture
async def client(db, monkeypatch):
    settings = Settings(_env_file=None, ai_enabled=False)
    monkeypatch.setattr(gemini, "get_settings", lambda: settings)
    paypal = StubPayPal()

    async def session():
        yield db

    app.dependency_overrides.update(
        {get_paypal: lambda: paypal, get_session: session, require_owner: lambda: SHOP}
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.paypal = paypal
        yield c
    app.dependency_overrides.clear()
    if hasattr(app.state, "broadcaster"):
        del app.state.broadcaster


async def test_healthz_ok_with_ai_disabled_and_stream_state(client):
    res = await client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok" and body["ai"] == "disabled" and body["stream"] == "not_started"


async def test_healthz_is_503_when_degraded_but_livez_stays_up(client):
    client.paypal.ok = False
    res = await client.get("/healthz")
    assert res.status_code == 503 and res.json()["status"] == "degraded"
    live = await client.get("/livez")
    assert live.status_code == 200 and live.json() == {"status": "ok"}


async def test_healthz_degraded_while_the_stream_reconnects(client):
    b = Broadcaster()
    b.state = "reconnecting"
    app.state.broadcaster = b
    res = await client.get("/healthz")
    assert res.status_code == 503 and res.json()["stream"] == "reconnecting"


# ---- Broadcaster: LISTEN reconnect with backoff --------------------------------------------------------


class FakeConn:
    def __init__(self):
        self.closed = False
        self.on_terminate = None
        self.listeners = []

    async def add_listener(self, channel, cb):
        self.listeners.append(cb)

    def add_termination_listener(self, cb):
        self.on_terminate = cb

    async def fetchval(self, _q):
        if self.closed:
            raise ConnectionError("closed")
        return 1

    def is_closed(self):
        return self.closed

    def terminate(self):
        self.closed = True

    async def close(self):
        self.closed = True

    def lose(self):
        """What asyncpg does when Postgres drops the connection."""
        self.closed = True
        self.on_terminate(self)


def fake_connect(fail_first: int = 0):
    conns: list[FakeConn] = []
    calls = {"n": 0}

    async def connect(_dsn):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise OSError("connection refused")
        conns.append(FakeConn())
        return conns[-1]

    return connect, conns, calls


def fast(b: Broadcaster) -> Broadcaster:
    b.BACKOFF_FIRST_S, b.BACKOFF_MAX_S, b.PING_EVERY_S = 0.01, 0.02, 0.05
    return b


async def wait_for(cond, limit_s: float = 2.0):
    """Polls: the broadcaster has no event to await for "reconnected", and the fakes are instant."""
    async with asyncio.timeout(limit_s):
        while not cond():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_broadcaster_retries_until_postgres_is_reachable():
    connect, conns, calls = fake_connect(fail_first=3)
    b = fast(Broadcaster(connect))
    await b.start()
    assert b.state == "reconnecting"
    await wait_for(lambda: b.state == "listening")
    assert calls["n"] == 4 and len(conns) == 1 and b.reconnects == 0  # first connection, not a resume
    await b.stop()
    assert b.state == "stopped" and conns[0].closed


async def test_broadcaster_reconnects_after_a_lost_connection_and_tells_subscribers():
    connect, conns, _ = fake_connect()
    b = fast(Broadcaster(connect))
    await b.start()
    assert b.state == "listening"
    async with b.subscribe() as q:
        conns[0].lose()
        assert b.state == "reconnecting"
        await wait_for(lambda: b.state == "listening")
        assert len(conns) == 2 and b.reconnects == 1
        assert json.loads(await asyncio.wait_for(q.get(), 1))["kind"] == "stream.resumed"
        conns[1].listeners[0](None, 0, "cg_events", '{"kind": "attempt"}')  # events flow again
        assert json.loads(await asyncio.wait_for(q.get(), 1))["kind"] == "attempt"
    await b.stop()


async def test_broadcaster_ping_catches_a_silently_dead_socket():
    connect, conns, _ = fake_connect()
    b = fast(Broadcaster(connect))
    await b.start()
    conns[0].closed = True  # no termination callback: the network just went away
    await wait_for(lambda: len(conns) == 2 and b.state == "listening")
    await b.stop()


async def test_broadcaster_does_not_reconnect_after_stop():
    connect, conns, _ = fake_connect()
    b = fast(Broadcaster(connect))
    await b.start()
    await b.stop()
    conns[0].on_terminate(conns[0])
    await asyncio.sleep(0.05)
    assert len(conns) == 1 and b.state == "stopped"


# ---- scripts/attack.py ---------------------------------------------------------------------------------

BASE = "http://guard.test"


def row(i: int, ts: str, outcome: str, scenario: str = "card_testing") -> dict:
    return {
        "id": f"a{i}",
        "ts": ts,
        "scenario": scenario,
        "outcome": outcome,
        "sku": "tealight-mini",
        "amount": "4.00",
        "method": "card",
        "fingerprint": "abcd1234",
        "email_domain": "mailinator.com",
        "decline_code": "CVV2_FAILURE" if outcome == "declined" else None,
        "guard_reason": None,
        "paypal_live": False,
    }


def test_follower_prints_each_attempt_of_this_run_once_in_time_order():
    f = attack.Follower("card_testing", "2026-10-25T10:00:00.500000Z")
    rows = [
        row(3, "2026-10-25T10:00:03Z", "declined"),
        row(1, "2026-10-25T10:00:01Z", "captured"),
        row(2, "2026-10-25T10:00:02Z", "created"),  # outcome not known yet
        row(0, "2026-10-25T09:59:00Z", "declined"),  # an earlier run
        row(9, "2026-10-25T10:00:04Z", "captured", scenario="flash_sale"),
    ]
    assert [a["id"] for a in f.new(rows)] == ["a1", "a3"]
    assert f.new(rows) == []
    assert [a["id"] for a in f.new(rows, final=True)] == ["a2"]  # the run ended: print what is left


def test_attempt_line_is_labelled_simulated_and_shows_the_decline_code():
    line = attack.attempt_line(row(1, "2026-10-25T10:00:01Z", "declined"))
    assert "10:00:01" in line and "DECLINED" in line and "CVV2_FAILURE" in line and "[simulated]" in line


def test_guard_changes_reports_what_the_defender_did():
    before = {"status": "quiet", "guard_state": {"mode": "normal", "blocked_devices": 0, "held_skus": []}}
    after = {
        "status": "under_attack",
        "guard_state": {"mode": "sca_all", "blocked_devices": 4, "held_skus": ["tealight-mini"]},
        "last_24h": {"refunded": 2},
    }
    assert attack.guard_changes(before, after) == [
        "status: ATTACK confirmed",
        "3-D Secure ON for card payments",
        "4 device(s) blocked",
        "held for first-time shoppers: tealight-mini",
        "2 payment(s) refunded (24 h)",
    ]
    assert attack.guard_changes(after, after) == []


def test_attack_cli_logs_in_starts_the_run_and_tails_it(monkeypatch):
    monkeypatch.setattr(attack.time, "sleep", lambda _s: None)
    started = "2026-10-25T10:00:00Z"
    runs = iter(
        [
            [{"run_id": "r1", "started_at": started, "finished_at": None, "done": 1, "total": 2}],
            [{"run_id": "r1", "started_at": started, "finished_at": None, "done": 1, "total": 2}],
            [
                {
                    "run_id": "r1",
                    "started_at": started,
                    "finished_at": "2026-10-25T10:00:05Z",
                    "done": 2,
                    "total": 2,
                    "outcomes": {"declined": 2},
                }
            ],
        ]
    )
    attempts = iter(
        [
            [row(1, "2026-10-25T10:00:01Z", "declined")],
            [row(2, "2026-10-25T10:00:02Z", "declined")],
            [row(2, "2026-10-25T10:00:02Z", "declined")],  # the final pass after the run finished
        ]
    )
    with respx.mock(base_url=BASE, assert_all_called=True) as mock:
        login = mock.post("/auth/login").respond(json={"ok": True, "token": "tok"})
        start = mock.post("/demo/run/card_testing").respond(
            json={
                "run_id": "r1",
                "scenario": "card_testing",
                "mode": "offline",
                "total": 2,
                "simulated": True,
            }
        )
        mock.get("/demo/runs").mock(side_effect=lambda _r: httpx.Response(200, json=next(runs)))
        mock.get("/dashboard/attempts").mock(side_effect=lambda _r: httpx.Response(200, json=next(attempts)))
        quiet = {"status": "quiet", "guard_state": {"mode": "normal"}, "last_24h": {"refunded": 5}}
        hot = {"status": "under_attack", "guard_state": {"mode": "normal"}, "last_24h": {"refunded": 5}}
        summaries = iter([quiet, quiet, hot])
        mock.get("/dashboard/summary").mock(
            side_effect=lambda _r: httpx.Response(200, json=next(summaries, hot))
        )
        result = attack_runner().invoke(
            attack.app, ["card_testing", "--count", "2", "--speed", "3", "--url", BASE, "--password", "pw"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(login.calls[0].request.content) == {"password": "pw"}
    assert json.loads(start.calls[0].request.content) == {
        "mode": "offline",
        "count": 2,
        "speed": 3.0,
        "announce_sale": False,
    }
    assert start.calls[0].request.headers["authorization"] == "Bearer tok"
    out = result.output
    assert "SIMULATION (offline) card_testing" in out and out.count("DECLINED") == 2
    assert "GUARD: status: ATTACK confirmed" in out and "done in" in out
    # Measured against the pre-run summary: earlier rehearsals' refunds and the unchanged 3-D Secure mode
    # are not announced as this run's defence.
    assert "refunded" not in out and "3-D Secure" not in out and out.count("GUARD:") == 1


@pytest.mark.parametrize(
    ("args", "announced"),
    [(["flash_sale"], True), (["flash_sale", "--no-announce-sale"], False), (["card_testing"], False)],
)
def test_attack_cli_announces_the_sale_like_the_war_room_button(args, announced):
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/login").respond(json={"ok": True, "token": "tok"})
        mock.get("/dashboard/summary").respond(json={"status": "quiet"})
        start = mock.post(f"/demo/run/{args[0]}").respond(409, json={"detail": "busy"})
        attack_runner().invoke(attack.app, [*args, "--url", BASE, "--password", "pw"])
    assert json.loads(start.calls[0].request.content)["announce_sale"] is announced


def test_attack_cli_explains_a_run_the_server_forgot(monkeypatch):
    monkeypatch.setattr(attack.time, "sleep", lambda _s: None)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/login").respond(json={"ok": True, "token": "tok"})
        mock.get("/dashboard/summary").respond(json={"status": "quiet"})
        mock.post("/demo/run/card_testing").respond(
            json={"run_id": "r1", "scenario": "card_testing", "mode": "offline", "total": 2}
        )
        mock.get("/demo/runs").respond(json=[])  # the API restarted: in-memory runs are gone
        result = attack_runner().invoke(attack.app, ["--url", BASE, "--password", "pw"])
    assert result.exit_code == 1 and "API restarted?" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_attack_cli_explains_a_refused_run():
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/login").respond(json={"ok": True, "token": "tok"})
        mock.post("/demo/run/card_testing").respond(
            409, json={"detail": {"code": "run_active", "message": "a flash_sale run is still going"}}
        )
        mock.get("/dashboard/summary").respond(json={"status": "quiet"})
        result = attack_runner().invoke(attack.app, ["--url", BASE, "--password", "pw"])
    assert result.exit_code == 1 and "run refused (409)" in result.output


def attack_runner():
    from typer.testing import CliRunner

    return CliRunner()
