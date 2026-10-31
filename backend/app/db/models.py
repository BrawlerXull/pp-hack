"""Database schema. Enum-like columns are plain strings backed by StrEnums so migrations stay simple."""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonB = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Base(DeclarativeBase):
    pass


# ---- enums -------------------------------------------------------------------------------------


class Source(StrEnum):
    WEB = "web"
    SIMULATOR = "simulator"
    SEED = "seed"  # synthetic baseline history
    # Seen only through PayPal webhooks: an order from a checkout that does not call the guard (a hosted
    # button, a plugin). No device signal; detection, refunds and alerts still work (incidents/observer.py).
    OBSERVED = "observed"
    API = "api"  # another shop's checkout calling POST /v1/guard/check


class Method(StrEnum):
    CARD = "card"
    WALLET = "wallet"


class Decision(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_SCA = "allow_with_sca"
    CHALLENGE = "challenge"
    BLOCK = "block"


class Outcome(StrEnum):
    CREATED = "created"  # wallet order waiting for buyer approval
    CAPTURED = "captured"
    DECLINED = "declined"
    ERROR = "error"  # PayPal/processor error (outage signal), not a card decline
    BLOCKED = "blocked"  # stopped by the guard before reaching PayPal
    CHALLENGED = "challenged"
    SCA_REQUIRED = "sca_required"  # 3DS step-up required; bots never complete it
    # A browser card order whose 3-D Secure check did not pass (failed, cancelled, or no liability shift when
    # the guard required it): not captured. app/paypal/three_ds.py decides.
    SCA_FAILED = "sca_failed"
    REFUNDED = "refunded"
    # Payment hold (app/actions/holds.py): during an incident a first-time shopper's order is authorised, not
    # captured. The bank approved the card, but no money has settled yet.
    AUTHORIZED = "authorized"
    # A hold the attack matched was voided: the authorisation was released, nothing settled, no fee, nothing
    # left to dispute.
    VOIDED = "voided"


class DeclineCategory(StrEnum):
    """Why PayPal said no (app/paypal/codes.py). Lets detection tell stolen cards from honest bad luck."""

    FRAUD = "fraud"  # stolen, lost, invalid or restricted card/account
    CUSTOMER = "customer"  # honest shopper's bad luck: insufficient funds, expired card
    PROBING = "probing"  # wrong CVV/expiry: someone guessing card details
    PROCESSOR = "processor"  # integration or processor problem (stored on ERROR rows)
    UNKNOWN_DECLINE = "unknown_decline"  # declined without a reason (a 422 carries only an issue name)


class GuardMode(StrEnum):
    NORMAL = "normal"
    SCA_ALL = "sca_all"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ActionType(StrEnum):
    SCA_ON = "SCA_ON"
    BLOCK_FP = "BLOCK_FP"
    HOLD_SKU = "HOLD_SKU"
    REFUND = "REFUND"
    NOTIFY = "NOTIFY"
    DEESCALATE = "DEESCALATE"
    RELAX = "RELAX"  # a confident harmless verdict undid this incident's 3-D Secure / SKU holds (policy R2)
    VOID = "VOID"  # released a held payment the attack matched (never settled), instead of capture + refund


class ActionStatus(StrEnum):
    EXECUTED = "executed"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    FAILED = "failed"
    # A refund committed as claimed before PayPal is called: a crash or a timeout leaves this on record,
    # and the sweeper (app/actions/executor.sweep_in_flight) re-sends it with the same PayPal-Request-Id.
    IN_FLIGHT = "in_flight"


# Refund states that settle a capture for good (one per capture, enforced by uq_actions_refund_attempt).
# FAILED is left out on purpose: a failed refund may be retried.
REFUND_CLAIM_STATUSES = (
    ActionStatus.EXECUTED,
    ActionStatus.AWAITING_APPROVAL,
    ActionStatus.REJECTED,
    ActionStatus.IN_FLIGHT,
)


class DeviceListKind(StrEnum):
    BLOCK = "block"  # guard check stops the device
    TRUST = "trust"  # owner unblocked it: never re-blocked or matched for refunds while the entry lasts


class CooldownReason(StrEnum):
    """Why the Gemini circuit breaker paused a model (app/ai/breaker.py)."""

    QUOTA = "quota"  # 429 RESOURCE_EXHAUSTED, per-minute limits
    DAILY_QUOTA = "daily_quota"  # 429 naming a per-day quota: retrying in a minute is pointless
    OVERLOADED = "overloaded"  # 5xx
    TIMEOUT = "timeout"  # our timeout or a network error
    BAD_REQUEST = "bad_request"  # other 4xx (unknown model, unsupported config): fails every time
    BAD_KEY = "bad_key"  # 401/403/invalid key: every model is down until the process restarts


# ---- tables ------------------------------------------------------------------------------------


class Shop(Base):
    __tablename__ = "shops"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(StrEnum):
    OWNER = "owner"  # everything, including settings, approvals and the team
    VIEWER = "viewer"  # sees everything and runs simulations; cannot change settings or move money


# What a member can be pushed about (app/push.py); each member picks theirs, all on by default.
PUSH_KINDS = ("incident", "approval", "dispute", "report")


class User(Base):
    """A team member of a shop. Passwords are scrypt hashes (app/passwords.py); the demo account has none
    and signs in with ADMIN_PASSWORD, so rotating that env var is enough."""

    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default=Role.VIEWER)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    push_kinds: Mapped[list[str]] = mapped_column(JsonB, default=lambda: list(PUSH_KINDS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushDevice(Base):
    """One browser or installed app that accepted notifications: its FCM registration token."""

    __tablename__ = "push_devices"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(512), unique=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """A secret key a shop's own checkout sends as X-Guard-Key. Only its SHA-256 is stored: the key itself is
    shown once, when it is created."""

    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16))  # the first characters, so the owner can tell keys apart
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantSettings(Base):
    __tablename__ = "merchant_settings"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), primary_key=True)
    autopilot: Mapped[bool] = mapped_column(Boolean, default=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    max_auto_refund_per_txn: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("10.00"))
    max_auto_refund_per_incident: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("100.00"))
    # Rolling 24 h across incidents: several incidents in one day cannot add up to more than this.
    max_auto_refund_per_day: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("250.00"), server_default="250.00"
    )
    # [{"start": iso, "end": iso, "note": "..."}]: owner-announced sales
    sale_windows: Mapped[list[dict[str, Any]]] = mapped_column(JsonB, default=list)
    deescalate_after_min: Mapped[int] = mapped_column(Integer, default=10)
    # Review window: a first-time shopper's payment is authorised at checkout and captured this many minutes
    # later, unless an attack it belongs to is confirmed first (then it is voided). 0: capture at once.
    review_hold_min: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GuardState(Base):
    __tablename__ = "guard_state"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default=GuardMode.NORMAL)
    # Device blocks live in device_entries (they expire and carry a reason); see app/guard/devices.py.
    held_skus: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    set_by_incident: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CheckoutAttempt(Base):
    __tablename__ = "checkout_attempts"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source: Mapped[str] = mapped_column(String(16), default=Source.WEB)
    # False when PayPal was not actually called (offline simulation / seeded history): keeps the demo honest.
    paypal_live: Mapped[bool] = mapped_column(Boolean, default=True)
    scenario: Mapped[str | None] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    method: Mapped[str] = mapped_column(String(10))
    fingerprint_hash: Mapped[str] = mapped_column(String(64), index=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64))  # keyed HMAC of the client IP (guard/signals.py)
    # Keyed HMAC of the client's network (IPv4 /24, IPv6 /48): a bot rotating device ids stays on one network.
    ip_net_hash: Mapped[str | None] = mapped_column(String(32))
    email_domain: Mapped[str | None] = mapped_column(String(255))
    payer_id_hash: Mapped[str | None] = mapped_column(String(64))
    card_bin6: Mapped[str | None] = mapped_column(String(6))
    card_last4: Mapped[str | None] = mapped_column(String(4))
    # YYYY-MM. Part of the card key, so expiry guessing on one card number counts as new cards.
    card_expiry: Mapped[str | None] = mapped_column(String(7))
    # From PayPal's answer for browser card orders (Card Fields): the number itself never reaches us.
    card_brand: Mapped[str | None] = mapped_column(String(20))
    # PayPal's payment_source.card.authentication_result: liability_shift (YES/POSSIBLE/NO/UNKNOWN) and the
    # 3-D Secure enrollment_status / authentication_status letters. NULL when 3-D Secure did not run.
    sca_liability_shift: Mapped[str | None] = mapped_column(String(10))
    sca_enrollment: Mapped[str | None] = mapped_column(String(2))
    sca_auth_status: Mapped[str | None] = mapped_column(String(2))
    guard_decision: Mapped[str] = mapped_column(String(20))
    guard_reason: Mapped[str | None] = mapped_column(String(60))
    paypal_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    order_status: Mapped[str | None] = mapped_column(String(40))
    capture_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Definitive (4xx) capture refusals so far. Part of the capture PayPal-Request-Id, so a capture
    # after 3-D Secure or a new funding source is a new request, not a replay of the cached refusal.
    capture_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Payment hold (app/actions/holds.py): why this order was authorised instead of captured ("review": a
    # first-time shopper's payment settles after the owner's review window; "incident": held while an
    # incident's defences were on), that incident, and PayPal's authorization id. A hold is then voided
    # (the attack matched it) or captured (everything else).
    hold_reason: Mapped[str | None] = mapped_column(String(12))
    hold_incident_id: Mapped[str | None] = mapped_column(String(16), index=True)
    authorization_id: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(20))
    # Canonical name from app/paypal/codes.py (e.g. SUSPECTED_FRAUD), set on DECLINED and ERROR rows.
    decline_code: Mapped[str | None] = mapped_column(String(80))
    # Raw processor_response.response_code (e.g. "9500") when PayPal sent one; 422s carry none.
    processor_code: Mapped[str | None] = mapped_column(String(20))
    decline_category: Mapped[str | None] = mapped_column(String(20))
    paypal_debug_id: Mapped[str | None] = mapped_column(String(40))
    refund_id: Mapped[str | None] = mapped_column(String(40))
    # True while a refund would risk paying twice: a dispute is open, or it ended with the buyer refunded.
    # A dispute settled in the seller's favour clears it. dispute_status is PayPal's last word on it.
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    dispute_status: Mapped[str | None] = mapped_column(String(40))
    # The dispute dispute_status is about: a CREATED for a different id is a new dispute, even after the
    # earlier one was resolved.
    dispute_id: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_attempts_shop_ts", "shop_id", "ts"),
        # The guard's per-device velocity count (app/guard/check.py) on every checkout.
        Index("ix_attempts_shop_fp_ts", "shop_id", "fingerprint_hash", "ts"),
        # The guard's per-network count (fresh devices with declines from one network).
        Index("ix_attempts_shop_net_ts", "shop_id", "ip_net_hash", "ts"),
    )


