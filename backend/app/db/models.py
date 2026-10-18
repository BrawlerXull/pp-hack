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
    REFUNDED = "refunded"


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


class ActionStatus(StrEnum):
    EXECUTED = "executed"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    FAILED = "failed"


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


class MerchantSettings(Base):
    __tablename__ = "merchant_settings"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), primary_key=True)
    autopilot: Mapped[bool] = mapped_column(Boolean, default=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    max_auto_refund_per_txn: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("10.00"))
    max_auto_refund_per_incident: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("100.00"))
    # [{"start": iso, "end": iso, "note": "..."}]: owner-announced sales
    sale_windows: Mapped[list[dict[str, Any]]] = mapped_column(JsonB, default=list)
    deescalate_after_min: Mapped[int] = mapped_column(Integer, default=10)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GuardState(Base):
    __tablename__ = "guard_state"
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), default=GuardMode.NORMAL)
    blocked_fingerprints: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
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
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    email_domain: Mapped[str | None] = mapped_column(String(255))
    payer_id_hash: Mapped[str | None] = mapped_column(String(64))
    card_bin6: Mapped[str | None] = mapped_column(String(6))
    card_last4: Mapped[str | None] = mapped_column(String(4))
    # YYYY-MM. Part of the card key, so expiry guessing on one card number counts as new cards.
    card_expiry: Mapped[str | None] = mapped_column(String(7))
    guard_decision: Mapped[str] = mapped_column(String(20))
    guard_reason: Mapped[str | None] = mapped_column(String(60))
    paypal_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    order_status: Mapped[str | None] = mapped_column(String(40))
    capture_id: Mapped[str | None] = mapped_column(String(40), index=True)
    # Definitive (4xx) capture refusals so far. Part of the capture PayPal-Request-Id, so a capture
    # after 3-D Secure or a new funding source is a new request, not a replay of the cached refusal.
    capture_tries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    outcome: Mapped[str] = mapped_column(String(20))
    # Canonical name from app/paypal/codes.py (e.g. SUSPECTED_FRAUD), set on DECLINED and ERROR rows.
    decline_code: Mapped[str | None] = mapped_column(String(80))
    # Raw processor_response.response_code (e.g. "9500") when PayPal sent one; 422s carry none.
    processor_code: Mapped[str | None] = mapped_column(String(20))
    decline_category: Mapped[str | None] = mapped_column(String(20))
    paypal_debug_id: Mapped[str | None] = mapped_column(String(40))
    refund_id: Mapped[str | None] = mapped_column(String(40))
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_attempts_shop_ts", "shop_id", "ts"),
        # The guard's per-device velocity count (app/guard/check.py) on every checkout.
        Index("ix_attempts_shop_fp_ts", "shop_id", "fingerprint_hash", "ts"),
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
    model_id: Mapped[str | None] = mapped_column(String(60))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    ai_available: Mapped[bool] = mapped_column(Boolean, default=True)
    # When the current classification was decided (AI or rules fallback). last_assessed_at moves on every
    # assessment, including a failed re-check that kept this verdict, so staleness is measured from here.
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A re-check failed but the last real AI verdict was kept: separate from ai_available so the badge
    # doesn't flip to "rules fallback" mid-attack. Failures count consecutive AI misses (reset on success).
    ai_refresh_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_refresh_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # In-flight claim: set (and committed) before the analyst call, so a second assessor skips the incident.
    assessing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # What the last assessment saw (app/incidents/cadence.py): decides whether a new AI call is worth it.
    assessment_context: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    previous_guard_state: Mapped[dict[str, Any] | None] = mapped_column(JsonB)
    # What the triggers alone suggest (app/policy/engine.rules_hint), kept apart from `classification` so a
    # rules guess is never presented as the AI's verdict.
    rules_hint: Mapped[str | None] = mapped_column(String(40))


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
