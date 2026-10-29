"""Owner-facing text: the late-night alert, the morning report and the evidence pack for PayPal.

Numbers never come from the model. Gemini writes prose with {{placeholders}}; we fill them from
the database. If the model invents a number or is unavailable, a deterministic template is used.
"""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.executor import classify_match
from app.ai import gemini
from app.ai.gemini import AIUnavailable, generate_json
from app.ai.schemas import ReportDraft
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    AuditLog,
    CheckoutAttempt,
    DeviceEntry,
    DeviceListKind,
    Incident,
    Outcome,
)
from app.incidents.cadence import RULES_FALLBACK, TEMPLATE
from app.incidents.decision import DECIDED_BY_AGREE, DECIDED_BY_DISAGREE
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
    # Neutral: a rules-only block (AI down, "unclear") is a guess from behaviour, not a confirmed attack.
    ActionType.BLOCK_FP: "blocked the devices showing card-testing behaviour",
    ActionType.HOLD_SKU: "paused first-time checkouts of the targeted item",
    # Also planned for a confident paypal_outage verdict, so it must not claim the shoppers were genuine.
    ActionType.RELAX: "switched the extra checks off again once Checkout Guard judged it was not fraud",
}
NO_DEFENCES = "no defences were needed"
# The alert's wording when the full DEFENCE_TEXT would not fit a phone notification.
DEFENCE_SHORT = {
    ActionType.SCA_ON: "turned on 3-D Secure",
    ActionType.BLOCK_FP: "blocked the devices",
    ActionType.HOLD_SKU: "paused the targeted item for new shoppers",
    ActionType.RELAX: "switched the extra checks off again",
}
# What an AI recommendation is called when the fixed rules did not apply it (policy decides, not the AI).
RECOMMENDED_TEXT = {
    ActionType.SCA_ON: "3-D Secure",
    ActionType.BLOCK_FP: "device blocks",
    ActionType.HOLD_SKU: "an item hold",
    ActionType.REFUND: "refunds",
}
# Only these protect the checkout beyond a bank prompt: with nothing but 3-D Secure on, the owner is never
# told "no action needed".
STRONG_DEFENCES = {ActionType.BLOCK_FP, ActionType.HOLD_SKU}
AI_STATUS = {
    "live": "decided by the scorecard (fixed, explainable rules)",
    "kept": "kept the verdict the AI second opinion confirmed: the AI's latest re-check failed",
    "rules": "the scorecard was unsure and the AI second opinion was unavailable, so fixed safety rules decided",
    "none": "no verdict yet: the incident ended before the first assessment",
}
ALERT_MAX = 400
# Lists and tables have no place in a phone notification: they can push the sentences the owner must see
# past the cut, and a public ntfy topic must not carry capture or refund ids.
LIST_PLACEHOLDERS = ("refund_table", "refunded_capture_ids", "refund_ids", "dispute_capture_ids")

# Everything the model may write as {{placeholder}}; gather_facts fills exactly these. Keys starting with
# "_" are template-only inputs and never offered to (or filled for) the model.
PLACEHOLDERS = (
    "shop_name",
    "classification",
    "confidence_pct",
    "date",
    "time_range",
    "duration_min",
    "attempts",
    "stopped",
    "declined",
    "blocked",
    "sca_required",
    "got_through",
    "refunded_count",
    "refunded_total",
    "refunded_capture_ids",
    "refund_ids",
    "pending_refunds",
    "awaiting_approval_count",
    "refunds_unresolved",
    "disputed_count",
    "dispute_capture_ids",
    "refund_table",
    "targeted_item",
    "devices_blocked",
    "new_email_domains",
    "defences",
    "recommended_only",
    "ai_status",
    "decision_path",
    "ai_summary",
    "incident_id",
)

SYSTEM = """You write messages for a small-shop owner about checkout security incidents handled by
Checkout Guard. Plain, calm, short sentences. No jargon beyond "3-D Secure".
NEVER write digits or numbers yourself. Use only the {{placeholders}} provided for every count,
amount, time or percentage, e.g. "{{refunded_count}} test payments ({{refunded_total}}) were refunded".
Use placeholders exactly as given; do not invent new ones.
Only describe what Checkout Guard actually did ({{defences}}, refunds counted in {{refunded_count}});
anything in {{recommended_only}} was NOT done. Never tell the owner "no action needed" unless asked to."""


