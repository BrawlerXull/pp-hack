"""Deterministic incident triggers and attack signature.

Triggers are deliberately sensitive. Telling a real rush or an outage apart from an attack is
the AI analyst's job, and what we're allowed to do about it is the policy engine's job.

Each trigger has a tier, so the policy can act on the kind of signal before (and without) the AI:
  volume   velocity_spike, sku_concentration     busy, not necessarily bad (a single-product sale trips both)
  fraud    decline_spike, fraud_declines,         stolen credentials: banks refusing, cards cycling
           cheap_sku_card_cycling
  ops      processor_errors                       PayPal or our integration failing, not the shoppers
  holding  blocked_activity                       defences already stopping someone; keeps the incident alive
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.db.models import DeclineCategory, Outcome
from app.detection.features import AttemptRow, Baseline, Features


class Tier(StrEnum):
    VOLUME = "volume"
    FRAUD = "fraud"
    OPS = "ops"
    HOLDING = "holding"


TRIGGER_TIER: dict[str, Tier] = {
    "velocity_spike": Tier.VOLUME,
    "sku_concentration": Tier.VOLUME,
    "decline_spike": Tier.FRAUD,
    "fraud_declines": Tier.FRAUD,
    "cheap_sku_card_cycling": Tier.FRAUD,
    "processor_errors": Tier.OPS,
    "blocked_activity": Tier.HOLDING,
}
FRAUD_TRIGGERS = frozenset(t for t, tier in TRIGGER_TIER.items() if tier == Tier.FRAUD)


def tiers(triggers: Iterable[str]) -> frozenset[Tier]:
    return frozenset(TRIGGER_TIER[t] for t in triggers if t in TRIGGER_TIER)


@dataclass(frozen=True)
class TriggerThresholds:
    velocity_z: float = 4.0
    velocity_min_attempts_5m: int = 8
    decline_multiplier: float = 3.0
    decline_floor: float = 0.30
    decline_min_attempts_15m: int = 10
    sku_share: float = 0.80
    sku_min_attempts_15m: int = 15
    error_rate: float = 0.30
    error_min_attempts_5m: int = 5
    blocked_min_5m: int = 3
    fraud_declines_10m: int = 3
    # Card cycling on the cheapest item, 15 min: enough tries, enough distinct credentials of ONE kind
    # (cards, or PayPal payers), and enough of them refused. A sale on the cheap item has the volume and
    # the cards but not the declines.
    cycling_min_attempts: int = 10
    cycling_min_credentials: int = 5
    cycling_decline_rate: float = 0.30


DEFAULT_THRESHOLDS = TriggerThresholds()


def evaluate(
    features: dict[int, Features], baseline: Baseline, t: TriggerThresholds = DEFAULT_THRESHOLDS
) -> list[str]:
    f5, f10, f15 = features[5], features[10], features[15]
    fired: list[str] = []
    if f5.attempts >= t.velocity_min_attempts_5m and f5.velocity_z > t.velocity_z:
        fired.append("velocity_spike")
    decline_limit = max(baseline.decline_rate * t.decline_multiplier, t.decline_floor)
    if f15.reached_paypal >= t.decline_min_attempts_15m and f15.decline_rate > decline_limit:
        fired.append("decline_spike")
    if f15.attempts >= t.sku_min_attempts_15m and f15.top_sku_share > t.sku_share:
        fired.append("sku_concentration")
    if f10.fraud_declines >= t.fraud_declines_10m:
        fired.append("fraud_declines")
    if (
        f15.cheapest_sku_attempts >= t.cycling_min_attempts
        and max(f15.cheapest_sku_cards, f15.cheapest_sku_payers) >= t.cycling_min_credentials
        and f15.cheapest_sku_decline_rate >= t.cycling_decline_rate
    ):
        fired.append("cheap_sku_card_cycling")
    if f5.reached_paypal >= t.error_min_attempts_5m and f5.error_rate > t.error_rate:
        fired.append("processor_errors")
    if f5.blocked_or_challenged >= t.blocked_min_5m:
        # Defences are already holding the attacker off; keeps the incident alive while it lasts.
        fired.append("blocked_activity")
    return fired


@dataclass
class Signature:
    """Deterministic description of the attack, used to match refunds and block fingerprints.
    It is computed from the data, never taken from the model's output."""

    window_start: datetime
    window_end: datetime
    top_sku: str | None
    # Devices flagged as attacking (blocked under an attack verdict): several cards or PayPal payers, a
    # fraud-coded refusal, two guessing refusals, or SKU_ATTEMPTS_TO_FLAG refusals on the targeted item.
    fingerprints: list[str] = field(default_factory=list)
    # The subset that is unmistakably a bot without any AI judgement: several cards or payers, or several
    # fraud/probing refusals from one device. Rules-only blocking (policy R3b) uses these; their captures
    # are the only ones refunded without the owner (with currently blocked devices).
    strong_fingerprints: list[str] = field(default_factory=list)
    # One honest-looking bad outcome (a decline that is not fraud-coded): never blocked, and a capture from
    # it is refunded only with the owner's approval.
    weak_fingerprints: list[str] = field(default_factory=list)
    # Why each flagged device was flagged, for the owner's device list.
    flag_reasons: dict[str, str] = field(default_factory=dict)
    new_email_domains: list[str] = field(default_factory=list)
    # New domains that are themselves a signal: enough attempts and enough of them declined. A domain
    # match alone is still weak (owner approval): a real first-time shopper can share one.
    suspect_domains: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"] = self.window_end.isoformat()
        return d


