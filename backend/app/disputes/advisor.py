"""Dispute assistant: what to do about a chargeback or PayPal dispute on one of our payments.

Who decides what, as everywhere in Checkout Guard:
  - FIXED RULES recommend, from our own records (never the AI's reading):
      D1 already refunded            → contest, with proof of refund (a chargeback would pay the buyer twice)
      D2 3-D Secure authenticated    → contest an unauthorised-use claim: the bank verified the cardholder
                                       and liability shifted to the issuer
      D3 part of a confirmed attack  → accept: the card was used without its owner's consent, contesting
                                       cannot honestly win and only drags the cardholder through it
      D4 anything else               → review: the owner knows whether the order was delivered
  - THE AI reads the language: the buyer's own messages in the dispute (what they actually say happened,
    and whether that contradicts the reason they picked), and drafts the note to PayPal. It never writes a
    number: amounts, dates and ids come from our records through {{placeholders}}, and a draft that breaks
    that rule, or any AI failure, falls back to a fixed template.
  - THE OWNER approves (or edits the note, or dismisses); only then is PayPal's Disputes API called
    (accept-claim, or provide-evidence).
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import push
from app.ai.gemini import AIUnavailable, generate_json
from app.ai.reporter import has_stray_numbers
from app.ai.schemas import ATTACK_CLASSES
from app.db.models import (
    Action,
    ActionStatus,
    ActionType,
    CheckoutAttempt,
    DisputeCase,
    Incident,
    Outcome,
    PayPalEvent,
)

log = structlog.get_logger()

INCIDENT_LEAD = timedelta(minutes=15)
UNAUTHORISED = {
    "UNAUTHORISED",
    "UNAUTHORIZED",
    "UNAUTHORIZED_TRANSACTION",
    "CREDIT_NOT_PROCESSED_UNAUTHORISED",
}
# The evidence PayPal accepts when the shop contests, by the buyer's reason. Seen live (Oct 29): for
# MERCHANDISE_OR_SERVICE_NOT_RECEIVED, OTHER is refused (EVIDENCE_TYPE_IS_NOT_ALLOWED) and
# PROOF_OF_FULFILLMENT is accepted. Reasons not listed here keep OTHER (not yet tried live).
CONTEST_EVIDENCE = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "PROOF_OF_FULFILLMENT",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "PROOF_OF_FULFILLMENT",
}

PLACEHOLDERS = ("capture_id", "amount", "payment_date", "refund_id", "refund_date", "dispute_reason")

RULE_TEXT = {
    "D1_already_refunded": "Already refunded: contest with proof of refund, so the buyer is not paid twice",
    "D2_3ds_authenticated": (
        "The bank verified the cardholder with 3-D Secure: liability for this claim is the issuer's"
    ),
    "D3_attack_payment": (
        "Part of a confirmed card-testing attack: the card was used without consent, accept it"
    ),
    "D4_review": "No record decides it: check whether the order was delivered before you answer",
}

TEMPLATES = {
    "D1_already_refunded": (
        "We refunded this payment in full ({{amount}}) on {{refund_date}}, before this dispute was "
        "opened (refund {{refund_id}}). The buyer already has their money back, so a chargeback would "
        "pay them twice."
    ),
    "D2_3ds_authenticated": (
        "The cardholder's bank authenticated payment {{capture_id}} ({{amount}}, {{payment_date}}) with "
        "3-D Secure before it was taken, so liability for an unauthorised-use claim rests with the issuer."
    ),
    "D3_attack_payment": (
        "We accept this claim. Payment {{capture_id}} ({{amount}}, {{payment_date}}) was part of a "
        "card-testing attack on our shop: the card was used without its owner's consent, and they should "
        "have their money back."
    ),
    "D4_review": "Payment {{capture_id}} ({{amount}}, {{payment_date}}): reason given {{dispute_reason}}.",
}

SYSTEM = """You help a small online shop answer a PayPal dispute. You get the dispute, the shop's records and
the recommendation fixed rules already made. You do not change the recommendation.

Write two things:
- buyer_says: one plain sentence on what the buyer's own messages claim happened (or "No message from the
  buyer." if there is none), and whether that contradicts the reason they picked.