def ai_status(incident: Incident) -> str:
    """live / kept / rules / none: how the classification behind this report was reached.
    live: a fresh scorecard verdict (with or without a Gemini tie-break or explanation). kept: an agreed
    tie-break the AI could not re-confirm. rules: the scorecard was unsure and no AI second opinion was
    available (policy R3b/R3d territory). "none" is an incident closed before its first assessment."""
    if incident.classification is None:
        return "none"
    if incident.ai_refresh_failed_at is not None:
        return "kept"
    if incident.classification == "unclear" and (
        not incident.ai_available or incident.model_id == RULES_FALLBACK
    ):
        return "rules"
    return "live"


def decision_path(incident: Incident) -> str:
    """Who decided and who wrote the words, e.g. "scorecard-v1, confirmed by the AI second opinion;
    explained by gemini-2.5-flash"."""
    if incident.classification is None:
        return "no verdict yet"
    if incident.classifier_version is None:
        return "the AI analyst (an incident from before the scorecard)"
    tiebreak = (incident.scorecard or {}).get("tiebreak") or {}
    who = incident.classifier_version
    if incident.decided_by == DECIDED_BY_AGREE:
        who += ", confirmed by the AI second opinion"
    elif incident.decided_by == DECIDED_BY_DISAGREE:
        who += "; the AI second opinion disagreed, so it stays unclear"
    elif tiebreak.get("outcome") == "unverified":
        who += " (the AI second opinion's evidence did not match our numbers, so it was ignored)"
    elif tiebreak.get("outcome") == "unavailable":
        who += " (the AI second opinion was unavailable)"
    ai_words = incident.model_id not in (None, TEMPLATE, RULES_FALLBACK)
    words = incident.model_id if ai_words else "Checkout Guard's own template"
    return f"{who}; explained by {words}"


async def _planned_types(session: AsyncSession, incident: Incident, actions: list[Action]) -> set[str]:
    """Action types the policy chose at any point (audit `decided`, plus everything on record), so an AI
    recommendation the rules did take up is never labelled "recommended only"."""
    rows = (
        await session.execute(
            select(AuditLog.data).where(
                AuditLog.incident_id == incident.id, AuditLog.event.in_(("decided", "rules_first"))
            )
        )
    ).scalars()
    planned = {str(entry).split(":", 1)[0] for data in rows for entry in (data or {}).get("actions", [])}
    return planned | {a.type for a in actions}


