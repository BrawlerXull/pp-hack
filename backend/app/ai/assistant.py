"""The dashboard assistant ("Ask Guard"): a Gemini chat with Checkout Guard tools.

Two kinds of tool:
  * look-ups (overview, incidents, approvals, disputes, devices, settings, recent checkouts) run here, on the
    member's own shop, and their results go back to the model;
  * changes (approve or reject a refund, close an incident, the switches, unblock a device, answer a dispute,
    run a simulation) never run here. Each becomes a proposal: a card in the chat with the exact request
    (method, path, body) the dashboard sends if the member presses Confirm. The request goes through the
    same endpoint as the button it replaces, so the role check (owners only) and every safety rule apply.

The model only ever picks among these tools and their ids are checked against the database before a card is
shown, so a proposal always names a real refund, incident or dispute, with the amount from our records.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from google.genai import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gemini
from app.config import Settings
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    DisputeCase,
    Incident,
    IncidentStatus,
    MerchantSettings,
    Role,
    User,
)

log = structlog.get_logger()

MAX_ROUNDS = 6  # model ↔ tool round trips in one answer
# A chat answer is waited for: a model that doesn't answer a step in this long is skipped for SLOW_FOR, so the
# next questions go straight to one that does.
TURN_TIMEOUT_S = 12.0
SLOW_FOR = timedelta(minutes=5)
_slow: dict[str, datetime] = {}
MAX_OUTPUT_TOKENS = 2048

SCENARIOS = {
    "card_testing": "a bot trying stolen cards on the cheapest item",
    "account_testing": "a bot trying stolen PayPal logins",
    "slow_drip": "card testing spread out slowly to stay under the radar",
    "flash_sale": "a genuine rush of real shoppers (the guard should stay calm)",
    "paypal_outage": "PayPal itself failing (simulated errors)",
    "normal_night": "an ordinary quiet night",
    "mixed_traffic": "real shoppers with a bot hidden among them",
    "stealth_bot": "a bot that rotates devices and networks",
}

SYSTEM = """You are Ask Guard, the assistant inside Checkout Guard's dashboard for {shop}, a small \
online shop that takes PayPal payments. Checkout Guard watches the checkout, recognises card-testing \
attacks (bots trying stolen cards or PayPal logins), and responds within the owner's limits: 3-D Secure, \
blocking devices, holding payments, refunding the attack's test payments, and helping answer PayPal disputes.

You are talking to {name} ({role}). It is {now} (UTC). {page}

How to work:
- Every fact (numbers, amounts, statuses, times) must come from a tool result in this conversation. Never \
guess. If the tools don't say, say you don't know.
- Look things up before answering questions about what is happening. Prefer one good look-up over many.
- To change anything, call the matching propose_ tool. That only shows {name} a card with Confirm and \
Cancel right under your answer in this chat: nothing has happened yet. Say it's ready to confirm below; \
never say it's done. Updates in brackets from the dashboard tell you what they confirmed or cancelled.
- Don't propose a change the facts show is pointless (a switch already in that position, a refund already \
decided). One proposal per change; several are fine when asked for several.
- {role_rule}
- Recommend when asked what to do, with the reason from the data, and offer the matching proposal.
- Simulations are labelled tests on the shop's demo; propose one only when asked.
- Write plain, short English for a shop owner: a few sentences or a short list. Money as $3.00. Times as \
"10 minutes ago" or "at 02:14 UTC". Use ids only when they are needed to tell things apart."""

OWNER_RULE = "They are an owner: they can confirm any proposal."
VIEWER_RULE = (
    "They are a viewer: they can look at everything and run simulations, but only an owner can confirm "
    "other changes. You may still propose them; say an owner has to confirm."
)


class AssistantError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Proposal:
    title: str
    detail: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    danger: bool = False
    owner_only: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def out(self, tool: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": tool,
            "title": self.title,
            "detail": self.detail,
            "danger": self.danger,
            "owner_only": self.owner_only,
            "request": {"method": self.method, "path": self.path, "body": self.body},
        }


@dataclass
class Ctx:
    session: AsyncSession
    shop_id: str
    user: User
    settings: Settings
    page: str | None = None
    proposals: list[dict[str, Any]] = field(default_factory=list)
    looked_up: list[str] = field(default_factory=list)


class Refused(Exception):
    """A proposal that can't be made (wrong id, already decided): told to the model, not the member."""


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "an unknown amount"


