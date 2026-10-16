"""Deterministic incident triggers and attack signature.

Triggers are deliberately sensitive. Telling a real rush or an outage apart from an attack is
the AI analyst's job, and what we're allowed to do about it is the policy engine's job.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.db.models import Outcome
from app.detection.features import AttemptRow, Baseline, Features


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


DEFAULT_THRESHOLDS = TriggerThresholds()


def evaluate(
    features: dict[int, Features], baseline: Baseline, t: TriggerThresholds = DEFAULT_THRESHOLDS
) -> list[str]:
    f5, f15 = features[5], features[15]
    fired: list[str] = []
    if f5.attempts >= t.velocity_min_attempts_5m and f5.velocity_z > t.velocity_z:
        fired.append("velocity_spike")
    decline_limit = max(baseline.decline_rate * t.decline_multiplier, t.decline_floor)
    if f15.reached_paypal >= t.decline_min_attempts_15m and f15.decline_rate > decline_limit:
        fired.append("decline_spike")
    if f15.attempts >= t.sku_min_attempts_15m and f15.top_sku_share > t.sku_share:
        fired.append("sku_concentration")
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
    fingerprints: list[str] = field(default_factory=list)
    new_email_domains: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["window_start"] = self.window_start.isoformat()
        d["window_end"] = self.window_end.isoformat()
        return d


def signature(rows: list[AttemptRow], since: datetime, now: datetime, baseline: Baseline) -> Signature:
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
    suspicious = []
    for fp, attempts in per_fp.items():
        if fp in baseline.known_fingerprints:
            continue  # returning customer with a past successful purchase
        bad = sum(a.outcome in {Outcome.DECLINED, Outcome.SCA_REQUIRED} for a in attempts)
        distinct_cards = len({a.card_key for a in attempts if a.card_key})
        if bad >= 1 or len(attempts) >= 3 or distinct_cards >= 2:
            suspicious.append(fp)
    sig.fingerprints = sorted(suspicious)

    sig.new_email_domains = sorted(
        {r.email_domain for r in w if r.email_domain and r.email_domain not in baseline.known_domains}
    )
    return sig


def quiet_since(rows: list[AttemptRow], now: datetime, minutes: int) -> bool:
    """True when there are no suspicious events in the last `minutes` (used for de-escalation)."""
    start = now - timedelta(minutes=minutes)
    bad = {Outcome.DECLINED, Outcome.BLOCKED, Outcome.CHALLENGED, Outcome.SCA_REQUIRED}
    recent_bad = sum(1 for r in rows if r.ts > start and r.outcome in bad)
    return recent_bad <= 1