- note: the shop's note to PayPal, courteous and factual, under 80 words, supporting the recommendation.

NEVER write digits yourself. For every amount, date or id use only these placeholders exactly:
{{capture_id}} {{amount}} {{payment_date}} {{refund_id}} {{refund_date}} {{dispute_reason}}.
Never repeat names, emails or anything identifying the buyer."""


class DisputeDraft(BaseModel):
    buyer_says: str = Field(max_length=400)
    contradicts_reason: bool = False
    note: str = Field(max_length=900)


def fill(text: str, facts: dict[str, Any]) -> str:
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(facts.get(m.group(1), m.group(0))), text)


def _scrub(text: str) -> str:
    """A buyer's message before the AI sees it: no emails, card or phone numbers, at most 600 characters."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
    text = re.sub(r"\d[\d -]{5,}\d", "[number]", text)
    return text[:600]


async def dispute_resource(session: AsyncSession, dispute_id: str) -> dict[str, Any]:
    """The dispute as PayPal's latest webhook described it (stored, buyer data redacted)."""
    ev = (
        await session.execute(
            select(PayPalEvent)
            .where(
                PayPalEvent.event_type.like("CUSTOMER.DISPUTE.%"),
                PayPalEvent.payload["resource"]["dispute_id"].as_string() == dispute_id,
            )
            .order_by(PayPalEvent.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ((ev.payload or {}).get("resource") or {}) if ev else {}


async def gather(session: AsyncSession, attempt: CheckoutAttempt) -> dict[str, Any]:
    """Our records about the disputed payment: refunded? 3-D Secure? part of an attack?"""
    refund = (
        await session.execute(
            select(Action)
            .where(
                Action.type == ActionType.REFUND,
                Action.status == ActionStatus.EXECUTED,
                Action.params["attempt_id"].as_string() == attempt.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    attack: Incident | None = None
    incidents = (
        await session.execute(
            select(Incident).where(
                Incident.shop_id == attempt.shop_id,
                Incident.classification.in_(ATTACK_CLASSES),
                Incident.opened_at - INCIDENT_LEAD <= attempt.ts,
                or_(Incident.closed_at.is_(None), Incident.closed_at >= attempt.ts),
            )
        )
    ).scalars()
    for inc in incidents:
        sig = inc.signature or {}
        matched_by_us = (
            await session.execute(
                select(Action.id)
                .where(
                    Action.incident_id == inc.id,
                    Action.type.in_([ActionType.REFUND, ActionType.VOID]),
                    Action.params["attempt_id"].as_string() == attempt.id,
                )
                .limit(1)
            )
        ).first() is not None
        if matched_by_us or attempt.fingerprint_hash in sig.get("fingerprints", []):
            attack = inc
            break
    return {
        "refunded": attempt.outcome == Outcome.REFUNDED or refund is not None,
        "refund_id": (refund.paypal_ref if refund else None) or attempt.refund_id,
        "refund_date": f"{refund.created_at:%d %b %Y}" if refund else None,
        "sca_authenticated": attempt.sca_liability_shift == "YES",
        "attack_incident": attack.id if attack else None,
    }


DISPUTE_PUSH = {
    "contest": "Our records support you: Checkout Guard recommends contesting and drafted the evidence.",
    "accept": "This payment came from the attack: Checkout Guard recommends accepting it.",
    "review": "Checkout Guard drafted a response. You decide whether to contest.",
}


def recommend(reason: str | None, facts: dict[str, Any]) -> tuple[str, str, str | None, dict[str, Any]]:
    """(recommendation, rule, evidence_type, evidence_info). Our records only, in this order."""
    if facts["refunded"]:
        info = {"refund_ids": [facts["refund_id"]]} if facts.get("refund_id") else {}
        return "contest", "D1_already_refunded", "PROOF_OF_REFUND", info
    if facts["sca_authenticated"] and (reason or "").upper() in UNAUTHORISED:
        return "contest", "D2_3ds_authenticated", "OTHER", {}
    if facts["attack_incident"]:
        return "accept", "D3_attack_payment", None, {}
    # Your call; if the owner contests, the evidence PayPal accepts for this reason.
    return "review", "D4_review", CONTEST_EVIDENCE.get((reason or "").upper()), {}


async def draft(
    rule: str, recommendation: str, reason: str | None, messages: list[str], facts: dict[str, Any]
) -> tuple[str, str, str | None, bool]:
    """(note, written_by, buyer_says, contradicts). The AI's draft only if it keeps every number in a
    placeholder; otherwise the fixed template."""
    template = fill(TEMPLATES[rule], facts)
    prompt = (
        f"Recommendation (fixed rules): {recommendation} - {RULE_TEXT[rule]}\n"
        f"Reason the buyer picked: {reason or 'not given'}\n"
        f"Our records: refunded={facts['refunded']}, bank 3-D Secure verified={facts['sca_authenticated']}, "
        f"part of a confirmed card-testing attack={bool(facts['attack_incident'])}\n"
        f"Buyer's messages: {' | '.join(_scrub(m) for m in messages) or 'none'}"
    )
    try:
        out, model = await generate_json(system=SYSTEM, prompt=prompt, schema=DisputeDraft, temperature=0.2)
    except AIUnavailable:
        return template, "template", None, False
    unknown = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", out.note)) - set(PLACEHOLDERS)
    if has_stray_numbers(out.note) or has_stray_numbers(out.buyer_says) or unknown or not out.note.strip():
        log.info("dispute_draft_rejected", rule=rule)
        return template, "template", None, False
    return fill(out.note, facts), model, out.buyer_says, out.contradicts_reason


async def open_case(session: AsyncSession, attempt: CheckoutAttempt) -> DisputeCase:
    """Build the case for a newly disputed payment. Caller commits."""
    r = await dispute_resource(session, attempt.dispute_id or "")
    reason = r.get("reason")
    amount_raw = (r.get("dispute_amount") or {}).get("value")
    amount = Decimal(str(amount_raw)) if amount_raw else attempt.amount
    facts = await gather(session, attempt)
    recommendation, rule, evidence_type, evidence = recommend(reason, facts)
    placeholders = {
        "capture_id": attempt.capture_id or attempt.id,
        "amount": f"${amount:.2f}",
        "payment_date": f"{attempt.ts:%d %b %Y}",
        "refund_id": facts.get("refund_id") or "the refund",
        "refund_date": facts.get("refund_date") or "the refund date",
        "dispute_reason": (reason or "not given").replace("_", " ").lower(),
    }
    messages = [str(m.get("content") or "") for m in r.get("messages") or [] if m.get("posted_by") == "BUYER"]
    note, by, buyer_says, contradicts = await draft(
        rule, recommendation, reason, messages, {**facts, **placeholders}
    )
    case = DisputeCase(
        id=attempt.dispute_id,
        shop_id=attempt.shop_id,
        attempt_id=attempt.id,
        incident_id=facts["attack_incident"],
        reason=reason,
        amount=amount,
        response_due=r.get("seller_response_due_date"),
        recommendation=recommendation,
        rule=rule,
        evidence_type=evidence_type,
        evidence={**evidence, "buyer_says": buyer_says, "contradicts_reason": contradicts},
        note=note,
        note_by=by,
        paypal_live=attempt.paypal_live,
    )
    session.add(case)
    return case


async def open_new_cases(session: AsyncSession, shop_id: str) -> int:
    """Worker pass: a case for every disputed payment that has none yet. Commits."""
    rows = (
        (
            await session.execute(
                select(CheckoutAttempt).where(
                    CheckoutAttempt.shop_id == shop_id,
                    CheckoutAttempt.disputed.is_(True),
                    CheckoutAttempt.dispute_id.is_not(None),
                    CheckoutAttempt.dispute_id.not_in(select(DisputeCase.id)),
                )
            )
        )
        .scalars()
        .all()
    )
    for attempt in rows:
        case = await open_case(session, attempt)
        log.info("dispute_case_opened", dispute=case.id, recommendation=case.recommendation, rule=case.rule)
        push.queue(
            session,
            "dispute",
            f"New PayPal dispute: ${case.amount:.2f}",
            DISPUTE_PUSH.get(case.recommendation, "Checkout Guard drafted a response. You decide."),
            "/guard#disputes",
            f"dispute-{case.id}",
        )
    if rows:
        await session.commit()
    return len(rows)


def now() -> datetime:
    return datetime.now(UTC)