class PayPalEvent(Base):
    __tablename__ = "paypal_events"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    transmission_id: Mapped[str] = mapped_column(String(80), unique=True)
    event_id: Mapped[str | None] = mapped_column(String(80))
    event_type: Mapped[str | None] = mapped_column(String(80), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(80))
    verified: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Unmatched event waiting for its attempt row: skipped until then, so it never starves new events.
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_assessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default=IncidentStatus.OPEN)
    triggers: Mapped[list[str]] = mapped_column(JsonB, default=list)
    features: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)
    signature: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    classification: Mapped[str | None] = mapped_column(String(30))
    confidence: Mapped[float | None]
    # Who wrote the verdict's text: a Gemini model, or "template" (our own wording). The classification
    # itself is the scorecard's (decided_by).
    model_id: Mapped[str | None] = mapped_column(String(60))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    # Gemini answered the last assessment (explanation or second opinion), or a kept answer stands in.
    ai_available: Mapped[bool] = mapped_column(Boolean, default=True)
    # When the current classification was decided (AI or rules fallback). last_assessed_at moves on every
    # assessment, including a failed re-check that kept this verdict, so staleness is measured from here.
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A re-check failed but the last AI-confirmed tie-break was kept: separate from ai_available so the badge
    # doesn't flip to "rules fallback" mid-attack. Failures count consecutive AI misses (reset on success).
    ai_refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_refresh_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # In-flight claim: set (and committed) before the analyst call, so a second assessor skips the incident.
    assessing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the last assessment saw (app/incidents/cadence.py): decides whether a new AI call is worth it.
    assessment_context: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    previous_guard_state: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    # What the triggers alone suggest (app/policy/engine.rules_hint), kept apart from `classification` so a
    # rules guess is never presented as a verdict (the scorecard supersedes it).
    rules_hint: Mapped[str | None] = mapped_column(String(40))
    # Evidence items the analyst cited that did not match our computed numbers (dropped before display), for
    # the verdict on record; shown as "N AI claims dropped" so the owner sees the check working.
    dropped_evidence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Why the last assessment could not use the AI (quota, timeout…), set with a rules fallback or a kept
    # verdict, cleared when the AI answers again.
    ai_fallback_reason: Mapped[str | None] = mapped_column(String(120))
    # The features the verdict on record was validated against. `features` moves on with every assessment,
    # including a failed re-check that keeps the verdict; "×N vs normal" next to the verdict must use these.
    verdict_features: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    # The deterministic scorecard (app/detection/classifier.py) that decides the classification, and how
    # the decision was reached: "scorecard", "scorecard+gemini_agree" or "gemini_disagree_unclear"
    # (app/incidents/decision.py). class_probs/class_margin are the scorecard's own numbers; `scorecard`
    # holds its top reasons, scores, notes and the tie-break outcome for the dashboard.
    classifier_version: Mapped[str | None] = mapped_column(String(20))
    decided_by: Mapped[str | None] = mapped_column(String(30))
    class_probs: Mapped[dict[str, float] | None] = mapped_column(JsonB)
    class_margin: Mapped[float | None]
    scorecard: Mapped[dict[str, Any] | None] = mapped_column(JsonB)

    __table_args__ = (
        # One open incident per shop: the detector extends the open one instead of opening a second, and
        # this makes a second one impossible even if two detectors ever ran at once (the worker lock).
        Index("uq_incidents_one_open", "shop_id", unique=True, postgresql_where=text("status = 'open'")),
    )


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    params: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)
    policy_rule: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))
    approved_by: Mapped[str] = mapped_column(String(20), default="auto")
    paypal_ref: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When an IN_FLIGHT refund was last sent to PayPal: the sweeper re-sends ones older than its timeout.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One refund claim per capture across every incident: an owner's "Keep" (rejected) in one incident
        # is respected by the next, and two racing claimers cannot both refund. Postgres-only expression.
        Index(
            "uq_actions_refund_attempt",
            text("(params ->> 'attempt_id')"),
            unique=True,
            postgresql_where=text(
                "type = 'REFUND' AND status IN ('executed', 'awaiting_approval', 'rejected', 'in_flight')"
            ),
        ),
    )


