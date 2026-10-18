"""Owner-facing text: the late-night alert, the morning report and the evidence pack for PayPal.

Numbers never come from the model. Gemini writes prose with {{placeholders}}; we fill them from
the database. If the model invents a number or is unavailable, a deterministic template is used.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gemini
from app.ai.gemini import AIUnavailable, generate_json
from app.ai.schemas import ReportDraft
from app.db.models import Action, ActionStatus, ActionType, CheckoutAttempt, Incident, Outcome
from app.shop.catalog import PRODUCTS

LABELS = {
    "card_testing": "card-testing attack",
    "account_testing": "stolen-account testing attack",
    "genuine_rush": "genuine rush of customers",
    "paypal_outage": "PayPal processing problem",
    "unclear": "unusual checkout activity",
}
DEFENCE_TEXT = {
    ActionType.SCA_ON: "required the bank's 3-D Secure check on every card payment",
    ActionType.BLOCK_FP: "blocked the devices the attack came from",
    ActionType.HOLD_SKU: "paused first-time checkouts of the targeted item",
}

SYSTEM = """You write messages for a small-shop owner about checkout security incidents handled by
Checkout Guard. Plain, calm, short sentences. No jargon beyond "3-D Secure".
NEVER write digits or numbers yourself. Use only the {{placeholders}} provided for every count,
amount, time or percentage, e.g. "{{refunded_count}} test payments ({{refunded_total}}) were refunded".
Use placeholders exactly as given; do not invent new ones."""


async def gather_facts(session: AsyncSession, incident: Incident) -> dict[str, Any]:
    end = incident.closed_at or datetime.now(UTC)
    # Same window the refunds use: the attack's first attempts usually land just before the incident opens.
    sig_start = (incident.signature or {}).get("window_start")
    start = min(datetime.fromisoformat(sig_start), incident.opened_at) if sig_start else incident.opened_at
    in_window = (
        CheckoutAttempt.shop_id == incident.shop_id,
        CheckoutAttempt.source != "seed",
        CheckoutAttempt.ts >= start,
        CheckoutAttempt.ts <= end,
    )
    counts = dict(
        (
            await session.execute(
                select(CheckoutAttempt.outcome, func.count())
                .where(*in_window)
                .group_by(CheckoutAttempt.outcome)
            )
        ).all()
    )
    actions = list((await session.execute(select(Action).where(Action.incident_id == incident.id))).scalars())
    refunds = [a for a in actions if a.type == ActionType.REFUND and a.status == ActionStatus.EXECUTED]
    pending = [
        a for a in actions if a.type == ActionType.REFUND and a.status == ActionStatus.AWAITING_APPROVAL
    ]
    refunded_total = sum((Decimal(a.params.get("amount", "0")) for a in refunds), Decimal("0"))
    defences = [
        DEFENCE_TEXT[ActionType(a.type)]
        for a in actions
        if a.type in DEFENCE_TEXT and a.status == ActionStatus.EXECUTED
    ]
    sig = incident.signature or {}
    top_sku = sig.get("top_sku")
    attempts = sum(counts.values())
    stopped = sum(
        counts.get(o, 0)
        for o in (Outcome.BLOCKED, Outcome.CHALLENGED, Outcome.SCA_REQUIRED, Outcome.DECLINED)
    )
    return {
        "shop_name": "Wick & Wax",
        "classification": LABELS.get(incident.classification or "unclear", "unusual activity"),
        "confidence_pct": f"{round((incident.confidence or 0) * 100)} percent",
        "date": incident.opened_at.strftime("%d %b %Y"),
        "time_range": (
            f"from {incident.opened_at:%H:%M} to {end:%H:%M} UTC"
            if incident.closed_at
            else f"since {incident.opened_at:%H:%M} UTC (still being handled)"
        ),
        "duration_min": str(max(1, round((end - incident.opened_at).total_seconds() / 60))),
        "attempts": str(attempts),
        "stopped": str(stopped),
        "declined": str(counts.get(Outcome.DECLINED, 0)),
        "blocked": str(counts.get(Outcome.BLOCKED, 0) + counts.get(Outcome.CHALLENGED, 0)),
        "sca_required": str(counts.get(Outcome.SCA_REQUIRED, 0)),
        "got_through": str(counts.get(Outcome.CAPTURED, 0) + counts.get(Outcome.REFUNDED, 0)),
        "refunded_count": str(len(refunds)),
        "refunded_total": f"${refunded_total:.2f}",
        "pending_refunds": str(len(pending)),
        "targeted_item": PRODUCTS[top_sku].name if top_sku in PRODUCTS else "several items",
        "devices_blocked": str(len(sig.get("fingerprints", []))),
        "new_email_domains": str(len(sig.get("new_email_domains", []))),
        "defences": "; ".join(defences) if defences else "no defences were needed",
        "ai_summary": (incident.verdict or {}).get("summary", ""),
        "incident_id": incident.id,
    }


def fill(text: str, facts: dict[str, Any]) -> str:
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(facts.get(m.group(1), m.group(0))), text)


def has_stray_numbers(text: str) -> bool:
    t = re.sub(r"\{\{\s*\w+\s*\}\}", "", text)
    t = re.sub(r"3-D|3DS", "", t)
    t = re.sub(r"(?m)^\s*\d+\.\s", "", t)  # ordered-list markers
    t = re.sub(r"(?m)^#+", "", t)
    return bool(re.search(r"\d", t))


def template(facts: dict[str, Any]) -> ReportDraft:
    is_attack = "attack" in facts["classification"]
    alert = (
        "Your checkout is under a {{classification}}. I've blocked it and refunded {{refunded_count}} test "
        "payments ({{refunded_total}}). No action needed tonight."
        if is_attack
        else "I noticed {{classification}} at your checkout and kept watching. Nothing was blocked."
    )
    report = """# Overnight report: {{classification}}