# ---- look-ups -----------------------------------------------------------------------------------------


async def get_overview(ctx: Ctx, _: dict[str, Any]) -> dict[str, Any]:
    from app.api import dashboard

    s = await dashboard.summary(ctx.shop_id, ctx.session, ctx.settings)
    pending = await _pending(ctx)
    waiting = (
        await ctx.session.execute(
            select(DisputeCase.id).where(DisputeCase.shop_id == ctx.shop_id, DisputeCase.status == "open")
        )
    ).all()
    inc = s["open_incident"]
    return {
        "status": s["status"],
        "open_incident": {
            k: inc.get(k) for k in ("id", "opened_at", "classification", "confidence", "summary")
        }
        if inc
        else None,
        "switches": s["switches"],
        "sale_window": s["sale_window"],
        "defences": s["guard_state"],
        "last_24h": s["last_24h"],
        "ai": s["ai"]["state"],
        "payments_waiting_for_approval": len(pending),
        "disputes_waiting_for_answer": len(waiting),
    }


async def list_incidents(ctx: Ctx, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = max(1, min(int(args.get("limit") or 5), 20))
    rows = (
        await ctx.session.execute(
            select(Incident)
            .where(Incident.shop_id == ctx.shop_id)
            .order_by(Incident.opened_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "id": i.id,
            "status": i.status,
            "opened_at": i.opened_at,
            "closed_at": i.closed_at,
            "classification": i.classification,
            "confidence": i.confidence,
            "summary": (i.verdict or {}).get("summary"),
        }
        for i in rows
    ]


async def get_incident(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    from app.api import dashboard

    d = await dashboard.incident_detail(str(args.get("incident_id") or ""), ctx.shop_id, ctx.session)
    actions = [
        {
            "id": a["id"],
            "type": a["type"],
            "status": a["status"],
            "amount": (a["params"] or {}).get("amount"),
            "rule": a["rule"],
        }
        for a in d["actions"]
    ]
    return {
        "id": d["id"],
        "status": d["status"],
        "opened_at": d["opened_at"],
        "closed_at": d["closed_at"],
        "classification": d["classification"],
        "confidence": d["confidence"],
        "summary": d["summary"],
        "detected_in_s": d["detected_in_s"],
        "evidence_vs_normal": d["evidence_vs_normal"],
        "actions": actions,
        "alert_text": (d["report"] or {}).get("alert_text"),
    }


async def _pending(ctx: Ctx) -> list[tuple[Action, Incident]]:
    return [
        (a, inc)
        for a, inc in (
            await ctx.session.execute(
                select(Action, Incident)
                .join(Incident, Action.incident_id == Incident.id)
                .where(Incident.shop_id == ctx.shop_id, Action.status == ActionStatus.AWAITING_APPROVAL)
                .order_by(Action.created_at)
                .limit(50)
            )
        ).all()
    ]


async def list_pending_approvals(ctx: Ctx, _: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "action_id": a.id,
            "incident_id": inc.id,
            "kind": "refund" if a.type == ActionType.REFUND else "release held payment",
            "amount": (a.params or {}).get("amount"),
            "match": (a.params or {}).get("match"),
            "why_waiting": a.policy_rule,
            "created_at": a.created_at,
        }
        for a, inc in await _pending(ctx)
    ]


async def list_disputes(ctx: Ctx, _: dict[str, Any]) -> list[dict[str, Any]]:
    from app.api import dashboard

    return [
        {
            k: c.get(k)
            for k in (
                "id",
                "reason",
                "amount",
                "status",
                "recommendation",
                "response_due",
                "buyer_says",
                "note",
                "paypal_live",
            )
        }
        for c in (await dashboard.disputes(ctx.shop_id, ctx.session))[:15]
    ]


async def list_devices(ctx: Ctx, _: dict[str, Any]) -> dict[str, Any]:
    from app.api import dashboard

    d = await dashboard.list_devices(ctx.shop_id, ctx.session)
    return {
        "blocked": d["blocked"][:25],
        "blocked_total": len(d["blocked"]),
        "trusted_total": len(d["trusted"]),
    }


async def get_settings_(ctx: Ctx, _: dict[str, Any]) -> dict[str, Any]:
    from app.api.settings import _out

    ms = await ctx.session.get(MerchantSettings, ctx.shop_id)
    assert ms is not None
    return _out(ms).model_dump(mode="json")


async def list_recent_checkouts(ctx: Ctx, args: dict[str, Any]) -> list[dict[str, Any]]:
    limit = max(1, min(int(args.get("limit") or 10), 30))
    rows = (
        await ctx.session.execute(
            select(CheckoutAttempt)
            .where(CheckoutAttempt.shop_id == ctx.shop_id, CheckoutAttempt.source != "seed")
            .order_by(CheckoutAttempt.ts.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "ts": a.ts,
            "sku": a.sku,
            "amount": str(a.amount),
            "method": a.method,
            "guard_decision": a.guard_decision,
            "outcome": a.outcome,
            "source": a.source,
            "email_domain": a.email_domain,
        }
        for a in rows
    ]


# ---- proposals ----------------------------------------------------------------------------------------


async def _waiting_action(ctx: Ctx, action_id: str) -> tuple[Action, Incident]:
    action = await ctx.session.get(Action, action_id)
    inc = await ctx.session.get(Incident, action.incident_id) if action else None
    if action is None or inc is None or inc.shop_id != ctx.shop_id:
        raise Refused("no such action: use an action_id from list_pending_approvals")
    if action.status != ActionStatus.AWAITING_APPROVAL:
        raise Refused(f"that action is already {action.status}")
    return action, inc


async def propose_approve_payment(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    action, inc = await _waiting_action(ctx, str(args.get("action_id") or ""))
    amount = _money((action.params or {}).get("amount"))
    void = action.type == ActionType.VOID
    return Proposal(
        title=f"Release the held {amount} payment" if void else f"Refund {amount} to the card",
        detail="The payment was held and is never charged."
        if void
        else "Refunded through PayPal, so it can't turn into a dispute. This can't be undone.",
        method="POST",
        path=f"/dashboard/incidents/{inc.id}/actions/{action.id}/approve",
        danger=not void,
    )


async def propose_reject_payment(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    action, inc = await _waiting_action(ctx, str(args.get("action_id") or ""))
    amount = _money((action.params or {}).get("amount"))
    return Proposal(
        title=f"Keep the {amount} payment",
        detail="Checkout Guard won't refund it. Do this when you know the buyer is genuine.",
        method="POST",
        path=f"/dashboard/incidents/{inc.id}/actions/{action.id}/reject",
    )


async def propose_close_incident(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    inc = await ctx.session.get(Incident, str(args.get("incident_id") or ""))
    if inc is None or inc.shop_id != ctx.shop_id:
        raise Refused("no such incident: use an id from get_overview or list_incidents")
    if inc.status == IncidentStatus.CLOSED:
        raise Refused("that incident is already closed")
    return Proposal(
        title="Close the incident and stand the guard down",
        detail="Lifts this incident's defences (3-D Secure for everyone, held items) and writes the final "
        "report. Blocked devices stay blocked.",
        method="POST",
        path=f"/dashboard/incidents/{inc.id}/close",
    )


async def _switch(ctx: Ctx, name: str, on: bool) -> MerchantSettings:
    ms = await ctx.session.get(MerchantSettings, ctx.shop_id)
    assert ms is not None
    if bool(getattr(ms, name)) == on:
        raise Refused(f"{name} is already {'on' if on else 'off'}")
    return ms


async def propose_set_kill_switch(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    on = bool(args.get("on"))
    await _switch(ctx, "kill_switch", on)
    return Proposal(
        title="Pause the guard (kill switch on)" if on else "Switch the guard back on (kill switch off)",
        detail="Checkouts go straight to PayPal and the guard only notifies you."
        if on
        else "The guard checks every checkout again.",
        method="PATCH",
        path="/settings/switches",
        body={"kill_switch": on},
        danger=on,
    )


async def propose_set_autopilot(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    on = bool(args.get("on"))
    await _switch(ctx, "autopilot", on)
    return Proposal(
        title="Turn autopilot refunds on" if on else "Turn autopilot refunds off",
        detail="Confident attack verdicts refund matched test payments on their own, within your limits."
        if on
        else "Every refund waits for your approval.",
        method="PATCH",
        path="/settings/switches",
        body={"autopilot": on},
    )


async def propose_unblock_device(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    from app.api import dashboard

    fp = str(args.get("fingerprint") or "")
    blocked = {d["fingerprint"] for d in (await dashboard.list_devices(ctx.shop_id, ctx.session))["blocked"]}
    if fp not in blocked:
        raise Refused("that device is not blocked: use a fingerprint from list_devices")
    return Proposal(
        title=f"Unblock and trust device {fp[:8]}…",
        detail="It can check out again, and Checkout Guard won't block it or refund its payments later.",
        method="POST",
        path=f"/dashboard/devices/{fp}/unblock",
    )


async def propose_reset_defences(ctx: Ctx, _: dict[str, Any]) -> Proposal:
    return Proposal(
        title="Clear all defences",
        detail="Unblocks every blocked device, releases held items and turns 3-D Secure for everyone off. "
        "Trusted devices stay trusted.",
        method="POST",
        path="/dashboard/guard/reset",
        danger=True,
    )


async def propose_answer_dispute(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    case = await ctx.session.get(DisputeCase, str(args.get("dispute_id") or ""))
    if case is None or case.shop_id != ctx.shop_id:
        raise Refused("no such dispute: use an id from list_disputes")
    if case.status not in {"open", "failed"}:
        raise Refused(f"that dispute is already {case.status}")
    decision = args.get("decision")
    if decision not in {"accept", "contest"}:
        raise Refused("decision must be accept or contest")
    note = str(args.get("note") or case.note or "").strip()[:2000]
    amount = _money(case.amount)
    return Proposal(
        title=f"Contest the {amount} dispute" if decision == "contest" else f"Accept the {amount} dispute",
        detail=(f"Sent to PayPal with this note: “{note}”" if note else "Sent to PayPal.")
        + ("" if case.paypal_live else " (Simulated dispute: nothing reaches PayPal.)"),
        method="POST",
        path=f"/dashboard/disputes/{case.id}/{decision}",
        body={"note": note} if note else None,
        danger=decision == "accept",
    )


async def propose_run_simulation(ctx: Ctx, args: dict[str, Any]) -> Proposal:
    if not ctx.settings.simulator_enabled:
        raise Refused("simulations are switched off on this server")
    scenario = str(args.get("scenario") or "")
    if scenario not in SCENARIOS:
        raise Refused(f"unknown scenario; one of {', '.join(SCENARIOS)}")
    return Proposal(
        title=f"Run the {scenario.replace('_', ' ')} simulation",
        detail=f"A labelled test: {SCENARIOS[scenario]}. Offline, so no PayPal calls are made.",
        method="POST",
        path=f"/demo/run/{scenario}",
        body={"mode": "offline", "speed": 3},
        owner_only=False,
    )


# ---- tool table ---------------------------------------------------------------------------------------

Handler = Callable[[Ctx, dict[str, Any]], Awaitable[Any]]

_ID = {"type": "string"}
_NONE: dict[str, Any] = {"type": "object", "properties": {}}

TOOLS: dict[str, tuple[str, dict[str, Any], Handler]] = {
    "get_overview": (
        "The shop right now: status, the open incident, switches, defences in force, the last 24 hours in "
        "numbers, and how many refunds and disputes are waiting for the owner.",
        _NONE,
        get_overview,
    ),
    "list_incidents": (
        "The latest incidents (attacks, rushes, outages), newest first.",
        {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        list_incidents,
    ),
    "get_incident": (
        "One incident in detail: verdict, evidence against normal, every action taken, the alert text.",
        {"type": "object", "properties": {"incident_id": _ID}, "required": ["incident_id"]},
        get_incident,
    ),
    "list_pending_approvals": (
        "Refunds and held payments waiting for the owner's decision, with amounts.",
        _NONE,
        list_pending_approvals,
    ),
    "list_disputes": (
        "PayPal disputes (chargebacks): waiting ones first, with Checkout Guard's recommendation and drafted "
        "note.",
        _NONE,
        list_disputes,
    ),
    "list_devices": ("Devices blocked now, with why and until when.", _NONE, list_devices),
    "get_settings": (
        "The owner's settings: refund limits, payment hold, de-escalation, sale windows, switches.",
        _NONE,
        get_settings_,
    ),
    "list_recent_checkouts": (
        "The latest checkout attempts with the guard's decision and the outcome.",
        {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30}}},
        list_recent_checkouts,
    ),
    "propose_approve_payment": (
        "Propose approving a refund (or the release of a held payment) that waits for the owner.",
        {"type": "object", "properties": {"action_id": _ID}, "required": ["action_id"]},
        propose_approve_payment,
    ),
    "propose_reject_payment": (
        "Propose keeping a payment instead of refunding it (the buyer is genuine).",
        {"type": "object", "properties": {"action_id": _ID}, "required": ["action_id"]},
        propose_reject_payment,
    ),
    "propose_close_incident": (
        "Propose closing an open incident and standing the guard down.",
        {"type": "object", "properties": {"incident_id": _ID}, "required": ["incident_id"]},
        propose_close_incident,
    ),
    "propose_set_kill_switch": (
        "Propose pausing the guard (on: checkouts go straight to PayPal) or switching it back on (off).",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        propose_set_kill_switch,
    ),
    "propose_set_autopilot": (
        "Propose turning autopilot refunds on or off.",
        {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
        propose_set_autopilot,
    ),
    "propose_unblock_device": (
        "Propose unblocking and trusting a blocked device (a genuine customer caught by mistake).",
        {"type": "object", "properties": {"fingerprint": _ID}, "required": ["fingerprint"]},
        propose_unblock_device,
    ),
    "propose_reset_defences": (
        "Propose clearing every defence at once (blocked devices, held items, 3-D Secure for everyone).",
        _NONE,
        propose_reset_defences,
    ),
    "propose_answer_dispute": (
        "Propose answering a PayPal dispute: contest (with a note for PayPal) or accept.",
        {
            "type": "object",
            "properties": {
                "dispute_id": _ID,
                "decision": {"type": "string", "enum": ["accept", "contest"]},
                "note": {"type": "string", "description": "For PayPal; leave out to use the drafted note"},
            },
            "required": ["dispute_id", "decision"],
        },
        propose_answer_dispute,
    ),
    "propose_run_simulation": (
        "Propose running a labelled simulation on the demo shop. Scenarios: "
        + "; ".join(f"{k}: {v}" for k, v in SCENARIOS.items()),
        {
            "type": "object",
            "properties": {"scenario": {"type": "string", "enum": list(SCENARIOS)}},
            "required": ["scenario"],
        },
        propose_run_simulation,
    ),
}


def declarations() -> list[types.Tool | Any]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name=name, description=desc, parameters_json_schema=schema)
                for name, (desc, schema, _) in TOOLS.items()
            ]
        )
    ]


async def run_tool(ctx: Ctx, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One tool call → the JSON the model sees. Errors are reported to the model, which can try again."""
    entry = TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool {name}"}
    try:
        result = await entry[2](ctx, args)
    except Refused as exc:
        return {"error": str(exc)}
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    if isinstance(result, Proposal):
        request = (result.method, result.path, json.dumps(result.body, sort_keys=True))
        same = next(
            (
                p
                for p in ctx.proposals
                if (
                    p["request"]["method"],
                    p["request"]["path"],
                    json.dumps(p["request"]["body"], sort_keys=True),
                )
                == request
            ),
            None,
        )
        if same is None:
            ctx.proposals.append(result.out(name))
        return {"proposal": "shown to the member with Confirm and Cancel; nothing has happened yet"}
    ctx.looked_up.append(name)
    return {"result": jsonable_encoder(result)}


# ---- the chat loop ------------------------------------------------------------------------------------


def to_contents(messages: list[dict[str, str]]) -> list[types.Content]:
    """Chat history → Gemini contents. Dashboard updates ("note") go in as bracketed user text; consecutive
    turns of one side are merged (Gemini wants them alternating)."""
    out: list[types.Content] = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        text = f"[{m['text']}]" if m["role"] == "note" else m["text"]
        if out and out[-1].role == role:
            out[-1].parts = [*(out[-1].parts or []), types.Part(text=text)]
        else:
            out.append(types.Content(role=role, parts=[types.Part(text=text)]))
    return out


def _system(ctx: Ctx) -> str:
    return SYSTEM.format(
        shop="Wick & Wax",
        name=ctx.user.name or "the owner",
        role=ctx.user.role,
        now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        role_rule=OWNER_RULE if ctx.user.role == Role.OWNER else VIEWER_RULE,
        page=f"They have this dashboard page open: {ctx.page} (an id in it is what they mean by 'this')."
        if ctx.page
        else "",
    )


def _retryable(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, TimeoutError) or any(
        code in text for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
    )


async def _generate(
    settings: Settings, contents: list[types.Content], config: types.GenerateContentConfig, sticky: list[str]
) -> Any:
    """One model call along the fallback chain (both keys). `sticky` remembers the model that answered, so
    the rest of this answer stays on it (its thought signatures belong to it)."""
    now = datetime.now(UTC)
    chain = sorted(gemini.models(settings), key=lambda m: _slow.get(m, now) > now)  # stable: slow ones last
    order = [*sticky, *[m for m in chain if m not in sticky]]
    last: Exception | None = None
    for key, _ in gemini.lanes(settings):
        client = gemini.client_for(key)
        for model in order:
            try:
                cfg = config.model_copy(update={"thinking_config": gemini._thinking(model)})
                res = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents, config=cfg),
                    timeout=TURN_TIMEOUT_S,
                )
                sticky[:] = [model]
                return res
            except Exception as exc:
                last = exc
                if isinstance(exc, TimeoutError):
                    _slow[model] = datetime.now(UTC) + SLOW_FOR
                log.warning(
                    "assistant_turn_failed", model=model, error=f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                if not _retryable(exc):
                    break
    raise AssistantError(502, "Ask Guard couldn't reach Gemini just now. Try again in a minute.") from last


async def chat(ctx: Ctx, messages: list[dict[str, str]]) -> dict[str, Any]:
    settings = ctx.settings
    if not settings.ai_enabled:
        raise AssistantError(503, "The AI is switched off on this server (AI_ENABLED=false).")
    if not settings.gemini_api_key:
        raise AssistantError(503, "Ask Guard needs GEMINI_API_KEY on the server.")
    contents = to_contents(messages)
    config = types.GenerateContentConfig(
        system_instruction=_system(ctx),
        tools=declarations(),
        temperature=0.2,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    sticky: list[str] = []
    said: list[str] = []  # text the model wrote alongside its tool calls belongs to the answer too
    for round_ in range(MAX_ROUNDS + 1):
        if round_ == MAX_ROUNDS:  # enough look-ups: answer with what is known
            config = config.model_copy(
                update={
                    "tool_config": types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.NONE
                        )
                    )
                }
            )
        res = await _generate(settings, contents, config, sticky)
        candidate = res.candidates[0] if getattr(res, "candidates", None) else None
        content = candidate.content if candidate else None
        parts = (content.parts if content else None) or []
        calls = [p.function_call for p in parts if p.function_call]
        if text := "".join(p.text or "" for p in parts if p.text and not p.thought).strip():
            said.append(text)
        if not calls:
            return {
                "reply": "\n\n".join(said) or "Sorry, I didn't get an answer. Try asking again.",
                "proposals": ctx.proposals,
                "looked_up": list(dict.fromkeys(ctx.looked_up)),
                "model": sticky[0] if sticky else None,
            }
        assert content is not None
        contents.append(content)  # as returned: keeps the thought signatures Gemini needs back
        replies = []
        for call in calls:
            out = await run_tool(ctx, call.name or "", dict(call.args or {}))
            replies.append(
                types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=out))
            )
        contents.append(types.Content(role="user", parts=replies))
    raise AssertionError("unreachable")