class DisputeCase(Base):
    """A chargeback or PayPal dispute on one of our payments, and what to do about it (disputes/advisor.py):
    fixed rules recommend accept or contest from our own records, the AI drafts the note, the owner
    decides, and the answer goes to PayPal's Disputes API."""

    __tablename__ = "dispute_cases"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # PayPal's dispute id
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(String(16), index=True)
    incident_id: Mapped[str | None] = mapped_column(String(16))  # the attack it belonged to, if any
    reason: Mapped[str | None] = mapped_column(String(60))  # PayPal's reason, e.g. UNAUTHORISED
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    response_due: Mapped[str | None] = mapped_column(String(40))
    # open (waiting for the owner) | accepted | contested | dismissed | failed
    status: Mapped[str] = mapped_column(String(20), default="open")
    recommendation: Mapped[str] = mapped_column(String(10))  # accept | contest | review
    rule: Mapped[str] = mapped_column(String(40))  # which fixed rule recommended it
    evidence_type: Mapped[str | None] = mapped_column(String(40))
    evidence: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)  # e.g. {"refund_ids": [...]}
    note: Mapped[str] = mapped_column(Text)
    note_by: Mapped[str] = mapped_column(String(60))  # a Gemini model id, or "template"
    paypal_live: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paypal_result: Mapped[str | None] = mapped_column(Text)


