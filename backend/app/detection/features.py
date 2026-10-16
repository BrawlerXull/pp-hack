"""Deterministic feature extraction over rolling windows of checkout attempts."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.db.models import Outcome
from app.shop.catalog import CHEAPEST_SKU

WINDOWS_MIN = (1, 5, 15, 60)


class FeatureKey(StrEnum):
    """Every number the AI may cite as evidence. The analyst's evidence is validated against these."""

    ATTEMPTS = "attempts"
    ATTEMPTS_PER_MIN = "attempts_per_min"
    VELOCITY_Z = "velocity_z"
    DECLINE_RATE = "decline_rate"
    ERROR_RATE = "error_rate"
    CAPTURED = "captured"
    BLOCKED_OR_CHALLENGED = "blocked_or_challenged"
    SCA_REQUIRED = "sca_required"
    UNIQUE_FINGERPRINTS = "unique_fingerprints"
    TOP_FINGERPRINT_SHARE = "top_fingerprint_share"
    UNIQUE_CARDS_RATIO = "unique_cards_ratio"
    UNIQUE_PAYERS_RATIO = "unique_payers_ratio"
    TOP_SKU_SHARE = "top_sku_share"
    CHEAPEST_SKU_SHARE = "cheapest_sku_share"
    AVG_AMOUNT = "avg_amount"
    NEW_EMAIL_DOMAIN_SHARE = "new_email_domain_share"
    KNOWN_CUSTOMER_SHARE = "known_customer_share"
    WALLET_SHARE = "wallet_share"


@dataclass(frozen=True)
class AttemptRow:
    ts: datetime
    sku: str
    amount: float
    method: str
    fingerprint_hash: str
    email_domain: str | None
    card_key: str | None  # bin6+last4, never the full number
    payer_id_hash: str | None
    outcome: str
    decline_code: str | None


@dataclass
class Baseline:
    attempts_per_hour_mean: float = 3.0
    attempts_per_hour_std: float = 2.0
    decline_rate: float = 0.05
    error_rate: float = 0.01
    known_domains: frozenset[str] = field(default_factory=frozenset)
    known_fingerprints: frozenset[str] = field(default_factory=frozenset)
    history_days: int = 0

    def per_min(self) -> tuple[float, float]:
        # Floor the std so a very quiet shop doesn't flag every second order.
        return self.attempts_per_hour_mean / 60, max(self.attempts_per_hour_std / 60, 0.05)

    def public(self) -> dict[str, Any]:
        return {
            "attempts_per_hour_mean": round(self.attempts_per_hour_mean, 2),
            "attempts_per_hour_std": round(self.attempts_per_hour_std, 2),
            "decline_rate": round(self.decline_rate, 3),
            "error_rate": round(self.error_rate, 3),
            "known_email_domains": len(self.known_domains),
            "history_days": self.history_days,
        }


@dataclass
class Features:
    window_min: int
    attempts: int = 0
    attempts_per_min: float = 0.0
    velocity_z: float = 0.0
    reached_paypal: int = 0
    declined: int = 0
    decline_rate: float = 0.0
    errors: int = 0
    error_rate: float = 0.0
    captured: int = 0
    captured_amount: float = 0.0
    blocked_or_challenged: int = 0
    sca_required: int = 0
    unique_fingerprints: int = 0
    top_fingerprint_share: float = 0.0
    unique_cards_ratio: float = 0.0
    unique_payers_ratio: float = 0.0
    top_sku: str | None = None
    top_sku_share: float = 0.0
    cheapest_sku_share: float = 0.0
    avg_amount: float = 0.0
    unique_email_domains: int = 0
    new_email_domain_share: float = 0.0
    known_customer_share: float = 0.0
    wallet_share: float = 0.0
    decline_codes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _ratio(a: float, b: float) -> float:
    return a / b if b else 0.0


def compute(rows: list[AttemptRow], window_min: int, now: datetime, baseline: Baseline) -> Features:
    start = now - timedelta(minutes=window_min)
    w = [r for r in rows if start < r.ts <= now]
    f = Features(window_min=window_min, attempts=len(w))
    if not w:
        return f

    mean_pm, std_pm = baseline.per_min()
    f.attempts_per_min = len(w) / window_min
    f.velocity_z = (f.attempts_per_min - mean_pm) / std_pm

    reached = [r for r in w if r.outcome not in {Outcome.BLOCKED, Outcome.CHALLENGED}]
    f.reached_paypal = len(reached)
    f.declined = sum(r.outcome == Outcome.DECLINED for r in reached)
    f.errors = sum(r.outcome == Outcome.ERROR for r in reached)
    f.decline_rate = _ratio(f.declined, len(reached))
    f.error_rate = _ratio(f.errors, len(reached))
    captured = [r for r in w if r.outcome in {Outcome.CAPTURED, Outcome.REFUNDED}]
    f.captured = len(captured)
    f.captured_amount = sum(r.amount for r in captured)
    f.blocked_or_challenged = len(w) - len(reached)
    f.sca_required = sum(r.outcome == Outcome.SCA_REQUIRED for r in w)

    fps = Counter(r.fingerprint_hash for r in w)
    f.unique_fingerprints = len(fps)
    f.top_fingerprint_share = fps.most_common(1)[0][1] / len(w)
    f.known_customer_share = _ratio(sum(r.fingerprint_hash in baseline.known_fingerprints for r in w), len(w))

    cards = [r.card_key for r in w if r.card_key]
    f.unique_cards_ratio = _ratio(len(set(cards)), len(cards))
    payers = [r.payer_id_hash for r in w if r.payer_id_hash]
    f.unique_payers_ratio = _ratio(len(set(payers)), len(payers))
    f.wallet_share = _ratio(sum(r.method == "wallet" for r in w), len(w))

    skus = Counter(r.sku for r in w)
    f.top_sku, top_n = skus.most_common(1)[0]
    f.top_sku_share = top_n / len(w)
    f.cheapest_sku_share = skus.get(CHEAPEST_SKU, 0) / len(w)
    f.avg_amount = sum(r.amount for r in w) / len(w)

    domains = [r.email_domain for r in w if r.email_domain]
    f.unique_email_domains = len(set(domains))
    f.new_email_domain_share = _ratio(sum(d not in baseline.known_domains for d in domains), len(domains))
    f.decline_codes = dict(Counter(r.decline_code for r in reached if r.decline_code).most_common(6))
    return f


def compute_all(rows: list[AttemptRow], now: datetime, baseline: Baseline) -> dict[int, Features]:
    return {m: compute(rows, m, now, baseline) for m in WINDOWS_MIN}


def lookup(features: dict[int, Features], key: str, window: int) -> float | None:
    """Value the analyst's evidence claims are checked against."""
    f = features.get(window)
    if f is None or key not in FeatureKey.__members__.values():
        return None
    value = getattr(f, key, None)
    return float(value) if isinstance(value, int | float) else None