**When:** {{date}}, {{time_range}}, {{duration_min}} min

| | |
|---|---|
| Checkout attempts | {{attempts}} |
| Stopped (declined, 3-D Secure, blocked) | {{stopped}} |
| Payments that went through | {{got_through}} |
| Refunded automatically | {{refunded_count}} ({{refunded_total}}) |
| Waiting for your approval | {{pending_refunds}} |

**What I did:** {{defences}}.

**Why:** {{ai_summary}}

The extra checks are switched off again once the attack stops, so real customers aren't slowed down.
"""
    evidence = """# Incident summary for PayPal support

- **Merchant:** {{shop_name}}
- **Incident ID:** {{incident_id}}
- **Type:** {{classification}} (confidence {{confidence_pct}})
- **Window:** {{date}}, {{time_range}}
- **Targeted item:** {{targeted_item}}
- **Attempts:** {{attempts}}; declined {{declined}}; stopped by 3-D Secure {{sca_required}}; blocked {{blocked}}
- **Unauthorised payments that succeeded:** {{got_through}}; refunded proactively {{refunded_count}} ({{refunded_total}})
- **Signals:** {{new_email_domains}} never-seen email domains, {{devices_blocked}} attacking devices

The merchant refunded the test payments before any dispute was opened and asks that disputes
related to this incident be reviewed with this context.
"""
    return ReportDraft(alert=alert, report_md=report, evidence_md=evidence)


async def write(
    session: AsyncSession, incident: Incident, skip_ai: bool = False
) -> tuple[dict[str, str], str]:
    """skip_ai: the analyst's call just failed in this tick, so another Gemini call would only add latency
    to the owner's alert. Also skipped when this process saw every model paused by the breaker."""
    facts = await gather_facts(session, incident)
    fallback = template(facts)
    model_id = "template"
    draft = fallback
    try:
        if skip_ai or await gemini.cooling_down():
            raise AIUnavailable("skipped: Gemini unavailable this tick")
        context = {k: facts[k] for k in ("classification", "defences", "targeted_item")} | {
            "incident_status": "closed: the attack has stopped and extra checks were switched off"
            if incident.closed_at
            else "ongoing: defences are active right now",
            "outcome_definitions": "declined = refused by the bank; blocked = stopped by Checkout Guard "
            "before reaching PayPal; sca_required = stopped by the 3-D Secure check",
        }
        prompt = (
            "Write the alert, the morning report and the PayPal evidence summary for this incident.\n"
            f"Available placeholders: {', '.join('{{' + k + '}}' for k in facts)}\n"
            "Use {{ai_summary}} where the analyst's explanation belongs.\n"
            f"Context (for wording only): {json.dumps(context)}"
        )
        ai_draft, model_id = await generate_json(
            system=SYSTEM, prompt=prompt, schema=ReportDraft, temperature=0.3
        )
        draft = ReportDraft(
            alert=fallback.alert if has_stray_numbers(ai_draft.alert) else ai_draft.alert,
            report_md=fallback.report_md if has_stray_numbers(ai_draft.report_md) else ai_draft.report_md,
            evidence_md=fallback.evidence_md
            if has_stray_numbers(ai_draft.evidence_md)
            else ai_draft.evidence_md,
        )
    except AIUnavailable:
        model_id = "template"
    return (
        {
            "alert_text": fill(draft.alert, facts)[:400],
            "report_md": fill(draft.report_md, facts),
            "evidence_md": fill(draft.evidence_md, facts),
        },
        model_id,
    )
