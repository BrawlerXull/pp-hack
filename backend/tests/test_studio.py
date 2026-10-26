"""AG Studio (Investigate page): the data tables and the Studio Agent Framework's LLM turns on Gemini.
Gemini is always faked; nothing here leaves the machine."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.ai import gemini, studio_agent
from app.api.deps import get_paypal, require_owner
from app.config import get_settings
from app.db.models import Action, CheckoutAttempt, Incident
from app.db.session import get_session
from app.main import app
from tests.conftest import SHOP


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


@pytest.fixture
def ai_key(monkeypatch):
    """A configured key without real Gemini: run_turn checks the setting before asking for a client."""
    settings = get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_enabled", True)
    return settings


def _part(**kw):
    base = {"text": None, "function_call": None, "thought": None, "thought_signature": None}
    return SimpleNamespace(**(base | kw))


def _reply(*parts, finish="STOP"):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)), finish_reason=finish)],
        response_id="r1",
        model_version="gemini-test",
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=5, total_token_count=15, thoughts_token_count=2
        ),
    )


class FakeClient:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


# ---- translation (pure) ------------------------------------------------------------------------------


def test_conversation_items_become_gemini_turns_with_named_function_responses():
    items = [
        {
            "type": "message",
            "kind": "input",
            "role": "user",
            "content": [{"type": "text", "text": "What happened?"}],
        },
        {
            "type": "function_call",
            "kind": "output",
            "callId": "c1",
            "name": "get_incident",
            "arguments": '{"incident_id": "abc"}',
        },
        {
            "type": "function_call",
            "kind": "output",
            "callId": "c2",
            "name": "get_guard_status",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "callId": "c1",
            "output": '{"classification": "card_testing"}',
            "status": "completed",
        },
        {"type": "function_call_output", "callId": "c2", "output": "quiet", "status": "completed"},
        {
            "type": "function_call",
            "kind": "output",
            "callId": "c3",
            "name": "list_incidents",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "callId": "c3",
            "output": '{"success": true, "response": "[{\\"id\\": \\"a\\"}]"}',
        },
        {
            "type": "message",
            "kind": "output",
            "role": "assistant",
            "content": [{"type": "text", "text": "A card-testing attack."}],
        },
    ]
    contents = studio_agent.to_contents(items)
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user", "model"]
    calls = contents[1].parts
    assert [p.function_call.name for p in calls] == ["get_incident", "get_guard_status"]
    assert calls[0].function_call.args == {"incident_id": "abc"}
    # Only the first call of a model turn carries a signature (the placeholder when we never saw a real one).
    assert calls[0].thought_signature == studio_agent.SKIP_SIGNATURE and calls[1].thought_signature is None
    responses = contents[2].parts
    assert [p.function_response.name for p in responses] == ["get_incident", "get_guard_status"]
    assert responses[0].function_response.response == {"classification": "card_testing"}
    assert responses[1].function_response.response == {"output": "quiet"}
    # Studio's {success, response: "<json>"} wrapper reaches Gemini as an object, not escaped JSON.
    assert contents[4].parts[0].function_response.response == {"success": True, "response": [{"id": "a"}]}


def test_reply_becomes_message_and_tool_call_and_its_signature_is_replayed():
    reply = _reply(
        _part(text="thinking…", thought=True),
        _part(text="Let me look."),
        _part(
            function_call=SimpleNamespace(id="call-9", name="get_incident", args={"incident_id": "x"}),
            thought_signature=b"sig",
        ),
    )
    out = studio_agent.from_response(reply, "gemini-test")
    assert out["status"] == "completed" and out["model"] == "gemini-test"
    message, call = out["output"]
    assert (
        message["type"] == "message" and message["content"][0]["text"] == "Let me look."
    )  # thought part hidden
    assert (
        call["type"] == "function_call"
        and call["callId"] == "call-9"
        and json.loads(call["arguments"]) == {"incident_id": "x"}
    )
    assert out["usage"] == {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15, "reasoningTokens": 2}
    # The next turn sends the call back with Gemini's own signature, not the placeholder.
    replay = studio_agent.to_contents(
        [{"type": "function_call", "callId": "call-9", "name": "get_incident", "arguments": "{}"}]
    )
    assert replay[0].parts[0].thought_signature == b"sig"


def test_truncated_and_empty_replies_are_reported():
    out = studio_agent.from_response(_reply(_part(text="partial"), finish="FinishReason.MAX_TOKENS"), "m")
    assert out["status"] == "incomplete" and out["incompleteDetails"] == {"reason": "max_output_tokens"}
    with pytest.raises(studio_agent.TurnError) as err:
        studio_agent.from_response(_reply(finish="SAFETY"), "m")
    assert err.value.status == 502


def test_schema_cleanup_drops_what_gemini_rejects():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"id": {"anyOf": [{"type": "string"}, {"not": {}}], "description": "incident"}},
    }
    assert studio_agent.clean_schema(schema) == {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "incident"}},
    }


def test_tool_choice_maps_to_gemini_modes():
    assert studio_agent.to_tool_config("auto", True) is None
    assert studio_agent.to_tool_config("required", True).function_calling_config.mode.value == "ANY"
    named = studio_agent.to_tool_config({"name": "get_incident"}, True).function_calling_config
    assert named.mode.value == "ANY" and named.allowed_function_names == ["get_incident"]
    assert studio_agent.to_tool_config("required", False) is None


# ---- POST /studio/agent/turn -------------------------------------------------------------------------


TURN = {
    "input": [
        {"type": "message", "kind": "input", "role": "user", "content": [{"type": "text", "text": "hi"}]}
    ],
    "instructions": "You are the Guard analyst.",
    "tools": [
        {
            "name": "get_incident",
            "description": "Read an incident",
            "parameters": {"type": "object", "properties": {}},
        }
    ],
    "toolChoice": "auto",
    "responseFormat": {"type": "text"},
}


async def test_agent_turn_runs_one_gemini_call_with_tools_and_instructions(client, ai_key, monkeypatch):
    fake = FakeClient(_reply(_part(function_call=SimpleNamespace(id="c1", name="get_incident", args={}))))
    monkeypatch.setattr(gemini, "_client", lambda: fake)
    res = await client.post("/studio/agent/turn", json=TURN)
    assert res.status_code == 200
    assert res.json()["output"][0]["name"] == "get_incident"
    (call,) = fake.calls
    assert call["model"] == gemini.models()[0]
    assert call["config"].system_instruction == "You are the Guard analyst."
    assert call["config"].tools[0].function_declarations[0].name == "get_incident"


async def test_agent_turn_falls_back_to_the_next_model_when_one_is_overloaded(client, ai_key, monkeypatch):
    fake = FakeClient(RuntimeError("503 UNAVAILABLE"), _reply(_part(text="ok")))
    monkeypatch.setattr(gemini, "_client", lambda: fake)
    res = await client.post("/studio/agent/turn", json=TURN)
    assert res.status_code == 200 and res.json()["output"][0]["content"][0]["text"] == "ok"
    assert [c["model"] for c in fake.calls] == gemini.models()[:2]


async def test_agent_turn_without_a_key_answers_503_without_calling_gemini(client):
    res = await client.post("/studio/agent/turn", json=TURN)
    assert res.status_code == 503 and res.json()["error"]["code"] == "ai_not_configured"


async def test_agent_turn_rejects_a_malformed_request(client, ai_key):
    assert (await client.post("/studio/agent/turn", json=[1, 2])).status_code == 400
    assert (await client.post("/studio/agent/turn", json={"input": []})).status_code == 400


async def test_agent_turn_requires_the_owner(db):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.post("/studio/agent/turn", json=TURN)).status_code == 401
        assert (await anon.get("/studio/data")).status_code == 401


# ---- GET /studio/data --------------------------------------------------------------------------------


def _attempt(ts, **kw):
    return CheckoutAttempt(
        shop_id=SHOP,
        ts=ts,
        source=kw.get("source", "simulator"),
        sku="SKU-TEALIGHT-3",
        amount=Decimal("3.00"),
        method="card",
        fingerprint_hash=kw.get("fp", "f" * 64),
        email_domain="x.top",
        guard_decision="allow",
        outcome=kw.get("outcome", "declined"),
    )


async def test_studio_data_links_attempts_to_the_incident_they_fell_in(client, db):
    now = datetime.now(UTC)
    inc = Incident(
        shop_id=SHOP,
        opened_at=now - timedelta(minutes=10),
        closed_at=now - timedelta(minutes=5),
        status="closed",
        classification="card_testing",
        confidence=0.95,
    )
    db.add(inc)
    await db.flush()
    db.add_all(
        [
            _attempt(now - timedelta(minutes=11)),  # the burst that opened it (lead-in)
            _attempt(now - timedelta(minutes=7), outcome="captured"),
            _attempt(now - timedelta(minutes=1)),  # after it closed
            Action(
                incident_id=inc.id,
                type="REFUND",
                policy_rule="R5_refund_matched",
                status="executed",
                params={"amount": "3.00"},
                paypal_ref="RF1",
            ),
            Action(
                incident_id=inc.id, type="SCA_ON", policy_rule="R0_fraud_signal", status="executed", params={}
            ),
        ]
    )
    await db.commit()

    res = await client.get("/studio/data?hours=1")
    assert res.status_code == 200
    data = res.json()
    linked = sorted((a["incident_id"] is not None, a["outcome"]) for a in data["attempts"])
    assert linked == [(False, "declined"), (True, "captured"), (True, "declined")]
    (incident,) = data["incidents"]
    assert (
        incident["refunds"] == 1
        and incident["refunded_total"] == 3.0
        and incident["classification"] == "card_testing"
    )
    assert {a["type"]: a["amount"] for a in data["actions"]} == {"REFUND": 3.0, "SCA_ON": None}
    assert data["ai"]["available"] is False  # no key in tests
