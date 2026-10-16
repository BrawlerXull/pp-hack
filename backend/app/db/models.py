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


class ActionStatus(StrEnum):
    EXECUTED = "executed"
    AWAITING_APPROVAL = "awaiting_approval"
    REJECTED = "rejected"
    FAILED = "failed"


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
    guard_decision: Mapped[str] = mapped_column(String(20))
    guard_reason: Mapped[str | None] = mapped_column(String(60))
    paypal_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    order_status: Mapped[str | None] = mapped_column(String(40))
    capture_id: Mapped[str | None] = mapped_column(String(40), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    decline_code: Mapped[str | None] = mapped_column(String(80))
    paypal_debug_id: Mapped[str | None] = mapped_column(String(40))
    refund_id: Mapped[str | None] = mapped_column(String(40))
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_attempts_shop_ts", "shop_id", "ts"),)


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
    previous_guard_state: Mapped[dict[str, Any] | None] = mapped_column(JsonB)


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
