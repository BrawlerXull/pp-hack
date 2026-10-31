"""Ask Guard (app/ai/assistant.py): look-ups run on the member's shop, changes come back as proposals that
name a real refund or incident with our amounts, and nothing changes until the dashboard sends the request.
Gemini is replaced by a scripted model: each test says which tool calls it makes."""

from types import SimpleNamespace

import httpx
import pytest
from google.genai import types
from sqlalchemy import select

from app.ai import assistant
from app.api.deps import get_paypal
from app.config import get_settings
from app.db.models import Action, ActionStatus, ActionType, Incident, MerchantSettings
from app.db.session import get_session
from app.main import app
from tests.conftest import SHOP


@pytest.fixture
async def api(db, monkeypatch):
    async def session():
        yield db

    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "ai_enabled", True)
    app.dependency_overrides.update({get_session: session, get_paypal: lambda: None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (
            await c.post("/auth/login", json={"password": get_settings().admin_password})
        ).status_code == 200
        yield c
    app.dependency_overrides.clear()


def reply(*parts: types.Part) -> SimpleNamespace:
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=types.Content(role="model", parts=list(parts)))]
    )


def call(name: str, **args) -> types.Part:
    return types.Part(function_call=types.FunctionCall(id=f"c-{name}", name=name, args=args))


def script(monkeypatch, *steps: list[types.Part]) -> list[list[types.Content]]:
    """The model answers with these steps in order; returns what it was sent each time."""
    seen: list[list[types.Content]] = []
    queue = list(steps)

    async def fake(settings, contents, config, sticky):
        seen.append(list(contents))
        sticky[:] = ["fake-model"]
        return reply(*queue.pop(0))

    monkeypatch.setattr(assistant, "_generate", fake)
    return seen


def tool_results(contents: list[types.Content]) -> list[dict]:
    return [p.function_response.response for c in contents for p in c.parts or [] if p.function_response]


async def pending_refund(db) -> Action:
    inc = Incident(shop_id=SHOP, classification="card_testing", confidence=0.95)
    db.add(inc)
    await db.flush()
    action = Action(
        incident_id=inc.id,
        type=ActionType.REFUND,
        params={"attempt_id": "a1", "amount": "3.00", "match": "weak"},
        policy_rule="R5_weak_match_needs_owner",
        status=ActionStatus.AWAITING_APPROVAL,
    )
    db.add(action)
    await db.commit()
    return action


async def test_a_question_is_answered_from_a_look_up(api, monkeypatch):
    seen = script(monkeypatch, [call("get_overview")], [types.Part(text="All quiet tonight.")])
    res = await api.post(
        "/assistant/chat", json={"messages": [{"role": "user", "text": "How are we doing?"}]}
    )
    out = res.json()
    assert res.status_code == 200 and out["reply"] == "All quiet tonight." and out["proposals"] == []
    assert out["looked_up"] == ["get_overview"]
    overview = tool_results(seen[1])[0]["result"]
    assert overview["status"] == "quiet" and overview["payments_waiting_for_approval"] == 0


async def test_approving_a_refund_is_a_proposal_with_our_amount_and_the_real_endpoint(api, db, monkeypatch):
    action = await pending_refund(db)
    script(
        monkeypatch,
        [call("list_pending_approvals")],
        [call("propose_approve_payment", action_id=action.id)],
        [types.Part(text="I've put the $3.00 refund up for you to confirm.")],
    )
    out = (
        await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "Refund it"}]})
    ).json()
    [p] = out["proposals"]
    assert p["title"] == "Refund $3.00 to the card" and p["danger"] and p["owner_only"]
    assert p["request"] == {
        "method": "POST",
        "path": f"/dashboard/incidents/{action.incident_id}/actions/{action.id}/approve",
        "body": None,
    }
    # Nothing happened: the refund still waits for the owner.
    assert (await db.get(Action, action.id, populate_existing=True)).status == ActionStatus.AWAITING_APPROVAL


async def test_a_made_up_id_or_a_pointless_change_is_refused_to_the_model(api, monkeypatch):
    seen = script(
        monkeypatch,
        [call("propose_approve_payment", action_id="nope"), call("propose_set_autopilot", on=True)],
        [types.Part(text="Nothing to approve.")],
    )
    out = (await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "go"}]})).json()
    assert out["proposals"] == []
    errors = [r["error"] for r in tool_results(seen[1])]
    assert "no such action" in errors[0] and "already on" in errors[1]


async def test_the_same_change_proposed_twice_shows_one_card(api, monkeypatch):
    script(
        monkeypatch,
        [call("propose_set_kill_switch", on=True)],
        [call("propose_set_kill_switch", on=True)],
        [types.Part(text="Up for you to confirm.")],
    )
    out = (await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "pause"}]})).json()
    assert [p["request"]["body"] for p in out["proposals"]] == [{"kill_switch": True}]


async def test_the_confirmed_request_goes_through_the_normal_endpoint(api, db, monkeypatch):
    """What the chat card sends on Confirm, end to end: the switch flips, like the toggle would."""
    script(monkeypatch, [call("propose_set_kill_switch", on=True)], [types.Part(text="Confirm to pause.")])
    out = (await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "pause"}]})).json()
    req = out["proposals"][0]["request"]
    assert (await api.request(req["method"], req["path"], json=req["body"])).status_code == 200
    ms = (await db.execute(select(MerchantSettings).execution_options(populate_existing=True))).scalar_one()
    assert ms.kill_switch is True


async def test_a_viewer_is_told_an_owner_must_confirm(api, monkeypatch):
    api.cookies.clear()
    await api.post("/auth/signup", json={"name": "Vi", "email": "vi@example.com", "password": "viewer-pass"})
    captured = {}

    async def fake(settings, contents, config, sticky):
        captured["system"] = config.system_instruction
        return reply(types.Part(text="Hi"))

    monkeypatch.setattr(assistant, "_generate", fake)
    assert (
        await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "hi"}]})
    ).status_code == 200
    assert "Vi (viewer)" in captured["system"] and "only an owner can confirm" in captured["system"]


async def test_history_must_end_with_the_member_and_ai_off_is_explained(api, monkeypatch):
    bad = {"messages": [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]}
    assert (await api.post("/assistant/chat", json=bad)).status_code == 422
    monkeypatch.setattr(get_settings(), "ai_enabled", False)
    res = await api.post("/assistant/chat", json={"messages": [{"role": "user", "text": "hi"}]})
    assert res.status_code == 503 and "AI_ENABLED" in res.json()["detail"]


def test_dashboard_updates_go_in_as_bracketed_member_text_and_turns_alternate():
    contents = assistant.to_contents(
        [
            {"role": "user", "text": "pause it"},
            {"role": "assistant", "text": "Confirm to pause."},
            {"role": "note", "text": "Confirmed: Pause the guard"},
            {"role": "user", "text": "thanks"},
        ]
    )
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [p.text for p in contents[2].parts] == ["[Confirmed: Pause the guard]", "thanks"]