def _recommended_only(incident: Incident, planned: set[str]) -> str:
    if ai_status(incident) in ("rules", "none"):
        return ""  # the rules fallback's fixed list is not a recommendation anyone made
    recs = {
        ActionType.REFUND if r == "REFUND_MATCHED" else r
        for r in (incident.verdict or {}).get("recommended_actions", [])
    }
    return ", ".join(
        f"{text} (recommended only)" for t, text in RECOMMENDED_TEXT.items() if t in recs and t not in planned
    )


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
    # Sent to PayPal with no answer yet, or refused / given up on: money may or may not have moved, so the
    # owner has to check these in PayPal and the report must never say "no action needed" while they exist.
    # A failed try that a later refund decision settled (a retry, the owner's approval or Keep) is history.
    settled = {
        a.params.get("attempt_id")
        for a in actions
        if a.type == ActionType.REFUND and a.status != ActionStatus.FAILED
    }
    unresolved = [
        a
        for a in actions
        if a.type == ActionType.REFUND
        and (
            a.status == ActionStatus.IN_FLIGHT
            or (a.status == ActionStatus.FAILED and a.params.get("attempt_id") not in settled)
        )
    ]
    refunded_total = sum((Decimal(a.params.get("amount", "0")) for a in refunds), Decimal("0"))
    executed = [a for a in actions if a.status == ActionStatus.EXECUTED]
    defences = list(
        dict.fromkeys(DEFENCE_TEXT[ActionType(a.type)] for a in executed if a.type in DEFENCE_TEXT)
    )
    # Devices this incident put on the block list (device_entries), expired or released ones included:
    # the BLOCK_FP action params would also count a re-block of the same device.
    own_blocks = set(
        (
            await session.execute(
                select(DeviceEntry.fingerprint).where(
                    DeviceEntry.incident_id == incident.id, DeviceEntry.kind == DeviceListKind.BLOCK
                )
            )
        ).scalars()
    )
    devices_blocked = len(own_blocks)
    sig = incident.signature or {}
    # The payments PayPal support needs to see: every capture a refund was decided on, plus any disputed
    # capture of this attack (those are never refunded automatically, executor._matches). A real customer's
    # dispute on another order in the same window is not part of this incident.
    refund_attempts = {a.params.get("attempt_id") for a in actions if a.type == ActionType.REFUND}
    dispute_q = or_(CheckoutAttempt.disputed.is_(True), CheckoutAttempt.dispute_id.is_not(None))
    found = (
        await session.execute(
            select(CheckoutAttempt).where(
                or_(CheckoutAttempt.id.in_(refund_attempts - {None}), and_(*in_window, dispute_q))
            )
        )
    ).scalars()

    def ours(a: CheckoutAttempt) -> bool:
        return (
            a.id in refund_attempts
            or a.fingerprint_hash in own_blocks
            or (a.sku == sig.get("top_sku") and classify_match(a.fingerprint_hash, a.email_domain, sig, ()))
            is not None
        )

    attempts_by_id = {a.id: a for a in found if ours(a)}
    disputed = sorted((a for a in attempts_by_id.values() if dispute_open(a)), key=lambda a: a.ts)
    top_sku = sig.get("top_sku")
    attempts = sum(counts.values())
    stopped = sum(
        counts.get(o, 0)
        for o in (
            Outcome.BLOCKED,
            Outcome.CHALLENGED,
            Outcome.SCA_REQUIRED,
            Outcome.SCA_FAILED,
            Outcome.DECLINED,
        )
    )
    status = ai_status(incident)
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
        # Stopped by 3-D Secure: bot orders left at the bank's check, and browser cards that failed it.
        "sca_required": str(counts.get(Outcome.SCA_REQUIRED, 0) + counts.get(Outcome.SCA_FAILED, 0)),
        "got_through": str(counts.get(Outcome.CAPTURED, 0) + counts.get(Outcome.REFUNDED, 0)),
        # Payment holds (app/actions/holds.py): approved by the bank, voided before anything settled.
        "held_count": str(counts.get(Outcome.AUTHORIZED, 0)),
        "voided_count": str(counts.get(Outcome.VOIDED, 0)),
        "refunded_count": str(len(refunds)),
        "refunded_total": f"${refunded_total:.2f}",
        "refunded_capture_ids": ", ".join(_capture_label(a.params) for a in refunds) or "none",
        "refund_ids": ", ".join(_refund_label(a) for a in refunds) or "none",
        "pending_refunds": str(len(pending)),  # P5 name, kept for drafts that use it
        "awaiting_approval_count": str(len(pending)),
        "refunds_unresolved": str(len(unresolved)),
        "disputed_count": str(len(disputed)),
        "dispute_capture_ids": ", ".join(a.capture_id or a.id for a in disputed) or "none",
        "refund_table": refund_table(actions, attempts_by_id),
        "targeted_item": PRODUCTS[top_sku].name if top_sku in PRODUCTS else "several items",
        "devices_blocked": str(devices_blocked),
        "new_email_domains": str(len(sig.get("new_email_domains", []))),
        "defences": "; ".join(defences) if defences else NO_DEFENCES,
        "recommended_only": _recommended_only(incident, await _planned_types(session, incident, actions)),
        "ai_status": AI_STATUS[status],
        "decision_path": decision_path(incident),
        "ai_summary": (incident.verdict or {}).get("summary", ""),
        "incident_id": incident.id,
        # Template-only: what decides the alert's wording.
        "_ai": status,
        "_executed": sorted({a.type for a in executed}),
        "_resolved_disputes": any(has_dispute(a) and not dispute_open(a) for a in attempts_by_id.values()),
        "_sale": bool((incident.assessment_context or {}).get("sale_window")),
        "_closed": incident.closed_at is not None,
    }


REFUND_STATUS = {
    ActionStatus.EXECUTED: "refunded",
    ActionStatus.IN_FLIGHT: "sent, no answer from PayPal yet",
    ActionStatus.FAILED: "not refunded (check in PayPal)",
    ActionStatus.AWAITING_APPROVAL: "waiting for the merchant",
    ActionStatus.REJECTED: "kept by the merchant",
}
MATCH_TEXT = {
    "strong_fingerprint": "device used several cards or accounts",
    "blocked_device": "device already blocked as a bot",
    "flagged_device": "device flagged in this attack",
    "single_decline_device": "device had a declined payment",
    "suspect_domain": "email domain with many declines",
}