STRONG_DECLINES = frozenset({DeclineCategory.FRAUD, DeclineCategory.PROBING})
SKU_ATTEMPTS_TO_FLAG = 3
SUSPECT_DOMAIN_MIN_ATTEMPTS = 3
SUSPECT_DOMAIN_DECLINE_RATE = 0.30


def signature(
    rows: list[AttemptRow],
    since: datetime,
    now: datetime,
    baseline: Baseline,
    trusted: frozenset[str] = frozenset(),
) -> Signature:
    """trusted: devices the owner unblocked; never flagged, whatever they do."""
    w = [r for r in rows if since <= r.ts <= now]
    sig = Signature(window_start=since, window_end=now, top_sku=None)
    if not w:
        return sig

    sku_counts: dict[str, int] = defaultdict(int)
    for r in w:
        sku_counts[r.sku] += 1
    top_sku, top_n = max(sku_counts.items(), key=lambda kv: kv[1])
    sig.top_sku = top_sku if top_n / len(w) >= 0.6 else None

    per_fp: dict[str, list[AttemptRow]] = defaultdict(list)
    for r in w:
        per_fp[r.fingerprint_hash].append(r)
    suspicious, strong, weak = [], [], []
    for fp, attempts in per_fp.items():
        if fp in baseline.known_fingerprints or fp in trusted:
            continue  # returning customer with a past successful purchase, or vouched for by the owner
        cards = len({a.card_key for a in attempts if a.card_key})
        payers = len({a.payer_id_hash for a in attempts if a.payer_id_hash})
        fraud = sum(a.decline_category == DeclineCategory.FRAUD for a in attempts)
        guessing = sum(a.decline_category in STRONG_DECLINES for a in attempts)
        # Refusals only: a shopper who pays (or retries through a PayPal outage, ERROR) is not attacking,
        # and a capture that counted here would get its own payment flagged and blocked.
        on_sku = (
            sum(a.sku == sig.top_sku and a.outcome == Outcome.DECLINED for a in attempts)
            if sig.top_sku
            else 0
        )
        reasons = [
            *([f"{cards} cards"] if cards >= 2 else []),
            *([f"{payers} PayPal accounts"] if payers >= 2 else []),
            *(["refused as stolen or restricted"] if fraud else []),
            *([f"{guessing} guessing refusals"] if guessing >= 2 and not fraud else []),
            *([f"{on_sku} refusals on the targeted item"] if on_sku >= SKU_ATTEMPTS_TO_FLAG else []),
        ]
        if reasons:
            suspicious.append(fp)
            sig.flag_reasons[fp] = ", ".join(reasons)
            if cards >= 2 or payers >= 2 or guessing >= 2:
                strong.append(fp)
        elif any(a.outcome == Outcome.DECLINED for a in attempts):
            weak.append(fp)
    sig.fingerprints = sorted(suspicious)
    sig.strong_fingerprints = sorted(strong)
    sig.weak_fingerprints = sorted(weak)

    per_domain: dict[str, list[AttemptRow]] = defaultdict(list)
    for r in w:
        if r.email_domain and r.email_domain not in baseline.known_domains:
            per_domain[r.email_domain].append(r)
    sig.new_email_domains = sorted(per_domain)
    sig.suspect_domains = sorted(
        d
        for d, attempts in per_domain.items()
        if len(attempts) >= SUSPECT_DOMAIN_MIN_ATTEMPTS
        and sum(a.outcome == Outcome.DECLINED for a in attempts) / len(attempts)
        >= SUSPECT_DOMAIN_DECLINE_RATE
    )
    return sig


def quiet_since(rows: list[AttemptRow], now: datetime, minutes: int) -> bool:
    """True when there are no suspicious events in the last `minutes` (used for de-escalation)."""
    start = now - timedelta(minutes=minutes)
    bad = {Outcome.DECLINED, Outcome.BLOCKED, Outcome.CHALLENGED, Outcome.SCA_REQUIRED}
    recent_bad = sum(1 for r in rows if r.ts > start and r.outcome in bad)
    return recent_bad <= 1