class DeviceEntry(Base):
    """A blocked or owner-trusted device. Blocks expire (a reused or shared device id must not stay locked
    out for good); released_at is set when one expires, is unblocked or the guard is reset. Active means
    released_at IS NULL AND expires_at > now()."""

    __tablename__ = "device_entries"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))
    incident_id: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_device_entries_shop_kind", "shop_id", "kind", "expires_at"),
        # At most one unreleased entry per device and kind (expired ones are released by the prune first).
        Index(
            "uq_device_entries_open",
            "shop_id",
            "fingerprint",
            "kind",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class Report(Base):
    __tablename__ = "reports"
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), primary_key=True)
    alert_text: Mapped[str | None] = mapped_column(Text)
    report_md: Mapped[str | None] = mapped_column(Text)
    evidence_md: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(String(60))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(20))  # detector | ai | policy | executor | owner | system
    incident_id: Mapped[str | None] = mapped_column(String(16), index=True)
    event: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)


class AIModelState(Base):
    """Circuit-breaker state per Gemini model, shared by the API process and the worker.

    model "*" means every model (the key itself was rejected). Rows are scoped by key_id so state learned
    with an old key never blocks a new one. until NULL = paused until the next process restart.
    """

    __tablename__ = "ai_model_state"
    model: Mapped[str] = mapped_column(String(80), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(20))
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SystemStatus(Base):
    """Liveness of background components, written by the worker (app/worker/heartbeat.py) every few seconds
    and read by the dashboard's header pills. One row per component ("worker")."""

    __tablename__ = "system_status"
    component: Mapped[str] = mapped_column(String(20), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Last finished detector pass: a fresh heartbeat with an old tick means busy (waiting on an AI call).
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last failed detector pass (exception type only): a failing detector must not look busy or healthy.
    last_tick_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tick_error: Mapped[str | None] = mapped_column(String(120))
    last_ai_model: Mapped[str | None] = mapped_column(String(80))
    last_ai_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ai_error: Mapped[str | None] = mapped_column(Text)
    last_ai_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Detector numbers from the last pass (5 and 15 min windows and the shop's normal), for the live tiles.
    live: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