def _capture_label(p: dict[str, Any]) -> str:
    return f"{p.get('capture_id') or '-'}{' (simulated)' if p.get('simulated') else ''}"


def _refund_label(a: Action) -> str:
    if not a.paypal_ref:
        return "-"
    return f"{a.paypal_ref}{' (simulated)' if (a.params or {}).get('simulated') else ''}"


def has_dispute(a: CheckoutAttempt) -> bool:
    return bool(a.disputed or a.dispute_id)


def dispute_open(a: CheckoutAttempt) -> bool:
    """Still waiting on the merchant's answer. A resolved dispute (webhook_processor.apply_dispute) keeps
    its dispute_id, and `disputed` stays True when the buyer was paid, but there is nothing left to answer."""
    return a.disputed and not (a.dispute_status or "").startswith("RESOLVED")


def _dispute_text(a: CheckoutAttempt | None) -> str:
    if a is None or not has_dispute(a):
        return "no"
    if not dispute_open(a):
        outcome = (a.dispute_status or "").partition(":")[2].lower() or "unknown outcome"
        return f"resolved ({outcome})"
    return f"yes ({a.dispute_status.lower()})" if a.dispute_status else "yes"


def refund_table(actions: list[Action], attempts: dict[str, CheckoutAttempt] | None = None) -> str:
    """The evidence pack's transactions table, straight from the actions and checkout_attempts tables
    (never the model): every refund decision with its PayPal refund id, then any disputed capture no refund
    was decided on. Offline-simulated captures and refunds are labelled. Empty when there is nothing."""
    attempts = attempts or {}
    rows = sorted(
        (a for a in actions if a.type == ActionType.REFUND),
        key=lambda a: a.created_at or datetime.min.replace(tzinfo=UTC),
    )
    decided = {a.params.get("attempt_id") for a in rows}
    disputed_only = sorted(
        (t for t in attempts.values() if t.id not in decided and has_dispute(t)), key=lambda t: t.ts
    )
    if not rows and not disputed_only:
        return ""
    lines = [
        "| Capture | Amount | Status | Why it matched | Refund ID | Payment outcome | Processor code | Disputed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a in rows:
        p = a.params or {}
        t = attempts.get(p.get("attempt_id"))
        simulated = p.get("simulated") or (t is not None and not t.paypal_live)
        why = ", ".join(MATCH_TEXT.get(m, m) for m in p.get("matched_on") or []) or "-"
        lines.append(
            f"| {_capture_label(p | {'simulated': simulated})} | ${Decimal(p.get('amount', '0')):.2f} "
            f"| {REFUND_STATUS.get(ActionStatus(a.status), a.status)} | {why} | {_refund_label(a)} "
            f"| {t.outcome if t else '-'} | {(t.processor_code if t else None) or '-'} | {_dispute_text(t)} |"
        )
    for t in disputed_only:
        label = _capture_label({"capture_id": t.capture_id, "simulated": not t.paypal_live})
        status = "disputed: answer it in PayPal" if dispute_open(t) else "dispute resolved"
        lines.append(
            f"| {label} | ${t.amount:.2f} | not refunded ({status}) | - | - "
            f"| {t.outcome} | {t.processor_code or '-'} | {_dispute_text(t)} |"
        )
    return "\n".join(lines)


def fill(text: str, facts: dict[str, Any]) -> str:
    def value(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(facts[key]) if key in PLACEHOLDERS and key in facts else m.group(0)

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", value, text)


def unknown_placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{\{\s*(\w+)\s*\}\}", text)) - set(PLACEHOLDERS)


def has_stray_numbers(text: str) -> bool:
    t = re.sub(r"\{\{\s*\w+\s*\}\}", "", text)
    t = re.sub(r"3-D|3DS", "", t)
    t = re.sub(r"(?m)^\s*\d+\.\s", "", t)  # ordered-list markers
    t = re.sub(r"(?m)^#+", "", t)
    return bool(re.search(r"\d", t))


def _nonzero(facts: dict[str, Any], key: str) -> bool:
    return str(facts.get(key, "0")) != "0"


def all_clear(facts: dict[str, Any]) -> bool:
    """Whether the owner may be told "no action needed": an attack met by real defences (not 3-D Secure
    alone), on a fresh verdict (not a kept AI-confirmed one), outside a sale, with nothing waiting on the owner or on PayPal and no
    recommendation left unapplied."""
    return (
        "attack" in facts["classification"]
        and facts.get("_ai", "live") == "live"
        and not facts.get("_sale")
        and bool(STRONG_DEFENCES & set(facts.get("_executed", [])))
        and not facts.get("recommended_only")
        and not any(
            _nonzero(facts, k) for k in ("refunds_unresolved", "awaiting_approval_count", "disputed_count")
        )
    )


def required_alert_placeholders(facts: dict[str, Any]) -> list[str]:
    """What an alert must mention for this incident; an AI alert missing one is replaced by the template."""
    keys = [
        k for k in ("refunds_unresolved", "awaiting_approval_count", "disputed_count") if _nonzero(facts, k)
    ]
    if facts.get("_ai", "live") != "live":
        keys.append("ai_status")
    if facts.get("recommended_only"):
        keys.append("recommended_only")
    return keys


def _alert(facts: dict[str, Any]) -> str:
    """Assembled from what actually happened: each sentence is there because its action ran (or is
    waiting on someone). Most urgent first; optional sentences are dropped to fit the notification."""
    is_attack = "attack" in facts["classification"]
    head = (
        "Your checkout is under a {{classification}}."
        if is_attack
        else "I noticed {{classification}} at your checkout."
    )
    must: list[str] = [head]
    if _nonzero(facts, "refunds_unresolved"):
        must.append("{{refunds_unresolved}} refunds were not confirmed by PayPal: please check them there.")
    if _nonzero(facts, "awaiting_approval_count"):
        must.append("{{awaiting_approval_count}} refunds wait for your approval.")
    if _nonzero(facts, "disputed_count"):
        must.append("{{disputed_count}} payments are disputed: answer them in PayPal.")
    defences_at = None
    if facts.get("defences", NO_DEFENCES) != NO_DEFENCES:
        defences_at = len(must)
        must.append("I {{defences}}.")
    if _nonzero(facts, "refunded_count"):
        noun = "test payments" if is_attack else "payments"
        must.append(f"I refunded {{{{refunded_count}}}} {noun} ({{{{refunded_total}}}}).")
    optional: list[str] = []
    ai = facts.get("_ai", "live")
    if ai == "rules":
        must.append("The scorecard was unsure and the AI was unavailable, so fixed safety rules decided.")
    elif ai == "kept":
        must.append("The AI's re-check failed, so its last verdict stands.")
    elif ai == "none":
        must.append("It ended before a verdict was reached.")
    if facts.get("recommended_only"):
        must.append("Not applied: {{recommended_only}}.")
    if facts.get("_sale"):
        optional.append("Your sale is on, so I kept the checks light.")
    if all_clear(facts):
        closing = "No action needed tonight."
    elif facts.get("defences", NO_DEFENCES) == NO_DEFENCES and not _nonzero(facts, "refunded_count"):
        closing = "Nothing was blocked."
    else:
        closing = "It has stopped." if facts.get("_closed") else "I'm still watching."

    def fits(parts: list[str]) -> bool:
        return len(fill(" ".join(parts), facts)) <= ALERT_MAX

    if not fits([*must, closing]) and defences_at is not None:
        executed = set(facts.get("_executed", []))
        short = [text for t, text in DEFENCE_SHORT.items() if t in executed]
        if short:
            listed = short[0] if len(short) == 1 else f"{', '.join(short[:-1])} and {short[-1]}"
            must[defences_at] = f"I {listed}."
    while optional and not fits([*must, *optional, closing]):
        optional.pop()
    parts = [*must, *optional, closing]
    while len(parts) > 1 and not fits(parts):
        parts.pop()  # whole sentences, least urgent last: never a cut mid-word
    return " ".join(parts)


RESOLVED_TEXT = (
    "Some disputes on payments from this incident have already been resolved; the table shows each outcome."
)


def _dispute_paragraph(facts: dict[str, Any]) -> str:
    if _nonzero(facts, "disputed_count"):
        return (
            "{{disputed_count}} payments from this incident have an open dispute ({{dispute_capture_ids}}). "
            "Checkout Guard does not refund a disputed payment automatically, so the buyer is not paid "
            "twice. The merchant asks that these disputes be reviewed with this context."
            + (f" {RESOLVED_TEXT}" if facts.get("_resolved_disputes") else "")
        )
    if facts.get("_resolved_disputes"):
        return (
            f"{RESOLVED_TEXT} No dispute is open. The merchant asks that any later dispute related to this "
            "incident be reviewed with this context."
        )
    if _nonzero(facts, "refunded_count"):
        return (
            "The merchant refunded the test payments before any dispute was opened and asks that disputes "
            "related to this incident be reviewed with this context."
        )
    return (
        "No dispute had been opened on these payments when this summary was written. The merchant asks "
        "that any later dispute related to this incident be reviewed with this context."
    )


def template(facts: dict[str, Any]) -> ReportDraft:
    not_applied = "\n**Not applied:** {{recommended_only}}.\n" if facts.get("recommended_only") else ""
    report = f"""# Overnight report: {{{{classification}}}}

**When:** {{{{date}}}}, {{{{time_range}}}}, {{{{duration_min}}}} min

| | |
|---|---|
| Checkout attempts | {{{{attempts}}}} |
| Stopped (declined, 3-D Secure, blocked) | {{{{stopped}}}} |
| Payments that went through | {{{{got_through}}}} |
| Refunded automatically or with your approval | {{{{refunded_count}}}} ({{{{refunded_total}}}}) |
| Waiting for your approval | {{{{awaiting_approval_count}}}} |
| Not confirmed by PayPal (check in PayPal) | {{{{refunds_unresolved}}}} |
| Open disputes | {{{{disputed_count}}}} |
| Devices blocked by this incident | {{{{devices_blocked}}}} |

**What I did:** {{{{defences}}}}.
{not_applied}
**Analysis:** {{{{ai_status}}}}.

**Decided by:** {{{{decision_path}}}}.

**Why:** {{{{ai_summary}}}}

The extra checks are switched off again once the attack stops, so real customers aren't slowed down.
"""
    evidence = f"""# Incident summary for PayPal support

- **Merchant:** {{{{shop_name}}}}
- **Incident ID:** {{{{incident_id}}}}
- **Type:** {{{{classification}}}} (confidence {{{{confidence_pct}}}}; {{{{ai_status}}}})
- **Decided by:** {{{{decision_path}}}}
- **Window:** {{{{date}}}}, {{{{time_range}}}}
- **Targeted item:** {{{{targeted_item}}}}
- **Attempts:** {{{{attempts}}}}; declined {{{{declined}}}}; stopped by 3-D Secure {{{{sca_required}}}}; blocked {{{{blocked}}}}
- **Payments captured in the window:** {{{{got_through}}}}; refunded proactively as attack payments {{{{refunded_count}}}} ({{{{refunded_total}}}})
- **Refunded captures:** {{{{refunded_capture_ids}}}}
- **PayPal refund IDs:** {{{{refund_ids}}}}
- **Refunds awaiting the merchant's approval:** {{{{awaiting_approval_count}}}}
- **Defences applied:** {{{{defences}}}}
- **Signals:** {{{{new_email_domains}}}} never-seen email domains; {{{{devices_blocked}}}} devices blocked by this incident

{{{{refund_table}}}}

{_dispute_paragraph(facts)}
"""
    return ReportDraft(alert=_alert(facts), report_md=report, evidence_md=evidence)


NO_ACTION = re.compile(r"no (further )?action (is )?needed|nothing (else )?(for you )?to do", re.IGNORECASE)


BEFORE_DISPUTE = re.compile(r"before any dispute", re.IGNORECASE)


def _owner_text_ok(text: str, facts: dict[str, Any]) -> bool:
    """The alert and the morning report: model wording stands only if it mentions everything the owner has
    to act on and claims "no action needed" only when that is true."""
    return (
        not has_stray_numbers(text)
        and not unknown_placeholders(text)
        and all("{{" + k + "}}" in text for k in required_alert_placeholders(facts))
        and (all_clear(facts) or not NO_ACTION.search(text))
    )


def _alert_ok(alert: str, facts: dict[str, Any]) -> bool:
    # Checked after filling: a draft that only fits by render()'s hard cut would lose its last sentences.
    used = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", alert))
    return (
        _owner_text_ok(alert, facts)
        and not used & set(LIST_PLACEHOLDERS)
        and len(fill(alert, facts)) <= ALERT_MAX
    )


def _evidence_ok(evidence: str, facts: dict[str, Any]) -> bool:
    # A model told nothing about disputes may still write the usual "refunded before any dispute" line,
    # which is only true when something was refunded and no dispute exists at all.
    disputed = _nonzero(facts, "disputed_count")
    before_ok = not disputed and not facts.get("_resolved_disputes") and _nonzero(facts, "refunded_count")
    return (
        not has_stray_numbers(evidence)
        and not unknown_placeholders(evidence)
        and (not disputed or "{{disputed_count}}" in evidence)
        and (before_ok or not BEFORE_DISPUTE.search(evidence))
    )


async def compose(
    incident: Incident, facts: dict[str, Any], skip_ai: bool = False
) -> tuple[ReportDraft, str]:
    """The model writes wording only; no database access, so callers run it with no transaction open.
    skip_ai: the analyst's call just failed in this tick, so another Gemini call would only add latency
    to the owner's alert. Also skipped when this process saw every model paused by the breaker."""
    fallback = template(facts)
    try:
        if skip_ai or await gemini.cooling_down():
            raise AIUnavailable("skipped: Gemini unavailable this tick")
        context = {k: facts[k] for k in ("classification", "defences", "targeted_item", "ai_status")} | {
            "incident_status": "closed: the attack has stopped and extra checks were switched off"
            if incident.closed_at
            else "ongoing: defences are active right now",
            "outcome_definitions": "declined = refused by the bank; blocked = stopped by Checkout Guard "
            "before reaching PayPal; sca_required = stopped by the 3-D Secure check (never verified, or "
            "the bank's verification failed or was cancelled)",
            "recommended_only": facts["recommended_only"] or "none",
            "disputes": "some payments have an open dispute"
            if _nonzero(facts, "disputed_count")
            else "some disputes were already resolved"
            if facts.get("_resolved_disputes")
            else "none",
        }
        required = required_alert_placeholders(facts)
        prompt = (
            "Write the alert, the morning report and the PayPal evidence summary for this incident.\n"
            f"Available placeholders: {', '.join('{{' + k + '}}' for k in PLACEHOLDERS)}\n"
            f"The alert is a phone notification: at most {ALERT_MAX} characters once filled, and never "
            f"{', '.join('{{' + k + '}}' for k in LIST_PLACEHOLDERS)}.\n"
            "Use {{ai_summary}} where the analyst's explanation belongs.\n"
            + (
                f"The alert and the report must include: {', '.join('{{' + k + '}}' for k in required)}\n"
                if required
                else ""
            )
            + (
                "The evidence summary must say that {{disputed_count}} payments are disputed "
                "({{dispute_capture_ids}}).\n"
                if _nonzero(facts, "disputed_count")
                else ""
            )
            + ("" if all_clear(facts) else "Do not tell the owner that no action is needed.\n")
            + f"Context (for wording only): {json.dumps(context)}"
        )
        ai_draft, model_id = await generate_json(
            system=SYSTEM, prompt=prompt, schema=ReportDraft, temperature=0.3
        )
    except AIUnavailable:
        return fallback, "template"
    evidence = ai_draft.evidence_md
    if facts["refund_table"] and "{{refund_table}}" not in evidence:
        evidence += "\n\n{{refund_table}}"  # PayPal support always gets the per-capture transactions table
    report_ok = _owner_text_ok(ai_draft.report_md, facts)
    return ReportDraft(
        alert=ai_draft.alert if _alert_ok(ai_draft.alert, facts) else fallback.alert,
        report_md=ai_draft.report_md if report_ok else fallback.report_md,
        evidence_md=evidence if _evidence_ok(evidence, facts) else fallback.evidence_md,
    ), model_id


def render(draft: ReportDraft, facts: dict[str, Any]) -> dict[str, str]:
    return {
        "alert_text": fill(draft.alert, facts)[:ALERT_MAX],  # backstop only: compose() checks the length
        "report_md": fill(draft.report_md, facts),
        "evidence_md": fill(draft.evidence_md, facts),
    }


async def write(
    session: AsyncSession, incident: Incident, skip_ai: bool = False
) -> tuple[dict[str, str], str]:
    """gather_facts + compose + render in one go. IncidentService.write_report runs the three steps with
    a commit in between instead, so no transaction stays open while Gemini writes."""
    facts = await gather_facts(session, incident)
    draft, model_id = await compose(incident, facts, skip_ai=skip_ai)
    return render(draft, facts), model_id
